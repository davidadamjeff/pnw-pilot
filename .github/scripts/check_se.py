#!/usr/bin/env python3
# ruff: noqa: E501, E701, E702  (compact CI marker table)
"""Checks run after merging Dirk's branch into david/3testpnw-se (see workflows/sync-dirk.yaml).
Pure stdlib: no openpilot build needed. Exit 1 on any failure."""
import json
import math
import os
import py_compile
import subprocess
import sys
import types

fails = []
def fail(msg): fails.append(msg); print("FAIL:", msg)

old = sys.argv[1] if len(sys.argv) > 1 else "HEAD~1"
changed = subprocess.run(["git", "diff", "--name-only", "--diff-filter=AM", old, "HEAD"],
                         capture_output=True, text=True).stdout.split()

# 1. every changed Python file still compiles
for f in changed:
  if f.endswith(".py") and os.path.exists(f):
    try:
      py_compile.compile(f, doraise=True)
    except py_compile.PyCompileError as e:
      fail(f"syntax: {f}: {e.msg.strip()}")
if subprocess.run(["bash", "-n", "launch_env.sh"]).returncode != 0:
  fail("syntax: launch_env.sh")

# 2. our changes are still there
MARKERS = {
  "system/location_services/location_servicesd.py": ["class SpeedCameras", "class CameraRefresher", "def _line_camera", 'out["camera"]', "policebudget-se", "police_budget.DriveGate", "def _line_waze_other", 'out["waze"]', "enabled_types"],
  "system/location_services/police_budget.py": ["class DriveGate", "def record_use", "WAZE_TYPES", "def usage_summary"],
  "selfdrive/ui/onroad/location_services_status.py": ["def _camera_line", "def _waze_line"],
  "selfdrive/controls/lib/speedadjust_pnw/speedadjust_controller.py": ["def _camera_cap", "cc = self._camera_cap(v_ego)", "def _nav_cap", "nc = self._nav_cap(v_ego)"],
  "common/connect_backend.py": ['BACKEND_COMMA = "comma"', "BACKEND_COMMA)", "def with_pairing_host"],
  "launch_env.sh": ['"$CONNECT_BACKEND" = "4"', "https://api.commadotai.com"],
  "system/loggerd/uploader.py": ["def _comma_backend", "if self._comma:"],
  "system/ui/widgets/network.py": ['tr("comma")'],
  "selfdrive/ui/widgets/prime.py": ["commabackend-se"],
  "selfdrive/haupload_se/hauploadd.py": ["def network_ok", "def candidates", "HOTSPOT_NETS"],
  "selfdrive/navse/navd.py": ["class Navd", "NavStateSE"],
  "selfdrive/navse/route.py": ["class Route", "class Tracker", "def maneuver_target_speed"],
  "selfdrive/navse/web.py": ["def _authed", "store.web_token()"],
  "selfdrive/controls/lib/desire_helper.py": ["def nav_desire_for", "nav_turn"],
  "selfdrive/ui/onroad/augmented_road_view.py": ["NavSECard"],
  "system/manager/process_config.py": ['"navd_se"', '"navweb_se"'],
  "selfdrive/controls/lib/vtsc_pnw/vtsc_controller.py": ['get("DisableVtsc")'],
  "common/params_keys.h": ['"DisableVtsc"', '"PoliceReports"', '"AccidentReports"', '"HazardReports"', '"RoadClosureReports"', '"NavigationSE"', '"NavStateSE"', '"NavTurnDesires"'],
  "selfdrive/ui/layouts/settings/toggles.py": ['"PoliceReports": (', '"HazardReports": (', "usage_summary"],
  ".gitmodules": ["https://github.com/dirkpetersen/pnw-panda.git", "https://github.com/dirkpetersen/pnw-opendbc.git"],
}
for f, needles in MARKERS.items():
  try:
    text = open(f).read()
  except OSError:
    fail(f"missing file: {f}"); continue
  for n in needles:
    if n not in text:
      fail(f"lost change in {f}: {n!r}")

# 3. speed camera selection logic (synthetic cameras, no data file needed)
try:
  sys.path.insert(0, "system/location_services")
  import geo
  src = open("system/location_services/location_servicesd.py").read()
  helpers = src[src.index("# ----------------------------- speed cameras (speedcam-se)"):src.index("\n\ndef _line_camera")]
  line_cam = src[src.index("def _line_camera"):src.index("# ----------------------------- main")]
  ns = {"os": os, "json": json, "math": math, "geo": geo, "time": __import__("time"), "threading": __import__("threading"),
        "urllib": __import__("urllib.request"), "cloudlog": types.SimpleNamespace(info=lambda *a: None, warning=lambda *a: None, exception=print)}
  exec(helpers + "\n\n" + line_cam, ns)
  tmp = "/tmp/cams_ci.json"
  cams = [{"id": "A", "lat": 59.40, "lon": 17.95, "bearing": 180, "name": "north-bound", "road": "E4"},
          {"id": "B", "lat": 59.40, "lon": 17.95, "bearing": 0, "name": "south-bound", "road": "E4"}]
  json.dump({"cameras": cams}, open(tmp, "w"))
  ns["CAM_FILE"] = tmp
  sc = ns["SpeedCameras"](); sc.reload()
  south = (59.40 - 800 / 111320, 17.95)          # 800 m south of the cameras, driving north
  r = ns["_line_camera"](sc, south[0], south[1], 0.0, [])
  if r is None or r[0]["id"] != "A": fail(f"camera ahead not picked (got {r})")
  north = (59.40 + 800 / 111320, 17.95)          # 800 m north, driving south
  r = ns["_line_camera"](sc, north[0], north[1], 180.0, [])
  if r is None or r[0]["id"] != "B": fail(f"opposite-direction camera not picked (got {r})")
  r = ns["_line_camera"](sc, north[0], north[1], 0.0, [])   # driving away
  if r is not None: fail(f"camera behind us was picked ({r})")
except Exception as e:
  fail(f"camera logic test crashed: {type(e).__name__}: {e}")

print("OK" if not fails else f"{len(fails)} check(s) failed")
sys.exit(1 if fails else 0)
