"""speedcam-se: fixed speed camera slowdown (AutoSpeedReduce >= 1) -- ease to the posted limit, no margin."""
from openpilot.selfdrive.controls.lib.speedadjust_pnw.tests.test_speedadjust import _ctrl, _cap, _settle
KMH = 1 / 3.6
L70, SET90, V90 = 70 * KMH, 90 * KMH, 90 * KMH


def _cam(dist_m, cid="C1"):
  return {"state": "ok", "dist_m": dist_m, "id": cid, "road": "E4", "name": "Test"}


def _c(mode=1, cam=None, sl=L70):
  c = _ctrl(mode=mode, sl=sl, sl_ref=sl, ratio=SET90 / sl if sl else 0.0)
  c._camera = cam
  return c


def test_camera_close_caps_to_limit():
  assert abs(_settle(_c(cam=_cam(400)), SET90, V90) - L70) < 1e-6     # 400 m at 25 m/s = 16 s < 20 s


def test_camera_far_no_action():
  assert _cap(_c(cam=_cam(1400)), SET90, V90) == SET90               # 56 s away


def test_camera_off_mode_no_action():
  assert _cap(_c(mode=0, cam=_cam(300)), SET90, V90) == SET90


def test_camera_no_limit_no_cap():
  assert _cap(_c(cam=_cam(300), sl=0.0), SET90, V90) == SET90


def test_camera_latch_holds_when_slowed_then_releases():
  c = _c(cam=_cam(400))
  _settle(c, SET90, V90)
  c._camera = _cam(450)                                               # slower now -> ttr grows, still latched
  assert abs(_settle(c, SET90, 10.0) - L70) < 1e-6
  c._camera = None                                                    # passed
  _cap(c, SET90, L70)                                                 # starts the release debounce
  c._release_t -= 3.0                                                 # ...which is wall-clock; age it past RELEASE_S
  out = _settle(c, SET90, L70)
  assert out == SET90


def test_camera_driver_override_dismisses():
  c = _c(cam=_cam(300))
  _settle(c, SET90, V90)
  out = _settle(c, 100 * KMH, V90)                                    # driver raises the set
  assert abs(out - 100 * KMH) < 1e-6


def test_camera_dismissal_survives_gps_blip():
  c = _c(cam=_cam(300))
  _settle(c, SET90, V90)
  _settle(c, 100 * KMH, V90)                                          # driver dismisses
  c._camera = None                                                    # GPS blip
  _cap(c, 100 * KMH, V90)
  c._camera = _cam(250)                                               # same camera again
  assert abs(_settle(c, 100 * KMH, V90) - 100 * KMH) < 1e-6


def test_camera_without_id_ignored():
  c = _c(cam={"state": "ok", "dist_m": 200})
  assert _cap(c, SET90, V90) == SET90
