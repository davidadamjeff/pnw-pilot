"""navse: pure route model for a Mapbox Directions response (no I/O, no openpilot imports).

A route is flattened into one polyline with cumulative distance `s` (m). Every Mapbox step starts with
its maneuver, so maneuver j sits at s = cum[start index of step j]. The tracker projects GPS fixes onto
the polyline near the last known `s`, dead-reckons between fixes, and flags off-route after a few
consecutive bad fixes. Everything here is unit-testable on a desktop with a saved response."""
import bisect
import math

R_EARTH = 6371000.0

OFF_ROUTE_M = 40.0              # perpendicular distance that counts as off the route
OFF_ROUTE_BEARING_DEG = 110.0   # driving against the route direction (only judged when moving)
OFF_ROUTE_FIXES = 3             # consecutive bad fixes before "off route" (1 Hz GPS -> ~3 s)
SEARCH_BACK_M = 150.0           # projection window behind / ahead of the last position
SEARCH_AHEAD_M = 1500.0
DEAD_RECKON_MAX_S = 2.0         # never extrapolate more than this past the last fix
ARRIVE_M = 30.0

KMH = 1 / 3.6

TURN_MODIFIERS_LEFT = ("left", "sharp left")
TURN_MODIFIERS_RIGHT = ("right", "sharp right")
SLIGHT_LEFT = "slight left"
SLIGHT_RIGHT = "slight right"
TURN_TYPES = ("turn", "end of road")
ROUNDABOUT_TYPES = ("roundabout", "rotary", "roundabout turn")
KEEP_TYPES = ("off ramp", "fork")


def haversine_m(lat1, lon1, lat2, lon2):
  p1, p2 = math.radians(lat1), math.radians(lat2)
  dp, dl = p2 - p1, math.radians(lon2 - lon1)
  a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
  return 2 * R_EARTH * math.asin(min(1.0, math.sqrt(a)))


def bearing_deg(lat1, lon1, lat2, lon2):
  p1, p2 = math.radians(lat1), math.radians(lat2)
  dl = math.radians(lon2 - lon1)
  x = math.sin(dl) * math.cos(p2)
  y = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl)
  return (math.degrees(math.atan2(x, y)) + 360.0) % 360.0


def angle_diff(a, b):
  return abs((a - b + 180.0) % 360.0 - 180.0)


def _xy(lat0, lon0, lat, lon):
  return (math.radians(lon - lon0) * R_EARTH * math.cos(math.radians(lat0)),
          math.radians(lat - lat0) * R_EARTH)


def maneuver_target_speed(mtype, modifier):
  """Route speed control: comfortable speed (m/s) AT the maneuver point, or None = no slowdown.
  Off-ramps and forks are deliberately None: slowing in the motorway lane before an exit is wrong;
  the ramp's own curve (vision turn speed) and its end-of-road / roundabout maneuver handle it."""
  t = (mtype or "").lower()
  m = (modifier or "").lower()
  if m == "uturn":
    return 18 * KMH              # >= speedadjust MIN_CAP (10 mph)
  if t in ROUNDABOUT_TYPES:
    return 30 * KMH
  if t in TURN_TYPES:
    if m in TURN_MODIFIERS_LEFT + TURN_MODIFIERS_RIGHT:
      return 25 * KMH
    if m in (SLIGHT_LEFT, SLIGHT_RIGHT):
      return 45 * KMH
  return None


class Maneuver:
  __slots__ = ("idx", "s", "type", "modifier", "text", "secondary", "instruction", "name", "lanes",
               "target_ms", "exit", "rb_exit")

  def __init__(self, **kw):
    for k in self.__slots__:
      setattr(self, k, kw.get(k))

  def as_dict(self, dist_m):
    return {"idx": self.idx, "dist_m": round(dist_m, 1), "type": self.type, "modifier": self.modifier,
            "text": self.text, "secondary": self.secondary, "instruction": self.instruction,
            "name": self.name, "lanes": self.lanes, "target_ms": self.target_ms, "exit": self.exit,
            "rb_exit": self.rb_exit}


def _lanes_from_banner(banner):
  """[{"dirs": [...], "active": bool}] from a banner's `sub` lane components (Mapbox lane guidance)."""
  out = []
  for src in (banner.get("sub"), banner.get("primary")):
    if not isinstance(src, dict):
      continue
    for c in src.get("components") or []:
      if isinstance(c, dict) and c.get("type") == "lane":
        out.append({"dirs": list(c.get("directions") or []), "active": bool(c.get("active")),
                    "active_dir": c.get("active_direction")})
    if out:
      break
  return out


class Route:
  """One Mapbox route (all legs concatenated)."""

  def __init__(self, data, route_index=0):
    r = data["routes"][route_index]
    self.distance = float(r.get("distance") or 0.0)
    self.duration = float(r.get("duration") or 0.0)
    self.duration_typical = float(r.get("duration_typical") or self.duration)
    self.summary = ", ".join(x for x in (leg.get("summary") for leg in r.get("legs", [])) if x)
    self.pts = []                 # [(lat, lon)]
    self.maxspeed = []            # per segment, m/s or None (only if the annotation lines up)
    steps = []
    for leg in r.get("legs", []):
      ann = (leg.get("annotation") or {}).get("maxspeed") or []
      leg_start = len(self.pts)
      for st in leg.get("steps", []):
        coords = [(float(c[1]), float(c[0])) for c in (st.get("geometry") or {}).get("coordinates", [])]
        start = len(self.pts)
        if self.pts and coords and coords[0] == self.pts[-1]:
          start -= 1
          coords = coords[1:]
        self.pts.extend(coords)
        steps.append((max(0, start), st))
      nseg = len(self.pts) - leg_start - (1 if leg_start == 0 else 0)
      if ann and abs(len(ann) - nseg) <= 3:      # Mapbox counts the duplicate arrive point differently
        vals = [_maxspeed_ms(a) for a in ann[:nseg]]
        self.maxspeed.extend(vals + [vals[-1] if vals else None] * (nseg - len(vals)))
      else:
        self.maxspeed.extend([None] * max(0, nseg))
    if len(self.pts) < 2:
      raise ValueError("route has no geometry")
    self.cum = [0.0]
    for (a, b) in zip(self.pts, self.pts[1:], strict=False):
      self.cum.append(self.cum[-1] + haversine_m(a[0], a[1], b[0], b[1]))
    self.length = self.cum[-1]
    self.seg_bearing = [bearing_deg(a[0], a[1], b[0], b[1]) for a, b in zip(self.pts, self.pts[1:], strict=False)]
    # step durations for time-remaining: (s_start, s_end, duration)
    self.step_spans = []
    self.maneuvers = []
    for j, (start, st) in enumerate(steps):
      s0 = self.cum[min(start, len(self.cum) - 1)]
      s1 = self.cum[steps[j + 1][0]] if j + 1 < len(steps) else self.length
      self.step_spans.append((s0, s1, float(st.get("duration") or 0.0)))
      if j == 0:
        continue                       # the depart maneuver is behind us by definition
      m = st.get("maneuver") or {}
      banner = {}
      prev_banners = steps[j - 1][1].get("bannerInstructions") or []
      if prev_banners:
        banner = prev_banners[-1]       # the last banner of the previous step = closest to this maneuver
      prim = banner.get("primary") or {}
      sec = banner.get("secondary") or {}
      exit_no = None
      for c in prim.get("components") or []:
        if isinstance(c, dict) and c.get("type") == "exit-number":
          exit_no = c.get("text")
      mtype = m.get("type") or prim.get("type") or ""
      mod = m.get("modifier") or prim.get("modifier") or ""
      self.maneuvers.append(Maneuver(
        idx=j, s=s0, type=mtype, modifier=mod, text=prim.get("text") or st.get("name") or "",
        secondary=sec.get("text") or "", instruction=m.get("instruction") or "", name=st.get("name") or "",
        lanes=_lanes_from_banner(banner), target_ms=maneuver_target_speed(mtype, mod), exit=exit_no,
        rb_exit=m.get("exit") if mtype in ROUNDABOUT_TYPES else None))
    self.dest = self.pts[-1]
    self._man_s = [mm.s for mm in self.maneuvers]

  # ---- geometry --------------------------------------------------------------------------------
  def locate(self, lat, lon, bearing=None, speed=0.0, hint_s=None):
    """Project (lat, lon) onto the route. -> (s, perp_m, seg_bearing) or None (empty window)."""
    n = len(self.pts) - 1
    if hint_s is None:
      i0, i1 = 0, n
    else:
      i0 = max(0, bisect.bisect_right(self.cum, hint_s - SEARCH_BACK_M) - 1)
      i1 = min(n, bisect.bisect_right(self.cum, hint_s + SEARCH_AHEAD_M) + 1)
    best = None
    for i in range(i0, i1):
      a, b = self.pts[i], self.pts[i + 1]
      ax, ay = _xy(lat, lon, a[0], a[1])
      bx, by = _xy(lat, lon, b[0], b[1])
      dx, dy = bx - ax, by - ay
      L2 = dx * dx + dy * dy
      t = 0.0 if L2 <= 1e-9 else max(0.0, min(1.0, -(ax * dx + ay * dy) / L2))
      px, py = ax + t * dx, ay + t * dy
      perp = math.hypot(px, py)
      cost = perp
      if bearing is not None and speed > 3.0 and angle_diff(bearing, self.seg_bearing[i]) > 90.0:
        cost += 50.0                 # prefer segments driven in our direction (parallel carriageways)
      if best is None or cost < best[0]:
        best = (cost, self.cum[i] + t * (self.cum[i + 1] - self.cum[i]), perp, self.seg_bearing[i])
    if best is None:
      return None
    return best[1], best[2], best[3]

  def speed_limit_at(self, s):
    i = max(0, min(len(self.maxspeed) - 1, bisect.bisect_right(self.cum, s) - 1))
    return self.maxspeed[i] if self.maxspeed else None

  def next_maneuvers(self, s, max_dist=3000.0, max_n=4):
    j = bisect.bisect_right(self._man_s, s + 1.0)
    out = []
    for m in self.maneuvers[j:j + max_n]:
      d = m.s - s
      if d > max_dist and out:
        break
      out.append((m, d))
    return out

  def time_remaining(self, s):
    t = 0.0
    for s0, s1, dur in self.step_spans:
      if s1 <= s:
        continue
      if s0 >= s:
        t += dur
      elif s1 > s0:
        t += dur * (s1 - s) / (s1 - s0)
    return t


def _maxspeed_ms(a):
  if not isinstance(a, dict) or a.get("unknown") or a.get("none") or a.get("speed") is None:
    return None
  try:
    v = float(a["speed"])
  except (TypeError, ValueError):
    return None
  return v * (0.44704 if a.get("unit") == "mph" else KMH)


class Tracker:
  """Position along one Route. update_fix() on every NEW GPS fix, advance() every loop tick."""

  def __init__(self, route):
    self.route = route
    self.s = None
    self.perp = 0.0
    self.bad_fixes = 0
    self.last_fix_t = None
    self.last_tick_t = None
    self.speed = 0.0

  @property
  def off_route(self):
    return self.bad_fixes >= OFF_ROUTE_FIXES

  def update_fix(self, now, lat, lon, bearing, speed):
    self.speed = max(0.0, float(speed or 0.0))
    loc = self.route.locate(lat, lon, bearing, self.speed, hint_s=self.s)
    if loc is None and self.s is not None:
      loc = self.route.locate(lat, lon, bearing, self.speed)
    if loc is None:
      self.bad_fixes += 1
      return
    s, perp, seg_brg = loc
    bad = perp > OFF_ROUTE_M or (bearing is not None and self.speed > 5.0 and angle_diff(bearing, seg_brg) > OFF_ROUTE_BEARING_DEG)
    self.bad_fixes = self.bad_fixes + 1 if bad else 0
    self.perp = perp
    self.s = s
    self.last_fix_t = now
    self.last_tick_t = now

  def advance(self, now, speed=None):
    """Dead-reckon along the route between fixes (speed from the car when available)."""
    if self.s is None or self.last_tick_t is None:
      return
    if speed is not None:
      self.speed = max(0.0, float(speed))
    dt = now - self.last_tick_t
    self.last_tick_t = now
    if dt <= 0 or self.last_fix_t is None or now - self.last_fix_t > DEAD_RECKON_MAX_S:
      return
    self.s = min(self.route.length, self.s + self.speed * dt)

  def arrived(self):
    return self.s is not None and self.s >= self.route.length - ARRIVE_M
