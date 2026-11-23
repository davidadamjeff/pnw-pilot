#!/usr/bin/env python3
"""haupload-se: copy drive logs (qlog, rlog, optionally qcamera) to Home Assistant whenever there is a connection.

Receiver: the `comma_logs` custom integration in HA (copy in tools/homeassistant/comma_logs).
Config (not in git): /data/pnw/haupload/config.json  {"webhook_id": "...", "urls": ["http://192.168.0.199:8123", ...]}
State: /data/pnw/haupload/state.json  {"route--seg/file": size}; HA's manifest is the source of truth,
so a lost state file only costs one manifest fetch, never a re-upload.

Uploads on any connection (toggle HaUploadAnyNetwork, default on; off = unmetered Wi-Fi only, never the
iPhone hotspot), skips segments still being written,
verifies every file with size + sha256 on the HA side, and posts a status (pending count) to HA.
Display/telemetry only: touches nothing in the driving stack.
"""
import hashlib
import ipaddress
import json
import os
import socket
import time

import requests

from cereal import log
import cereal.messaging as messaging
from openpilot.common.params import Params
from openpilot.common.swaglog import cloudlog
from openpilot.system.hardware.hw import Paths

BASE = "/data/pnw/haupload"
CFG_FILE = f"{BASE}/config.json"
STATE_FILE = f"{BASE}/state.json"
STATUS_FILE = f"{BASE}/status.json"
KIND_ORDER = ("qlog.zst", "rlog.zst", "qcamera.ts")   # small and useful first
MAX_FAILS = 3          # per file per run; a file that keeps failing is skipped until the next restart
SETTLE_S = 120          # a segment is final once nothing in it changed for this long
IDLE_S = 300            # re-scan interval when everything is uploaded
STATUS_EVERY_S = 120
HOTSPOT_NETS = (ipaddress.ip_network("172.20.10.0/28"),)   # iPhone personal hotspot
WIFI = log.DeviceState.NetworkType.wifi
NONE = log.DeviceState.NetworkType.none
NET_NAMES = {0: "none", 1: "wifi", 2: "cell2G", 3: "cell3G", 4: "cell4G", 5: "cell5G", 6: "ethernet"}


def load_json(path, default):
  try:
    with open(path) as f:
      return json.load(f)
  except (OSError, ValueError):
    return default


def save_json(path, data, mode=0o644):
  os.makedirs(os.path.dirname(path), exist_ok=True)
  tmp = path + ".tmp"
  with open(tmp, "w") as f:
    json.dump(data, f)
  os.chmod(tmp, mode)
  os.replace(tmp, path)


def local_ip():
  s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
  try:
    s.connect(("10.255.255.255", 1))   # sends nothing; picks the outgoing interface
    return s.getsockname()[0]
  except OSError:
    return None
  finally:
    s.close()


def network_ok(network_type, metered, ip, any_network=True):
  """any_network (default): upload on whatever link the comma has -- Wi-Fi, phone hotspot or metered.
  Otherwise only unmetered Wi-Fi, never the phone hotspot (NM can't always tell it's metered)."""
  if not ip or network_type == NONE:
    return False
  if any_network:
    return True
  if network_type != WIFI or metered:
    return False
  try:
    addr = ipaddress.ip_address(ip)
  except ValueError:
    return False
  return not any(addr in n for n in HOTSPOT_NETS)


def any_network(params):
  try:
    return bool(params.get_bool("HaUploadAnyNetwork"))
  except Exception:      # key not built yet on this device: the owner's choice is "always upload"
    return True


def net_name(network_type, ip):
  try:
    if ip and any(ipaddress.ip_address(ip) in n for n in HOTSPOT_NETS):
      return "hotspot"
  except ValueError:
    pass
  return NET_NAMES.get(int(network_type), "?")


def kinds(params):
  k = ["qlog.zst", "rlog.zst"]
  if params.get_bool("HaUploadCamera"):
    k.append("qcamera.ts")
  return k


def segment_key(name):
  """'00000029--74f5996f4a--9' -> sortable (route, seg)."""
  try:
    a, b, c = name.split("--")
    return (a, b, int(c))
  except ValueError:
    return None


def candidates(root, wanted, done, now=None):
  """Files to upload, as [(rel, path, size)], ordered by kind priority then newest segment first."""
  now = time.time() if now is None else now
  segs = []
  try:
    names = os.listdir(root)
  except OSError:
    return []
  for name in names:
    key = segment_key(name)
    d = os.path.join(root, name)
    if key is None or not os.path.isdir(d):
      continue
    try:
      files = os.listdir(d)
    except OSError:
      continue
    if any(f.endswith(".lock") for f in files):
      continue
    try:
      newest = max(os.path.getmtime(os.path.join(d, f)) for f in files) if files else now
    except OSError:
      continue
    if now - newest < SETTLE_S:
      continue
    segs.append((key, name, files))
  segs.sort(reverse=True)
  out = []
  for kind in KIND_ORDER:
    if kind not in wanted:
      continue
    for _, name, files in segs:
      if kind not in files:
        continue
      p = os.path.join(root, name, kind)
      try:
        size = os.path.getsize(p)
      except OSError:
        continue
      if size == 0:          # empty file from an aborted segment: nothing to keep
        continue
      rel = f"{name}/{kind}"
      if done.get(rel) != size:
        out.append((rel, p, size))
  return out


class Uploader:
  def __init__(self, params=None, session=None):
    self.params = params or Params()
    self.s = session or requests.Session()
    self.done = load_json(STATE_FILE, {})
    self.uploaded_session = 0
    self.base = None
    self.last_status = 0.0
    self.error = ""
    self.failures = {}   # rel -> failed attempts this run; skipped after MAX_FAILS

  def endpoint(self, base, cfg):
    return f"{base.rstrip('/')}/api/webhook/{cfg['webhook_id']}"

  def pick_base(self, cfg):
    """First URL whose manifest answers; merges the manifest into the done-set."""
    for base in cfg.get("urls", []):
      try:
        r = self.s.get(self.endpoint(base, cfg), timeout=(4, 30))
        if r.status_code == 200:
          files = r.json().get("files", {})
          self.done = {k: v for k, v in self.done.items() if files.get(k, v) == v}
          self.done.update(files)
          save_json(STATE_FILE, self.done)
          return base
        self.error = f"{base}: HTTP {r.status_code}"
      except (requests.RequestException, ValueError) as e:
        self.error = f"{base}: {type(e).__name__}"
    return None

  def upload(self, cfg, rel, path, size):
    # Send bytes, not the file object: requests falls back to chunked encoding for a file it can't size
    # (e.g. an empty qlog), and HA rejects chunked + Content-Length. Logs are a few MB, so memory is fine.
    with open(path, "rb") as f:
      body = f.read()
    if len(body) != size:
      raise RuntimeError("file changed while reading")
    sha = hashlib.sha256(body).hexdigest()
    r = self.s.put(self.endpoint(self.base, cfg), params={"path": rel}, data=body,
                   headers={"X-Size": str(size), "X-Sha256": sha, "Content-Type": "application/octet-stream"},
                   timeout=(5, 300))
    if r.status_code != 200:
      raise RuntimeError(f"HTTP {r.status_code} {r.text[:120]}")
    self.done[rel] = size
    self.uploaded_session += 1
    save_json(STATE_FILE, self.done)

  def status(self, cfg, state, pending, network):
    st = {"state": state, "pending_files": len(pending), "pending_bytes": sum(p[2] for p in pending),
          "network": network, "uploaded_session": self.uploaded_session, "url": self.base or "",
          "error": self.error, "version": "haupload-se 1", "t": time.time()}
    save_json(STATUS_FILE, st)
    if self.base:
      try:
        self.s.post(self.endpoint(self.base, cfg), json=st, timeout=(4, 15))
        self.last_status = time.monotonic()
      except requests.RequestException:
        pass


def main():
  try:
    os.nice(10)
  except OSError:
    pass
  params = Params()
  sm = messaging.SubMaster(["deviceState"])
  up = Uploader(params)
  root = Paths.log_root()
  backoff = 30.0
  while True:
    sm.update(1000)
    cfg = load_json(CFG_FILE, {})
    if not cfg.get("webhook_id") or not cfg.get("urls"):
      save_json(STATUS_FILE, {"state": "no config", "t": time.time()})
      time.sleep(60)
      continue
    ds = sm["deviceState"]
    ip = local_ip()
    if not network_ok(ds.networkType, ds.networkMetered, ip, any_network(params)):
      up.base = None
      save_json(STATUS_FILE, {"state": "waiting for network", "t": time.time(), "uploaded_session": up.uploaded_session})
      time.sleep(15)
      continue
    if up.base is None:
      up.base = up.pick_base(cfg)
      if up.base is None:
        save_json(STATUS_FILE, {"state": "HA unreachable", "error": up.error, "t": time.time()})
        time.sleep(backoff)
        backoff = min(backoff * 2, 900)
        continue
      up.error = ""
    net = net_name(ds.networkType, ip)
    pending = [p for p in candidates(root, kinds(params), up.done) if up.failures.get(p[0], 0) < MAX_FAILS]
    if not pending:
      up.status(cfg, "up to date", pending, net)
      time.sleep(IDLE_S)
      up.base = None          # re-check manifest + nearest URL next round
      continue
    if time.monotonic() - up.last_status > STATUS_EVERY_S:
      up.status(cfg, "uploading", pending, net)
    rel, path, size = pending[0]
    try:
      up.upload(cfg, rel, path, size)
      backoff = 30.0
    except (OSError, RuntimeError, requests.RequestException) as e:
      up.failures[rel] = up.failures.get(rel, 0) + 1
      up.error = f"{rel}: {e}"[:200]
      cloudlog.warning(f"haupload-se: {up.error}")
      up.status(cfg, "error", pending, net)
      up.base = None
      time.sleep(backoff)
      backoff = min(backoff * 2, 900)


if __name__ == "__main__":
  main()
