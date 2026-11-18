"""navse: the two Mapbox calls (stdlib only). Directions (traffic-aware) and Search Box forward search."""
import json
import urllib.error
import urllib.parse
import urllib.request

DIRECTIONS = "https://api.mapbox.com/directions/v5/mapbox/driving-traffic/"
SEARCH = "https://api.mapbox.com/search/searchbox/v1/forward"
TIMEOUT_S = 12


class MapboxError(Exception):
  pass


def _get(url, params):
  q = urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})
  req = urllib.request.Request(f"{url}?{q}", headers={"User-Agent": "navse/1.0"})
  try:
    with urllib.request.urlopen(req, timeout=TIMEOUT_S) as resp:
      return json.loads(resp.read())
  except urllib.error.HTTPError as e:
    raise MapboxError(f"HTTP {e.code}") from None
  except (urllib.error.URLError, TimeoutError, OSError) as e:
    raise MapboxError("no internet" if isinstance(e, urllib.error.URLError) else type(e).__name__) from None
  except ValueError:
    raise MapboxError("bad response") from None


def directions(key, origin, dest, bearing=None, alternatives=False, language="sv"):
  """origin/dest = (lat, lon). -> raw Mapbox response with code == "Ok"."""
  coords = f"{origin[1]:.6f},{origin[0]:.6f};{dest[1]:.6f},{dest[0]:.6f}"
  params = {
    "access_token": key, "geometries": "geojson", "overview": "full", "steps": "true",
    "banner_instructions": "true", "annotations": "maxspeed", "language": language,
    "alternatives": "true" if alternatives else "false",
  }
  if bearing is not None:
    params["bearings"] = f"{int(bearing) % 360},45;"
  data = _get(DIRECTIONS + coords, params)
  if not isinstance(data, dict) or data.get("code") != "Ok" or not data.get("routes"):
    raise MapboxError(str((data or {}).get("code") or "no route") if isinstance(data, dict) else "no route")
  return data


def only_route(data, idx):
  """A response reduced to one route (for storing the route the driver picked)."""
  out = dict(data)
  out["routes"] = [data["routes"][idx]]
  return out


def search(key, q, near=None, limit=8, language="sv", country="se,no,dk,fi,de"):
  params = {"q": q[:120], "access_token": key, "limit": limit, "language": language, "country": country}
  if near is not None:
    params["proximity"] = f"{near[1]:.5f},{near[0]:.5f}"
  data = _get(SEARCH, params)
  out = []
  for f in (data or {}).get("features", []) if isinstance(data, dict) else []:
    try:
      p = f.get("properties") or {}
      lon, lat = f["geometry"]["coordinates"][:2]
      out.append({"name": p.get("name") or "", "address": p.get("full_address") or p.get("place_formatted") or "",
                  "lat": float(lat), "lon": float(lon), "kind": p.get("feature_type") or ""})
    except (KeyError, TypeError, ValueError):
      continue
  return out
