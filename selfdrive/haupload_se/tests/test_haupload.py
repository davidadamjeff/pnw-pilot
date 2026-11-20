import os
import time

from openpilot.selfdrive.haupload_se import hauploadd as hu


def _seg(root, name, files, age=1000):
  d = root / name
  d.mkdir()
  for f, n in files.items():
    p = d / f
    p.write_bytes(b"x" * n)
    t = time.time() - age
    os.utime(p, (t, t))


def test_network_gate():
  assert hu.network_ok(hu.WIFI, False, "192.168.0.162")
  assert not hu.network_ok(hu.WIFI, True, "192.168.0.162")
  assert not hu.network_ok(hu.WIFI, False, "172.20.10.3")   # iPhone hotspot
  assert not hu.network_ok(hu.WIFI + 1, False, "192.168.0.162")
  assert not hu.network_ok(hu.WIFI, False, None)


def test_candidates_order_settle_lock_done(tmp_path):
  _seg(tmp_path, "00000001--aaaa--0", {"qlog.zst": 10, "rlog.zst": 100, "qcamera.ts": 50})
  _seg(tmp_path, "00000001--aaaa--1", {"qlog.zst": 11, "rlog.zst": 101})
  _seg(tmp_path, "00000002--bbbb--0", {"qlog.zst": 5}, age=10)            # still settling
  _seg(tmp_path, "00000003--cccc--0", {"qlog.zst": 5, "rlog.zst.lock": 0})  # being written
  _seg(tmp_path, "boot", {"qlog.zst": 5})                                  # not a segment
  _seg(tmp_path, "00000000--zzzz--0", {"qlog.zst": 0, "rlog.zst": 0})      # empty: aborted segment
  done = {"00000001--aaaa--0/qlog.zst": 10, "00000001--aaaa--1/rlog.zst": 999}  # wrong size -> redo
  c = [r for r, _, _ in hu.candidates(str(tmp_path), ["qlog.zst", "rlog.zst"], done)]
  assert c == ["00000001--aaaa--1/qlog.zst", "00000001--aaaa--1/rlog.zst", "00000001--aaaa--0/rlog.zst"]
  c2 = [r for r, _, _ in hu.candidates(str(tmp_path), ["qlog.zst", "rlog.zst", "qcamera.ts"], done)]
  assert c2[-1] == "00000001--aaaa--0/qcamera.ts"


def test_segment_key_sorts_numerically():
  names = ["00000001--a--10", "00000001--a--9", "00000002--b--0"]
  assert sorted(names, key=hu.segment_key, reverse=True) == ["00000002--b--0", "00000001--a--10", "00000001--a--9"]


def test_upload_sends_bytes_with_length(tmp_path, monkeypatch):
  monkeypatch.setattr(hu, "STATE_FILE", str(tmp_path / "state.json"))
  sent = {}

  class R:
    status_code = 200
    text = ""

  class S:
    def put(self, url, params, data, headers, timeout):
      sent.update(data=data, headers=headers, params=params)
      return R()

  for size in (0, 5):
    p = tmp_path / f"f{size}"
    p.write_bytes(b"y" * size)
    u = hu.Uploader(params=object(), session=S())
    u.base = "http://ha"
    u.upload({"webhook_id": "w"}, f"00000001--a--{size}/qlog.zst", str(p), size)
    assert isinstance(sent["data"], bytes) and len(sent["data"]) == size
    assert "Content-Length" not in sent["headers"] and sent["headers"]["X-Size"] == str(size)
    assert u.done[f"00000001--a--{size}/qlog.zst"] == size
