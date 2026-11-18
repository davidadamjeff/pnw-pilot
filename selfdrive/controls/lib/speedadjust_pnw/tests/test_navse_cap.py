"""navse: route speed control -- slow for the route's turns/roundabouts (AutoSpeedReduce >= 1, NavSpeedControl)."""
import time

from openpilot.selfdrive.controls.lib.speedadjust_pnw.tests.test_speedadjust import _ctrl, _cap, _settle

KMH = 1 / 3.6
SET70, V70 = 70 * KMH, 70 * KMH
T25 = 25 * KMH


def _nav(dist_m, idx=5, target=T25, mtype="turn", valid=True, age=0.0):
  return {"valid": valid, "ts": time.monotonic() - age,
          "maneuvers": [{"idx": idx, "dist_m": dist_m, "type": mtype, "modifier": "left", "target_ms": target}]}


def _c(mode=1, nav=None):
  c = _ctrl(mode=mode)
  c._nav = nav
  return c


def test_turn_far_away_no_action():
  assert _cap(_c(nav=_nav(900)), SET70, V70) == SET70        # needs (19.4^2-6.9^2)/2 + 58 = ~223 m


def test_turn_inside_braking_distance_caps():
  assert abs(_settle(_c(nav=_nav(200)), SET70, V70) - T25) < 1e-6


def test_latch_holds_after_slowing_then_releases_when_passed():
  c = _c(nav=_nav(200))
  _settle(c, SET70, V70)
  c._nav = _nav(120)
  assert abs(_settle(c, SET70, 8.0) - T25) < 1e-6              # slower now: not "needed" any more, still latched
  c._nav = {"valid": True, "ts": time.monotonic(), "maneuvers": []}   # passed: navd dropped it
  _cap(c, SET70, T25)
  c._release_t -= 3.0
  assert _settle(c, SET70, T25) == SET70


def test_no_target_no_cap():
  assert _cap(_c(nav=_nav(100, target=None, mtype="off ramp")), SET70, V70) == SET70


def test_stale_state_ignored():
  assert _cap(_c(nav=_nav(150, age=10.0)), SET70, V70) == SET70


def test_mode_off_no_action():
  assert _cap(_c(mode=0, nav=_nav(150)), SET70, V70) == SET70


def test_driver_override_dismisses_that_maneuver():
  c = _c(nav=_nav(200))
  _settle(c, SET70, V70)
  out = _settle(c, 80 * KMH, V70)                               # driver raises the set
  assert abs(out - 80 * KMH) < 1e-6
  c._nav = _nav(150)                                            # same maneuver, closer
  assert abs(_settle(c, 80 * KMH, V70) - 80 * KMH) < 1e-6
  c._nav = _nav(200, idx=6)                                     # the NEXT turn slows again
  assert abs(_settle(c, 80 * KMH, V70) - T25) < 1e-6
