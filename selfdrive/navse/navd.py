#!/usr/bin/env python3
"""navse: Navigate on openpilot (Sweden fork). Route engine daemon.

Inputs : /data/pnw/nav/destination.json (set from the phone page, navse/web.py), the Mapbox key,
         the LastGPSPosition mem param (mapd_configd, ~1 Hz) and carState.vEgo for dead reckoning.
Outputs: NavStateSE mem param (JSON, 5 Hz) -> on-screen card, route speed control, turn desires;
         cereal navInstruction (1 Hz) / navRoute (on change) for logs.
Never commands anything itself: consumers decide, each behind its own toggle."""
import threading
import time

import cereal.messaging as messaging
from openpilot.common.params import Params
from openpilot.common.realtime import Ratekeeper
from openpilot.common.swaglog import cloudlog
from openpilot.selfdrive.navse import mapbox, store
from openpilot.selfdrive.navse.route import Route, Tracker

HZ = 10
STATE_EVERY = 2                  # NavStateSE at 5 Hz
INSTR_EVERY = 10                 # navInstruction at 1 Hz
GPS_MAX_AGE_S = 5.0
REROUTE_MIN_S = 12.0             # between route requests
MAX_REQ_PER_HOUR = 40            # runaway guard (a GPS fault must not burn the Mapbox quota)
PRECHOSEN_MAX_AGE_S = 20 * 60    # a route picked on the phone is used if this fresh and we are on it
ARRIVED_CLEAR_S = 60.0           # destination is cleared this long after arrival
POS_SAVE_S = 60.0


class Navd:
  def __init__(self):
    self.params = Params()
    self.mem = Params("/dev/shm/params")
    self.sm = messaging.SubMaster(["carState"])
    self.pm = messaging.PubMaster(["navInstruction", "navRoute"])
    self.route = None
    self.tracker = None
    self.dest = None
    self.dest_mtime = None
    self.state = "idle"
    self.err = ""
    self.fetching = False
    self.fetch_result = None       # (dest_key, data | None, err)
    self.req_times = []
    self.last_req_t = -1e9
    self.last_fix_ts = None
    self.fix = None                # (lat, lon, bearing, speed, monotonic ts)
    self.arrived_t = None
    self.last_pos_save = -1e9
    self.route_sent = False
    self.frame = 0
    self.enabled = True

  # ---- inputs ----------------------------------------------------------------------------------
  def _read_fix(self):
    try:
      g = self.mem.get("LastGPSPosition", return_default=True)
      if isinstance(g, (bytes, str)):
        import json
        g = json.loads(g)
      if not isinstance(g, dict):
        return None
      ts = float(g.get("ts") or 0.0)
      if time.monotonic() - ts > GPS_MAX_AGE_S:
        return None
      return (float(g["latitude"]), float(g["longitude"]), g.get("bearing"), float(g.get("speed") or 0.0), ts)
    except Exception:
      return None

  def _read_dest(self):
    mt = store.mtime(store.DEST_FILE)
    if mt == self.dest_mtime:
      return
    self.dest_mtime = mt
    d = store.read_json(store.DEST_FILE) if mt is not None else None
    if d is not None and not store._valid_place(d):
      d = None
    if (d or {}).get("ts") != (self.dest or {}).get("ts"):
      self._clear_route()
    self.dest = d
    if d is None:
      self.state, self.err = "idle", ""
    else:
      cloudlog.event("navse_destination", name=d.get("name"), prechosen="route" in d)

  def _clear_route(self):
    self.route = None
    self.tracker = None
    self.arrived_t = None
    self.route_sent = False

  # ---- routing ---------------------------------------------------------------------------------
  def _dest_key(self):
    return None if self.dest is None else (self.dest.get("ts"), self.dest["lat"], self.dest["lon"])

  def _can_request(self, now):
    self.req_times = [t for t in self.req_times if now - t < 3600]
    return now - self.last_req_t >= REROUTE_MIN_S and len(self.req_times) < MAX_REQ_PER_HOUR and not self.fetching

  def _start_fetch(self, now, reason):
    key = store.mapbox_key()
    if not key:
      self.state, self.err = "error", "no Mapbox key"
      return
    lat, lon, brg, _, _ = self.fix
    dest = (float(self.dest["lat"]), float(self.dest["lon"]))
    dkey = self._dest_key()
    self.fetching = True
    self.last_req_t = now
    self.req_times.append(now)
    cloudlog.event("navse_route_request", reason=reason, n_hour=len(self.req_times))

    def worker():
      try:
        data = mapbox.directions(key, (lat, lon), dest, bearing=brg)
        self.fetch_result = (dkey, data, "")
      except mapbox.MapboxError as e:
        self.fetch_result = (dkey, None, str(e))
      except Exception as e:  # the daemon must survive anything the network does
        self.fetch_result = (dkey, None, type(e).__name__)
      finally:
        self.fetching = False

    threading.Thread(target=worker, daemon=True).start()

  def _take_fetch_result(self):
    if self.fetch_result is None:
      return
    dkey, data, err = self.fetch_result
    self.fetch_result = None
    if dkey != self._dest_key():
      return                        # destination changed while fetching
    if data is None:
      self.state, self.err = ("rerouting" if self.route else "error"), err
      cloudlog.event("navse_route_failed", err=err)
      return
    try:
      self._set_route(Route(data))
    except Exception as e:
      self.state, self.err = "error", f"bad route ({type(e).__name__})"

  def _set_route(self, route):
    self.route = route
    self.tracker = Tracker(route)
    if self.fix is not None:
      lat, lon, brg, spd, ts = self.fix
      self.tracker.update_fix(time.monotonic(), lat, lon, brg, spd)
    self.route_sent = False
    self.state, self.err = "active", ""
    cloudlog.event("navse_route", km=round(route.length / 1000, 1), min=round(route.duration / 60), summary=route.summary)

  def _try_prechosen(self):
    """Use the route the driver picked on the phone if it is fresh and we are on it."""
    d = self.dest or {}
    if "route" not in d or self.fix is None or time.time() - float(d.get("ts") or 0) > PRECHOSEN_MAX_AGE_S:  # noqa: TID251 -- wall clock on purpose (ETA / destination age)
      return False
    try:
      r = Route(d["route"])
    except Exception:
      return False
    loc = r.locate(self.fix[0], self.fix[1])
    if loc is None or loc[1] > 60.0 or loc[0] > 500.0:
      return False
    self._set_route(r)
    return True

  # ---- outputs ---------------------------------------------------------------------------------
  def _state_dict(self, now):
    st = {"ts": now, "wall": time.time(), "state": self.state, "err": self.err,  # noqa: TID251 -- wall clock on purpose (ETA / destination age)
          "dest": None if self.dest is None else {k: self.dest.get(k) for k in ("name", "address", "lat", "lon")}}
    t = self.tracker
    if self.route is not None and t is not None and t.s is not None:
      r = self.route
      ups = r.next_maneuvers(t.s)
      remaining = max(0.0, r.length - t.s)
      trem = r.time_remaining(t.s)
      st.update({
        "valid": self.state == "active" and not t.off_route,
        "s": round(t.s, 1), "perp": round(t.perp, 1),
        "dist_remaining": round(remaining), "time_remaining": round(trem), "eta": round(time.time() + trem),  # noqa: TID251 -- wall clock on purpose (ETA / destination age)
        "speed_limit": r.speed_limit_at(t.s),
        "maneuvers": [m.as_dict(d) for m, d in ups],
        "summary": r.summary,
      })
    else:
      st["valid"] = False
    return st

  def _publish(self, st):
    try:
      self.mem.put_nonblocking("NavStateSE", st)
    except Exception:
      pass
    if self.frame % INSTR_EVERY == 0:
      msg = messaging.new_message("navInstruction", valid=bool(st.get("valid")))
      ni = msg.navInstruction
      ms = st.get("maneuvers") or []
      if ms:
        m = ms[0]
        ni.maneuverPrimaryText = m.get("text") or ""
        ni.maneuverSecondaryText = m.get("secondary") or ""
        ni.maneuverDistance = float(m["dist_m"])
        ni.maneuverType = m.get("type") or ""
        ni.maneuverModifier = m.get("modifier") or ""
        ni.allManeuvers = [{"distance": float(x["dist_m"]), "type": x.get("type") or "", "modifier": x.get("modifier") or ""} for x in ms]
      ni.distanceRemaining = float(st.get("dist_remaining") or 0)
      ni.timeRemaining = float(st.get("time_remaining") or 0)
      if st.get("speed_limit"):
        ni.speedLimit = float(st["speed_limit"])
        ni.speedLimitSign = "vienna"
      self.pm.send("navInstruction", msg)
    if self.route is not None and not self.route_sent:
      msg = messaging.new_message("navRoute")
      step = max(1, len(self.route.pts) // 1500)
      msg.navRoute.coordinates = [{"latitude": a, "longitude": b} for a, b in self.route.pts[::step]]
      self.pm.send("navRoute", msg)
      self.route_sent = True

  # ---- loop ------------------------------------------------------------------------------------
  def step(self):
    now = time.monotonic()
    self.sm.update(0)
    if self.frame % (2 * HZ) == 0:
      try:
        self.enabled = self.params.get_bool("NavigationSE")
      except Exception:
        self.enabled = True
    self._read_dest()
    if not self.enabled:
      self.state = "off"
      self._clear_route()
    fix = self._read_fix()
    if fix is not None:
      self.fix = fix
      if fix[4] != self.last_fix_ts:
        self.last_fix_ts = fix[4]
        if self.tracker is not None:
          self.tracker.update_fix(now, fix[0], fix[1], fix[2], fix[3])
        if now - self.last_pos_save > POS_SAVE_S:
          self.last_pos_save = now
          try:
            store.write_json(store.POS_FILE, {"lat": fix[0], "lon": fix[1], "t": time.time()})  # noqa: TID251 -- wall clock on purpose (ETA / destination age)
          except OSError:
            pass
    v_ego = self.sm["carState"].vEgo if self.sm.alive["carState"] and self.sm.recv_frame["carState"] > 0 else None
    if self.tracker is not None:
      self.tracker.advance(now, v_ego)

    self._take_fetch_result()
    if self.enabled and self.dest is not None:
      gps_ok = self.fix is not None and now - self.fix[4] <= GPS_MAX_AGE_S
      if self.route is None:
        if not gps_ok:
          self.state = "waiting_gps" if not self.fetching else self.state
        elif not self._try_prechosen() and self._can_request(now):
          self.state = "routing"
          self._start_fetch(now, "new")
      elif self.tracker is not None:
        if self.tracker.arrived():
          if self.arrived_t is None:
            self.arrived_t = now
            cloudlog.event("navse_arrived")
          self.state = "arrived"
          if now - self.arrived_t > ARRIVED_CLEAR_S:
            store.clear_destination()
        elif self.tracker.off_route and gps_ok:
          self.state = "rerouting"
          if self._can_request(now):
            self._start_fetch(now, "offroute")
        elif self.state in ("rerouting", "routing", "waiting_gps"):
          self.state = "active"
      # far from the destination with no route at all and no GPS is fine: just keep waiting

    if self.frame % STATE_EVERY == 0:
      self._publish(self._state_dict(now))
    self.frame += 1


def main():
  navd = Navd()
  rk = Ratekeeper(HZ, print_delay_threshold=None)
  while True:
    try:
      navd.step()
    except Exception:  # keep running; a nav fault must never take anything else down
      cloudlog.exception("navse: step failed")
      time.sleep(1.0)
    rk.keep_time()


if __name__ == "__main__":
  main()
