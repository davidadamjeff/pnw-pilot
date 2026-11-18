"""policebudget-se: own-key (direct OpenWebNinja) police polling on a tiny monthly quota.

The free OpenWebNinja plan allows 50 requests/month, so polling is rationed:
  * a poll window opens only on a "long" drive: the drive has lasted >= activate_after_drive_s
    (default 30 min) OR the car has been continuously >= activate_speed_kmh (80 km/h) for
    activate_speed_hold_s (10 min);
  * once open: poll immediately, then every interval_s (default 1 h) until the drive ends;
  * a drive ends after idle_end_s (10 min) without motion (speed < moving_kmh) or without GPS speed;
  * a persistent per-month counter hard-stops at monthly_cap (default 45, 5 spare under the 50 limit).
Pure/stdlib so it is unit-testable off-device. Times are monotonic seconds; the counter file is the
only persistence. Never raises: a broken counter file fails CLOSED (no poll)."""
import json
import os
import time

DEFAULTS = {
  "monthly_cap": 45,
  "activate_after_drive_s": 1800,
  "activate_speed_kmh": 80.0,
  "activate_speed_hold_s": 600,
  "interval_s": 3600,
  "moving_kmh": 5.0,
  "idle_end_s": 600,
  "bbox_deg": 0.8,          # ~±90 km lat: an hourly poll should cover the next hour of driving
  "max_alerts": 50,
}
USAGE_FILE = "/data/pnw/location/police_budget_usage.json"


def settings(cfg_budget) -> dict:
  s = dict(DEFAULTS)
  if isinstance(cfg_budget, dict):
    for k, v in cfg_budget.items():
      if k in DEFAULTS and isinstance(v, (int, float)) and not isinstance(v, bool) and v > 0:
        s[k] = v
  return s


class DriveGate:
  """Decides when an own-key poll is due. Feed update(now, speed_ms) every few seconds."""
  def __init__(self, s: dict):
    self.s = s
    self.drive_start = None      # monotonic start of the current drive (None = parked)
    self.last_moving = None      # last time speed >= moving_kmh
    self.fast_since = None       # start of the current continuous >= activate_speed_kmh streak
    self.active = False          # poll window open for this drive
    self.last_poll = None

  def update(self, now: float, speed_ms):
    s = self.s
    kmh = None if speed_ms is None else speed_ms * 3.6
    if kmh is not None and kmh >= s["moving_kmh"]:
      if self.drive_start is None:
        self.drive_start = now
      self.last_moving = now
    # drive end: no motion (or no GPS speed) for idle_end_s
    if self.drive_start is not None and (self.last_moving is None or now - self.last_moving >= s["idle_end_s"]):
      self.drive_start = self.last_moving = self.fast_since = self.last_poll = None
      self.active = False
      return
    if kmh is not None and kmh >= s["activate_speed_kmh"]:
      self.fast_since = now if self.fast_since is None else self.fast_since
    else:
      self.fast_since = None
    if self.drive_start is not None and not self.active:
      long_drive = now - self.drive_start >= s["activate_after_drive_s"]
      fast_hold = self.fast_since is not None and now - self.fast_since >= s["activate_speed_hold_s"]
      if long_drive or fast_hold:
        self.active = True

  def due(self, now: float) -> bool:
    return self.active and (self.last_poll is None or now - self.last_poll >= self.s["interval_s"])

  def mark_polled(self, now: float):
    self.last_poll = now

  def reason(self, now: float) -> str:
    if self.drive_start is None:
      return "own key: parked"
    if not self.active:
      return "own key: waiting (30 min / 10 min ≥80)"
    left = int(max(0, self.s["interval_s"] - (now - (self.last_poll or now))) // 60)
    return f"own key: next in {left} min"


def _month(epoch: float) -> str:
  return time.strftime("%Y-%m", time.gmtime(epoch))


def read_usage(path=None, now_epoch=None):
  """-> (month, used). Missing file -> (this month, 0). Corrupt file -> (this month, None) = fail closed."""
  path = USAGE_FILE if path is None else path
  now_epoch = time.time() if now_epoch is None else now_epoch  # noqa: TID251 -- calendar month needs wall clock
  m = _month(now_epoch)
  try:
    with open(path) as f:
      d = json.load(f)
    used = int(d.get("used", 0))
    return m, (used if d.get("month") == m else 0)
  except FileNotFoundError:
    return m, 0
  except (OSError, ValueError, TypeError, AttributeError):
    return m, None


def quota_left(cap, path=None, now_epoch=None) -> int:
  _, used = read_usage(path, now_epoch)
  return 0 if used is None else max(0, int(cap) - used)


def record_use(path=None, now_epoch=None) -> int:
  """Count ONE request BEFORE it is sent (a request that fails still costs quota). Atomic write."""
  path = USAGE_FILE if path is None else path
  m, used = read_usage(path, now_epoch)
  used = (used or 0) + 1
  tmp = path + ".tmp"
  try:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(tmp, "w") as f:
      json.dump({"month": m, "used": used}, f)
      f.flush()
      os.fsync(f.fileno())
    os.replace(tmp, path)
  except OSError:
    pass
  return used
