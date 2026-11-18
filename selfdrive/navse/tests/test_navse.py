"""navse: route engine, tracker, store and the desire/speed consumers, against a real Mapbox response
(Stockholm C -> Uppsala, 2026-11-18, language=sv). Runs off-device: python -m pytest selfdrive/navse/tests"""
import bisect
import json
import os
import random

from openpilot.selfdrive.navse.route import KMH, Route, Tracker, maneuver_target_speed

FIXTURE = os.path.join(os.path.dirname(__file__), "data", "route_sthlm_uppsala.json")


def _route():
  with open(FIXTURE) as f:
    return Route(json.load(f))


def _point_at(r, s):
  i = min(max(0, bisect.bisect_right(r.cum, s) - 1), len(r.pts) - 2)
  f = (s - r.cum[i]) / max(1e-6, r.cum[i + 1] - r.cum[i])
  a, b = r.pts[i], r.pts[i + 1]
  return a[0] + (b[0] - a[0]) * f, a[1] + (b[1] - a[1]) * f, r.seg_bearing[i]


def test_parse():
  r = _route()
  assert 69000 < r.length < 71500
  types = [(m.type, m.modifier) for m in r.maneuvers]
  assert ("turn", "left") in types and ("off ramp", "slight right") in types
  assert any(m.type == "roundabout" for m in r.maneuvers)
  exit_m = next(m for m in r.maneuvers if m.type == "off ramp")
  assert exit_m.exit == "186" and "Uppsala" in exit_m.text
  assert sum(1 for x in r.maxspeed if x) > 1000          # speed limits line up with the geometry
  assert abs(r.speed_limit_at(40000) - 110 * KMH) < 0.1   # E4 motorway


def test_target_speeds():
  assert abs(maneuver_target_speed("turn", "left") - 25 * KMH) < 1e-6
  assert abs(maneuver_target_speed("roundabout", "slight right") - 30 * KMH) < 1e-6
  assert maneuver_target_speed("off ramp", "slight right") is None   # never brake in the motorway lane
  assert maneuver_target_speed("fork", "left") is None
  assert maneuver_target_speed("turn", "uturn") >= 4.4704             # >= speedadjust MIN_CAP
  assert maneuver_target_speed("depart", None) is None


def test_drive_whole_route_with_gps_noise():
  r = _route()
  t = Tracker(r)
  rnd = random.Random(7)
  now, s_true, v = 0.0, 0.0, 12.0
  while s_true < r.length:
    lat, lon, brg = _point_at(r, s_true)
    t.update_fix(now, lat + rnd.gauss(0, 4e-5), lon + rnd.gauss(0, 8e-5), brg + rnd.gauss(0, 5), v)
    for _ in range(10):
      now += 0.1
      t.advance(now, v)
    s_true += v
    assert not t.off_route, s_true
    assert abs(t.s - s_true) < 40, (s_true, t.s)
    v = 30.0 if 3500 < s_true < 61000 else 12.0
  assert t.arrived()


def test_off_route_and_recovery():
  r = _route()
  t = Tracker(r)
  lat, lon, brg = _point_at(r, 1500)
  t.update_fix(0, lat, lon, brg, 10)
  for k in range(1, 3):
    t.update_fix(k, lat, lon + 0.004, brg, 10)             # ~230 m east
  assert not t.off_route                                   # two bad fixes are not enough
  t.update_fix(3, lat, lon + 0.004, brg, 10)
  assert t.off_route
  t.update_fix(4, lat, lon, brg, 10)
  assert not t.off_route


def test_wrong_direction_counts_as_off_route():
  r = _route()
  t = Tracker(r)
  lat, lon, brg = _point_at(r, 30000)
  for k in range(4):
    t.update_fix(k, lat, lon, (brg + 180) % 360, 25)
  assert t.off_route


def test_next_maneuvers_and_time():
  r = _route()
  ups = r.next_maneuvers(0.0)
  assert ups and ups[0][0].type == "turn" and 300 < ups[0][1] < 500
  assert abs(r.time_remaining(0.0) - r.duration) < 1.0
  assert r.time_remaining(r.length) == 0.0
  assert r.next_maneuvers(r.length) == []


def test_store_roundtrip(tmp_path, monkeypatch):
  from openpilot.selfdrive.navse import store
  monkeypatch.setattr(store, "NAV_DIR", str(tmp_path))
  tok = store.web_token()
  assert len(tok) >= 8 and store.web_token() == tok
  store.set_destination({"lat": 59.85, "lon": 17.64, "name": "Uppsala C", "address": "x"})
  assert store.read_json(store.DEST_FILE)["name"] == "Uppsala C"
  assert store.places()["recents"][0]["name"] == "Uppsala C"
  store.save_favorite({"lat": 59.3, "lon": 18.0, "name": "Hem"}, "home")
  store.save_favorite({"lat": 59.4, "lon": 18.1, "name": "Hem2"}, "home")     # replaces the old home
  assert [f["name"] for f in store.places()["favorites"]] == ["Hem2"]
  store.clear_destination()
  assert store.read_json(store.DEST_FILE) is None
