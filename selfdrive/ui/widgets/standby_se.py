"""standby-se: the offroad home screen without the comma upsells.

Left column (instead of the empty "prime" slot): a big clock, Swedish weekday + date and the ISO week.
Right column (instead of the Firehose card): the phone navigation page address (+ active destination) and the
calibration state with a one-tap reset -- the device is moved between cars often, so the reset lives here and
not three screens deep. The reset does exactly what Settings -> Device -> Reset Calibration does."""
import math
import socket
import time
from datetime import datetime

import pyray as rl

from cereal import log, messaging
from openpilot.common.params import Params
from openpilot.common.swaglog import cloudlog
from openpilot.common.time_helpers import system_time_valid
from openpilot.selfdrive.ui.ui_state import ui_state
from openpilot.system.ui.lib.application import gui_app, FontWeight
from openpilot.system.ui.lib.text_measure import measure_text_cached
from openpilot.system.ui.widgets import Widget, DialogResult
from openpilot.system.ui.widgets.button import Button, ButtonStyle
from openpilot.system.ui.widgets.confirm_dialog import ConfirmDialog, alert_dialog

try:
  from zoneinfo import ZoneInfo
  _TZ = ZoneInfo("Europe/Stockholm")      # the device clock is UTC
except Exception:
  _TZ = None

WEEKDAYS = ("måndag", "tisdag", "onsdag", "torsdag", "fredag", "lördag", "söndag")
MONTHS = ("januari", "februari", "mars", "april", "maj", "juni", "juli", "augusti", "september", "oktober",
          "november", "december")

CARD = rl.Color(40, 42, 46, 255)
WHITE = rl.Color(255, 255, 255, 255)
DIM = rl.Color(160, 166, 172, 255)
GREEN = rl.Color(110, 215, 140, 255)
AMBER = rl.Color(240, 190, 70, 255)
CALIB_KEYS = ("CalibrationParams", "LiveTorqueParameters", "LiveParameters", "LiveParametersV2", "LiveDelay")


def _fonts():
  # The prebaked .fnt atlases have no "å" (måndag, söndag...): use the bundled Inter TTFs like the nav card.
  from openpilot.selfdrive.ui.onroad.nav_se_card import _load_ttf
  return (_load_ttf("Inter-Bold.ttf", gui_app.font(FontWeight.BOLD)),
          _load_ttf("Inter-Medium.ttf", gui_app.font(FontWeight.MEDIUM)))


_FONT_CACHE = {}


def fonts():
  if not _FONT_CACHE:
    _FONT_CACHE["b"], _FONT_CACHE["m"] = _fonts()
  return _FONT_CACHE["b"], _FONT_CACHE["m"]


def _text(font, s, x, y, size, color):
  rl.draw_text_ex(font, s, rl.Vector2(int(x), int(y)), size, 0, color)


def _fit(font, s, size, max_w, min_size=28):
  while size > min_size and measure_text_cached(font, s, size).x > max_w:
    size -= 2
  return size


class StandbyClock(Widget):
  def _render(self, rect):
    bold, med = fonts()
    rl.draw_rectangle_rounded(rect, 0.03, 20, CARD)
    now = datetime.now(_TZ) if _TZ else datetime.now()
    valid = system_time_valid()
    hhmm = now.strftime("%H:%M") if valid else "--:--"
    x = rect.x + 70
    y = rect.y + rect.height * 0.16
    size = _fit(bold, "00:00", 300, rect.width - 140)
    _text(bold, hhmm, x, y, size, WHITE)
    y += size * 1.05
    if valid:
      date = f"{WEEKDAYS[now.weekday()].capitalize()} {now.day} {MONTHS[now.month - 1]}"
      ds = _fit(med, date, 76, rect.width - 140)
      _text(med, date, x, y, ds, WHITE)
      y += ds * 1.35
      iso = now.isocalendar()
      _text(med, f"Vecka {iso[1]}  ·  {now.year}", x, y, 56, DIM)
    else:
      _text(med, "Klockan är inte synkad ännu", x, y, 52, DIM)


class StandbyInfo(Widget):
  def __init__(self):
    super().__init__()
    self.params = Params()
    self._reset_btn = Button(lambda: "Nollställ kalibrering", self._reset_prompt, button_style=ButtonStyle.NORMAL)
    self._cancel_btn = Button(lambda: "Avbryt", self._cancel_prompt, button_style=ButtonStyle.NORMAL)  # navcancel-se
    self._last = 0.0
    self._link = ""
    self._dest = ""
    self._calib = ""
    self._calib_color = DIM

  # ---- data (refreshed every 3 s; the screen redraws far more often) ------------------------------
  def _refresh(self):
    now = time.monotonic()
    if now - self._last < 3.0:
      return
    self._last = now
    try:
      from openpilot.selfdrive.navse import store
      host = socket.gethostname()
      self._link = f"{host}.local/{store.web_token()}"
      d = store.read_json(store.DEST_FILE) or {}
      self._dest = d.get("name") or d.get("address") or ""
    except Exception:
      self._link, self._dest = "", ""
    self._calib, self._calib_color = self._calib_status()

  def _calib_status(self):
    b = self.params.get("CalibrationParams")
    if not b:
      return "Inte kalibrerad: lärs in när du kör rakt fram en stund.", AMBER
    try:
      c = messaging.log_from_bytes(b, log.Event).liveCalibration
      if c.calStatus == log.LiveCalibrationData.Status.uncalibrated:
        return f"Kalibrerar... {c.calPerc}%", AMBER
      pitch, yaw = math.degrees(c.rpyCalib[1]), math.degrees(c.rpyCalib[2])
      return (f"Kalibrerad: {abs(pitch):.1f}° {'ner' if pitch > 0 else 'upp'}, " +
              f"{abs(yaw):.1f}° {'vänster' if yaw > 0 else 'höger'}"), GREEN
    except Exception:
      cloudlog.exception("standby-se: invalid CalibrationParams")
      return "Kalibrering: okänd", DIM

  def _reset_prompt(self):
    if ui_state.engaged:
      gui_app.push_widget(alert_dialog("Koppla ur openpilot först"))
      return

    def done(result):
      if ui_state.engaged or result != DialogResult.CONFIRM:
        return
      for k in CALIB_KEYS:
        self.params.remove(k)
      self.params.put_bool("OnroadCycleRequested", True)
      cloudlog.event("standby_se_reset_calibration")
      self._last = 0.0

    gui_app.push_widget(ConfirmDialog("Nollställa kalibreringen? Den lärs in igen de första minuterna du kör.",
                                      "Nollställ", cancel_text="Avbryt", callback=done))

  def _cancel_prompt(self):   # navcancel-se
    def done(result):
      if result != DialogResult.CONFIRM:
        return
      try:
        from openpilot.selfdrive.navse import store
        store.clear_destination()
        cloudlog.event("standby_se_cancel_nav")
      except Exception:
        cloudlog.exception("standby-se: cancel navigation failed")
      self._last = 0.0

    gui_app.push_widget(ConfirmDialog(f"Avbryta navigeringen till {self._dest}?", "Avbryt navigering",
                                      cancel_text="Behåll", callback=done))

  # ---- render ----------------------------------------------------------------------------------
  def _render(self, rect):
    self._refresh()
    bold, med = fonts()
    gap = 25
    nav_h = 355
    nav = rl.Rectangle(rect.x, rect.y, rect.width, nav_h)
    rl.draw_rectangle_rounded(nav, 0.06, 20, CARD)
    x, w = nav.x + 50, nav.width - 100
    _text(med, "NAVIGERING", x, nav.y + 34, 36, DIM)
    if self._link:
      _text(med, "Öppna på telefonen:", x, nav.y + 90, 40, WHITE)
      ls = _fit(bold, self._link, 60, w)
      _text(bold, self._link, x, nav.y + 145, ls, GREEN)
    if self._dest:
      d = f"Mål: {self._dest}"
      bw = 300
      _text(med, d, x, nav.y + 258, _fit(med, d, 40, w - bw - 30), WHITE)
      self._cancel_btn.render(rl.Rectangle(x + w - bw, nav.y + 225, bw, 105))
    else:
      _text(med, "Inget mål valt", x, nav.y + 258, 40, DIM)

    cal = rl.Rectangle(rect.x, rect.y + nav_h + gap, rect.width, max(260.0, rect.height - nav_h - gap))
    rl.draw_rectangle_rounded(cal, 0.06, 20, CARD)
    x, w = cal.x + 50, cal.width - 100
    _text(med, "KALIBRERING", x, cal.y + 34, 36, DIM)
    cs = _fit(med, self._calib, 40, w, 30)
    _text(med, self._calib, x, cal.y + 90, cs, self._calib_color)
    self._reset_btn.render(rl.Rectangle(x, cal.y + cal.height - 40 - 150, w, 150))
