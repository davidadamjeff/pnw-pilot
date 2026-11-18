"""navse: on-device files (outside git, survive updates). All reads are defensive; writes are atomic."""
import json
import os
import secrets
import time

NAV_DIR = os.environ.get("NAVSE_DIR", "/data/pnw/nav")
KEY_FILE = "mapbox.key"            # Mapbox access token (mode 600)
DEST_FILE = "destination.json"     # active destination (+ optional pre-chosen route)
PLACES_FILE = "places.json"        # {"favorites": [...], "recents": [...]}
POS_FILE = "last_pos.json"         # last known position (search proximity while parked, GPS is off offroad)
TOKEN_FILE = "web_token"           # secret for the phone page
MAX_RECENTS = 10


def path(name):
  return os.path.join(NAV_DIR, name)


def read_json(name, default=None):
  try:
    with open(path(name)) as f:
      return json.load(f)
  except (OSError, ValueError):
    return default


def write_json(name, obj, mode=0o644):
  os.makedirs(NAV_DIR, exist_ok=True)
  tmp = path(name) + ".tmp"
  with open(tmp, "w") as f:
    json.dump(obj, f)
    f.flush()
    os.fsync(f.fileno())
  os.chmod(tmp, mode)
  os.replace(tmp, path(name))


def remove(name):
  try:
    os.remove(path(name))
  except OSError:
    pass


def mtime(name):
  try:
    return os.stat(path(name)).st_mtime
  except OSError:
    return None


def mapbox_key():
  env = os.environ.get("MAPBOX_TOKEN")
  if env:
    return env.strip()
  try:
    with open(path(KEY_FILE)) as f:
      return f.read().strip() or None
  except OSError:
    return None


def web_token():
  try:
    with open(path(TOKEN_FILE)) as f:
      t = f.read().strip()
      if len(t) >= 4:            # the owner may choose a short one (he did: home Wi-Fi / own hotspot only)
        return t
  except OSError:
    pass
  t = secrets.token_urlsafe(6).replace("-", "x").replace("_", "y")
  os.makedirs(NAV_DIR, exist_ok=True)
  with open(path(TOKEN_FILE), "w") as f:
    f.write(t)
  os.chmod(path(TOKEN_FILE), 0o600)
  return t


def places():
  p = read_json(PLACES_FILE, {}) or {}
  favs = [x for x in p.get("favorites", []) if _valid_place(x)]
  recents = [x for x in p.get("recents", []) if _valid_place(x)]
  return {"favorites": favs, "recents": recents}


def _valid_place(x):
  try:
    return isinstance(x, dict) and -90 <= float(x["lat"]) <= 90 and -180 <= float(x["lon"]) <= 180
  except (KeyError, TypeError, ValueError):
    return False


def clean_place(x, slot=None):
  out = {"lat": round(float(x["lat"]), 6), "lon": round(float(x["lon"]), 6),
         "name": str(x.get("name") or "")[:80], "address": str(x.get("address") or "")[:160]}
  if slot or x.get("slot"):
    out["slot"] = str(slot or x.get("slot"))[:20]
  return out


def save_favorite(place, slot):
  p = places()
  slot = (slot or "fav").lower()
  pl = clean_place(place, slot)
  if slot in ("home", "work"):
    p["favorites"] = [f for f in p["favorites"] if f.get("slot") != slot]
  p["favorites"].append(pl)
  write_json(PLACES_FILE, p)
  return p


def delete_favorite(index):
  p = places()
  if 0 <= index < len(p["favorites"]):
    p["favorites"].pop(index)
    write_json(PLACES_FILE, p)
  return p


def add_recent(place):
  p = places()
  pl = clean_place(place)
  p["recents"] = [pl] + [r for r in p["recents"] if (round(r["lat"], 4), round(r["lon"], 4)) != (round(pl["lat"], 4), round(pl["lon"], 4))]
  p["recents"] = p["recents"][:MAX_RECENTS]
  write_json(PLACES_FILE, p)


def set_destination(place, route=None, route_from=None):
  d = clean_place(place)
  d["ts"] = time.time()
  if route is not None:
    d["route"] = route                # one Mapbox response, reduced to the chosen route
    d["route_from"] = route_from
  write_json(DEST_FILE, d)
  add_recent(place)
  return d


def clear_destination():
  remove(DEST_FILE)
