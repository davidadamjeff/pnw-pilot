"""navse: route hints into the driving model -- always driver-confirmed (blinker for turns, nudge for keeps)."""
import time

from cereal import log
from openpilot.selfdrive.controls.lib.desire_helper import LaneChangeState
from openpilot.selfdrive.controls.lib.tests.test_desire_helper import _CS, _dh

D = log.Desire


class _Mem:
  def __init__(self, st):
    self.st = st

  def get(self, key, return_default=True):
    return self.st if key == "NavStateSE" else None


class _P:
  def __init__(self, on=True):
    self.on = on

  def get_bool(self, key):
    return self.on if key == "NavTurnDesires" else False


class _CS2(_CS):
  def __init__(self, *a, standstill=False, **k):
    super().__init__(*a, **k)
    self.standstill = standstill


def _st(dist, mtype="turn", mod="left", valid=True, age=0.0):
  return {"valid": valid, "ts": time.monotonic() - age,
          "maneuvers": [{"idx": 3, "dist_m": dist, "type": mtype, "modifier": mod}]}


def _run(monkeypatch, st, cs, on=True, ticks=4, lateral=True):
  d = _dh(monkeypatch)
  d.params = _P(on)
  d.mem_params = _Mem(st)
  for _ in range(ticks):
    d.update(cs, lateral, 0.0)
  return d


def test_turn_left_with_matching_blinker(monkeypatch):
  d = _run(monkeypatch, _st(40), _CS2(8.0, left_blinker=True))
  assert d.desire == D.turnLeft and d.lane_change_state == LaneChangeState.off


def test_turn_without_blinker_no_hint(monkeypatch):
  assert _run(monkeypatch, _st(40), _CS2(8.0)).desire == D.none


def test_turn_wrong_blinker_no_hint(monkeypatch):
  assert _run(monkeypatch, _st(40), _CS2(8.0, right_blinker=True)).desire != D.turnRight


def test_turn_too_far_or_too_fast(monkeypatch):
  assert _run(monkeypatch, _st(200), _CS2(8.0, left_blinker=True)).desire == D.none
  assert _run(monkeypatch, _st(40), _CS2(16.0, left_blinker=True)).desire != D.turnLeft


def test_turn_blocked_by_blindspot(monkeypatch):
  assert _run(monkeypatch, _st(40), _CS2(8.0, left_blinker=True, left_blindspot=True)).desire != D.turnLeft


def test_toggle_off_no_hint(monkeypatch):
  assert _run(monkeypatch, _st(40), _CS2(8.0, left_blinker=True), on=False).desire == D.none


def test_stale_or_invalid_no_hint(monkeypatch):
  assert _run(monkeypatch, _st(40, age=5.0), _CS2(8.0, left_blinker=True)).desire == D.none
  assert _run(monkeypatch, _st(40, valid=False), _CS2(8.0, left_blinker=True)).desire == D.none


def test_turn_hint_suppresses_lane_change_at_35kmh(monkeypatch):
  # 12 m/s is above the lane-change floor: without navse the blinker would arm a lane change
  d = _run(monkeypatch, _st(60), _CS2(12.0, left_blinker=True, steering_pressed=True, steering_torque=1.0), ticks=10)
  assert d.desire == D.turnLeft and d.lane_change_state == LaneChangeState.off


def test_keep_right_at_exit_needs_nudge(monkeypatch):
  st = _st(120, mtype="off ramp", mod="slight right")
  assert _run(monkeypatch, st, _CS2(28.0)).desire == D.none
  d = _run(monkeypatch, st, _CS2(28.0, steering_pressed=True, steering_torque=-1.0))
  assert d.desire == D.keepRight


def test_no_hint_when_lateral_inactive(monkeypatch):
  assert _run(monkeypatch, _st(40), _CS2(8.0, left_blinker=True), lateral=False).desire == D.none
