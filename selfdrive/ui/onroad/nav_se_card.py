"""navse: turn-by-turn card (top-left, under the set-speed box). Reads the NavStateSE mem param at 5 Hz.
Display only. Drawn arrows (no icon assets): straight / slight / turn / sharp / U-turn / roundabout / arrive."""
import math
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import pyray as rl

from openpilot.common.params import Params
from openpilot.selfdrive.ui.onroad.hud_renderer import UI_CONFIG
from openpilot.selfdrive.ui.ui_state import ui_state
from openpilot.system.ui.lib.application import gui_app, FontWeight
from openpilot.system.ui.lib.text_measure import measure_text_cached
from openpilot.system.ui.widgets import Widget

_REFRESH_S = 0.2
_STALE_S = 3.0
_X = 60
_Y = 45 + UI_CONFIG.set_speed_height + 28
_W = 640
_H = 176
_PAD = 18
_ICON = 140
try:
  _TZ = ZoneInfo("Europe/Stockholm")   # the device clock is UTC
except Exception:
  _TZ = None
_SHOW = ("active", "rerouting", "arrived", "routing")
_BTN = 104          # cancel button (right of the card): tap once to arm, again within _ARM_S to cancel
_BTN_ARMED_W = 250
_ARM_S = 4.0
RED = rl.Color(200, 45, 45, 235)

WHITE = rl.Color(255, 255, 255, 240)
DIM = rl.Color(170, 178, 174, 230)
AMBER = rl.Color(245, 190, 60, 240)
GREEN = rl.Color(90, 205, 115, 240)
BG = rl.Color(10, 30, 60, 200)

_ANGLE = {"straight": 0, "slight right": 45, "right": 90, "sharp right": 135,
          "slight left": -45, "left": -90, "sharp left": -135, "uturn": 180}


# The prebaked UI atlases (*.fnt) have no "å"/"Å", "…" or "·" -- load the bundled Inter TTFs with the glyphs
# Swedish street names need. Falls back to the UI font if anything goes wrong.
_CODEPOINTS = list(range(32, 127)) + list(range(160, 384)) + [0x2013, 0x2014, 0x2026, 0x2192, 0x00B7]


def _load_ttf(name, fallback):
  try:
    import os
    import raylib as _r
    from openpilot.common.basedir import BASEDIR
    path = os.path.join(BASEDIR, "selfdrive", "assets", "fonts", name)
    if not os.path.isfile(path):
      return fallback
    arr = _r.ffi.new("int[]", _CODEPOINTS)
    font = _r.LoadFontEx(path.encode(), 96, arr, len(_CODEPOINTS))
    if font.texture.id == 0:
      return fallback
    rl.gen_texture_mipmaps(font.texture)
    rl.set_texture_filter(font.texture, rl.TextureFilter.TEXTURE_FILTER_TRILINEAR)
    return font
  except Exception:
    return fallback


def _tri(a, b, c, color):
  rl.draw_triangle(a, b, c, color)     # raylib only fills counter-clockwise triangles: draw both windings
  rl.draw_triangle(a, c, b, color)


def _head(tip, ang_deg, size, color):
  a = math.radians(ang_deg)
  dx, dy = math.sin(a), -math.cos(a)            # 0 deg = up
  bx, by = tip.x - dx * size, tip.y - dy * size
  px, py = -dy, dx
  _tri(tip, rl.Vector2(bx + px * size * 0.6, by + py * size * 0.6), rl.Vector2(bx - px * size * 0.6, by - py * size * 0.6), color)


def _poly(pts, thick, color):
  for a, b in zip(pts, pts[1:], strict=False):
    rl.draw_line_ex(a, b, thick, color)
    rl.draw_circle_v(b, thick / 2, color)


def draw_maneuver(mtype, modifier, cx, cy, s, color):
  t = (mtype or "").lower()
  m = (modifier or "straight").lower()
  th = s * 0.11
  hs = s * 0.2
  if t == "arrive":
    rl.draw_circle_v(rl.Vector2(cx, cy), s * 0.22, color)
    rl.draw_circle_v(rl.Vector2(cx, cy), s * 0.1, BG)
    return
  if t in ("roundabout", "rotary", "roundabout turn", "exit roundabout", "exit rotary"):
    r = s * 0.17
    rl.draw_ring(rl.Vector2(cx, cy - s * 0.05), r - th / 2, r + th / 2, 0, 360, 36, color)
    _poly([rl.Vector2(cx, cy + s * 0.42), rl.Vector2(cx, cy - s * 0.05 + r)], th, color)
    ang = _ANGLE.get(m, 0)
    a = math.radians(ang)
    sx, sy = cx + math.sin(a) * r, cy - s * 0.05 - math.cos(a) * r
    ex, ey = cx + math.sin(a) * s * 0.4, cy - s * 0.05 - math.cos(a) * s * 0.4
    _poly([rl.Vector2(sx, sy), rl.Vector2(ex, ey)], th, color)
    _head(rl.Vector2(ex + math.sin(a) * hs * 0.6, ey - math.cos(a) * hs * 0.6), ang, hs, color)
    return
  if m == "uturn":
    pts = [rl.Vector2(cx + s * 0.15, cy + s * 0.4), rl.Vector2(cx + s * 0.15, cy - s * 0.1)]
    for k in range(1, 9):
      a = math.pi * k / 8
      pts.append(rl.Vector2(cx + math.cos(a) * s * 0.15, cy - s * 0.1 - math.sin(a) * s * 0.15))
    pts.append(rl.Vector2(cx - s * 0.15, cy + s * 0.12))
    _poly(pts, th, color)
    _head(rl.Vector2(cx - s * 0.15, cy + s * 0.12 + hs * 0.6), 180, hs, color)
    return
  ang = _ANGLE.get(m, 0)
  a = math.radians(ang)
  ex, ey = cx + math.sin(a) * s * 0.36, cy - math.cos(a) * s * 0.36
  if ang == 0:
    pts = [rl.Vector2(cx, cy + s * 0.42), rl.Vector2(cx, cy - s * 0.3)]
    ex, ey = cx, cy - s * 0.3
  else:
    pts = [rl.Vector2(cx, cy + s * 0.42), rl.Vector2(cx, cy), rl.Vector2(ex, ey)]
  _poly(pts, th, color)
  _head(rl.Vector2(ex + math.sin(a) * hs * 0.6, ey - math.cos(a) * hs * 0.6), ang, hs, color)


class NavSECard(Widget):
  def __init__(self):
    super().__init__()
    try:
      self._mem = Params("/dev/shm/params")
    except Exception:
      self._mem = None
    self._st = {}
    self._last = 0.0
    self._metric = True
    self.font = _load_ttf("Inter-Medium.ttf", gui_app.font(FontWeight.MEDIUM))
    self.font_bold = _load_ttf("Inter-Bold.ttf", gui_app.font(FontWeight.BOLD))
    self._btn = None            # hit rect of the cancel button while the card is shown
    self._armed_until = 0.0

  def handle_tap(self, pos) -> bool:
    """navcancel-se: called by the onroad view on a press. True = the tap was ours (don't toggle the sidebar).
    Two taps on purpose: one stray touch while driving must not drop the route."""
    if self._btn is None:
      return False
    b = self._btn
    hit = rl.Rectangle(b.x - 20, b.y - 20, b.width + 40, b.height + 40)
    if not rl.check_collision_point_rec(pos, hit):
      return False
    now = time.monotonic()
    if now < self._armed_until:
      self._armed_until = 0.0
      try:
        from openpilot.selfdrive.navse import store
        store.clear_destination()
        self._st = {}
        self._btn = None
      except Exception:
        pass
    else:
      self._armed_until = now + _ARM_S
    return True

  def _draw_cancel(self, x, y):
    armed = time.monotonic() < self._armed_until
    w = _BTN_ARMED_W if armed else _BTN
    self._btn = rl.Rectangle(x, y, w, _BTN)
    rl.draw_rectangle_rounded(self._btn, 0.5, 12, RED if armed else BG)
    if armed:
      tw = measure_text_cached(self.font_bold, "Avbryt?", 44).x
      rl.draw_text_ex(self.font_bold, "Avbryt?", rl.Vector2(x + (w - tw) / 2, y + (_BTN - 44) / 2), 44, 0, WHITE)
    else:
      c, d = rl.Vector2(x + _BTN / 2, y + _BTN / 2), _BTN * 0.2
      rl.draw_line_ex(rl.Vector2(c.x - d, c.y - d), rl.Vector2(c.x + d, c.y + d), 9, WHITE)
      rl.draw_line_ex(rl.Vector2(c.x - d, c.y + d), rl.Vector2(c.x + d, c.y - d), 9, WHITE)

  def _update_state(self):
    now = time.monotonic()
    if now - self._last < _REFRESH_S:
      return
    self._last = now
    try:
      st = self._mem.get("NavStateSE", return_default=True) if self._mem is not None else None
      self._st = st if isinstance(st, dict) and now - float(st.get("ts") or 0) < _STALE_S else {}
      self._metric = ui_state.params.get_bool("IsMetric")
    except Exception:
      self._st = {}

  def _dist(self, m):
    if m is None:
      return ""
    if self._metric:
      if m >= 1000:
        return f"{m / 1000:.1f} km" if m < 10000 else f"{m / 1000:.0f} km"
      return f"{int(round(m / 50.0) * 50) if m >= 300 else int(round(m / 10.0) * 10)} m"
    mi = m / 1609.344
    return f"{mi:.1f} mi" if mi >= 0.2 else f"{int(round(m * 3.28084 / 50) * 50)} ft"

  def _fit(self, text, font, fs, max_w):
    while fs > 24 and measure_text_cached(font, text, fs).x > max_w:
      fs -= 2
    if measure_text_cached(font, text, fs).x > max_w:
      while text and measure_text_cached(font, text + "…", fs).x > max_w:
        text = text[:-1]
      text += "…"
    return text, fs

  def _render(self, rect):
    st = self._st
    if not st or st.get("state") not in _SHOW:
      self._btn = None
      return
    x, y = rect.x + _X, rect.y + _Y
    rl.draw_rectangle_rounded(rl.Rectangle(x, y, _W, _H), 0.15, 10, BG)
    self._draw_cancel(x + _W + 16, y)
    tx = x + _PAD * 2 + _ICON
    tw = _W - (tx - x) - _PAD
    state = st.get("state")
    ms = st.get("maneuvers") or []
    if state == "arrived":
      draw_maneuver("arrive", None, x + _PAD + _ICON / 2, y + _H / 2, _ICON, GREEN)
      rl.draw_text_ex(self.font_bold, "Framme", rl.Vector2(tx, y + 30), 64, 0, GREEN)
      name = (st.get("dest") or {}).get("name") or ""
      t, fs = self._fit(name, self.font, 38, tw)
      rl.draw_text_ex(self.font, t, rl.Vector2(tx, y + 106), fs, 0, WHITE)
      return
    if state in ("routing", "rerouting") or not ms:
      draw_maneuver("", "straight", x + _PAD + _ICON / 2, y + _H / 2, _ICON, AMBER)
      label = "Räknar om…" if state == "rerouting" else "Hämtar rutt…"
      rl.draw_text_ex(self.font_bold, label, rl.Vector2(tx, y + 40), 54, 0, AMBER)
      if st.get("err"):
        err = {"no internet": "inget internet", "no Mapbox key": "ingen Mapbox-nyckel"}.get(str(st["err"]), str(st["err"]))
        t, fs = self._fit(err, self.font, 34, tw)
        rl.draw_text_ex(self.font, t, rl.Vector2(tx, y + 108), fs, 0, DIM)
      return
    m = ms[0]
    draw_maneuver(m.get("type"), m.get("modifier"), x + _PAD + _ICON / 2, y + _H / 2, _ICON, WHITE)
    rl.draw_text_ex(self.font_bold, self._dist(m.get("dist_m")), rl.Vector2(tx, y + 12), 70, 0, WHITE)
    label = m.get("text") or m.get("name") or ""
    if m.get("rb_exit"):                       # roundabout: Mapbox's banner is a whole sentence
      label = f"Rondell, avfart {m['rb_exit']}" + (f" ({m['name']})" if m.get("name") else "")
    elif m.get("exit") and str(m["exit"]) not in label:
      label = f"Avfart {m['exit']} {label}"
    t, fs = self._fit(label, self.font, 40, tw)
    rl.draw_text_ex(self.font, t, rl.Vector2(tx, y + 86), fs, 0, WHITE)
    eta = st.get("eta")
    foot = []
    if eta:
      foot.append(datetime.fromtimestamp(eta, _TZ).strftime("%H:%M"))
    if st.get("dist_remaining") is not None:
      foot.append(self._dist(st["dist_remaining"]))
    if len(ms) > 1 and ms[1].get("dist_m") is not None and ms[1]["dist_m"] - (m.get("dist_m") or 0) < 400:
      foot.append("sedan " + ({"left": "vänster", "right": "höger", "slight left": "vänster", "slight right": "höger",
                               "sharp left": "vänster", "sharp right": "höger"}.get(ms[1].get("modifier") or "", "rakt fram")))
    rl.draw_text_ex(self.font, "  ·  ".join(foot), rl.Vector2(tx, y + 132), 32, 0, DIM)
