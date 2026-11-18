#!/usr/bin/env python3
"""navse: phone page for choosing a destination. http://<comma-ip>:8088/?k=<token>

Stdlib only. The token (store.web_token, shown in Settings -> Toggles -> Navigation) is required on
every request (query ?k= once, then a cookie), so other people on the same Wi-Fi/hotspot cannot set a
destination. Mapbox is only called from here (search + route choices) and from navd (the route itself)."""
import json
import threading
import time
import urllib.parse
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from openpilot.common.params import Params
from openpilot.common.swaglog import cloudlog
from openpilot.selfdrive.navse import mapbox, store
from openpilot.selfdrive.navse.route import Route

PORT = 8088
CHOICES_TTL_S = 15 * 60
_choices = {}                       # id -> (t, origin, response) for the route alternatives shown on the page
_lock = threading.Lock()


def _mem_get(key):
  try:
    v = Params("/dev/shm/params").get(key, return_default=True)
    if isinstance(v, (bytes, str)):
      v = json.loads(v)
    return v
  except Exception:  # noqa: BLE001
    return None


def _position():
  g = _mem_get("LastGPSPosition")
  if isinstance(g, dict) and time.monotonic() - float(g.get("ts") or 0) < 10:
    return float(g["latitude"]), float(g["longitude"]), g.get("bearing"), True
  p = store.read_json(store.POS_FILE)
  if isinstance(p, dict) and "lat" in p:
    return float(p["lat"]), float(p["lon"]), None, False
  return None


def _route_choices(dest):
  key = store.mapbox_key()
  pos = _position()
  if not key:
    raise mapbox.MapboxError("no Mapbox key")
  if pos is None:
    raise mapbox.MapboxError("no position yet")
  data = mapbox.directions(key, pos[:2], dest, bearing=pos[2] if pos[3] else None, alternatives=True)
  cid = uuid.uuid4().hex[:12]
  with _lock:
    now = time.time()
    for k in [k for k, v in _choices.items() if now - v[0] > CHOICES_TTL_S]:
      _choices.pop(k, None)
    _choices[cid] = (now, pos[:2], data)
  out = []
  for i in range(len(data["routes"])):
    r = Route(data, i)
    out.append({"idx": i, "km": round(r.length / 1000, 1), "min": round(r.duration / 60),
                "typical_min": round(r.duration_typical / 60), "via": r.summary})
  return {"id": cid, "routes": out, "live_gps": pos[3]}


class Handler(BaseHTTPRequestHandler):
  server_version = "navse"

  def log_message(self, fmt, *args):
    pass

  # ---- helpers ---------------------------------------------------------------------------------
  def _authed(self):
    tok = store.web_token()
    q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
    if q.get("k", [""])[0] == tok:
      return True, True
    for part in (self.headers.get("Cookie") or "").split(";"):
      k, _, v = part.strip().partition("=")
      if k == "navk" and v == tok:
        return True, False
    return False, False

  def _send(self, code, body, ctype="application/json", cookie=False):
    data = body if isinstance(body, bytes) else (json.dumps(body) if ctype == "application/json" else body).encode()
    self.send_response(code)
    self.send_header("Content-Type", ctype + "; charset=utf-8")
    self.send_header("Content-Length", str(len(data)))
    self.send_header("Cache-Control", "no-store")
    if cookie:
      self.send_header("Set-Cookie", f"navk={store.web_token()}; Max-Age=31536000; Path=/; SameSite=Strict; HttpOnly")
    self.end_headers()
    self.wfile.write(data)

  def _body(self):
    n = min(int(self.headers.get("Content-Length") or 0), 65536)
    try:
      return json.loads(self.rfile.read(n) or b"{}")
    except ValueError:
      return {}

  # ---- routes ----------------------------------------------------------------------------------
  def do_GET(self):
    ok, set_cookie = self._authed()
    url = urllib.parse.urlparse(self.path)
    if not ok:
      return self._send(403, "<h2>Öppna länken från comma: Settings → Toggles → Navigation</h2>", "text/html")
    if url.path == "/":
      return self._send(200, PAGE, "text/html", cookie=set_cookie)
    if url.path == "/api/state":
      return self._send(200, {"nav": _mem_get("NavStateSE"), "dest": store.read_json(store.DEST_FILE, None) and
                              {k: v for k, v in store.read_json(store.DEST_FILE, {}).items() if k != "route"},
                              "places": store.places(), "has_key": bool(store.mapbox_key()),
                              "enabled": Params().get_bool("NavigationSE")})
    if url.path == "/api/search":
      q = urllib.parse.parse_qs(url.query).get("q", [""])[0].strip()
      if len(q) < 2:
        return self._send(200, {"results": []})
      pos = _position()
      try:
        return self._send(200, {"results": mapbox.search(store.mapbox_key(), q, near=pos[:2] if pos else None)})
      except mapbox.MapboxError as e:
        return self._send(502, {"error": str(e)})
    return self._send(404, {"error": "not found"})

  def do_POST(self):
    ok, _ = self._authed()
    if not ok:
      return self._send(403, {"error": "token"})
    path = urllib.parse.urlparse(self.path).path
    b = self._body()
    try:
      if path == "/api/routes":
        return self._send(200, _route_choices((float(b["lat"]), float(b["lon"]))))
      if path == "/api/go":
        place = store.clean_place(b)
        route, frm = None, None
        cid, idx = b.get("choice"), b.get("idx")
        with _lock:
          ch = _choices.get(cid) if cid else None
        if ch is not None and idx is not None and 0 <= int(idx) < len(ch[2]["routes"]):
          route, frm = mapbox.only_route(ch[2], int(idx)), ch[1]
        store.set_destination(place, route, frm)
        cloudlog.event("navse_web_go", name=place.get("name"), prechosen=route is not None)
        return self._send(200, {"ok": True})
      if path == "/api/cancel":
        store.clear_destination()
        return self._send(200, {"ok": True})
      if path == "/api/fav":
        return self._send(200, {"places": store.save_favorite(b, b.get("slot"))})
      if path == "/api/fav/delete":
        return self._send(200, {"places": store.delete_favorite(int(b.get("index", -1)))})
    except mapbox.MapboxError as e:
      return self._send(502, {"error": str(e)})
    except (KeyError, TypeError, ValueError) as e:
      return self._send(400, {"error": type(e).__name__})
    return self._send(404, {"error": "not found"})


PAGE = r"""<!doctype html><html lang="sv"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="apple-mobile-web-app-capable" content="yes"><meta name="theme-color" content="#111">
<title>comma nav</title><style>
:root{--bg:#111;--card:#1d1f22;--fg:#eee;--dim:#9aa;--acc:#3ba55d;--warn:#e0a030;--bad:#e05050}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:16px -apple-system,system-ui,sans-serif;padding:12px 14px 40px}
h1{font-size:20px;margin:4px 0 12px}h2{font-size:14px;color:var(--dim);text-transform:uppercase;letter-spacing:.06em;margin:18px 0 6px}
.card{background:var(--card);border-radius:12px;padding:12px;margin:8px 0}
input{width:100%;font-size:18px;padding:12px;border-radius:10px;border:1px solid #333;background:#000;color:var(--fg)}
button{font-size:16px;padding:10px 14px;border-radius:10px;border:0;background:#2c3035;color:var(--fg);margin:4px 6px 0 0}
button.go{background:var(--acc);color:#000;font-weight:600}button.bad{background:var(--bad);color:#000}
.row{display:flex;justify-content:space-between;align-items:center;gap:8px}.sub{color:var(--dim);font-size:14px}
.item{padding:10px 4px;border-bottom:1px solid #2a2a2a}.item:last-child{border:0}.big{font-size:22px;font-weight:600}
.err{color:var(--bad)}.ok{color:var(--acc)}
</style></head><body>
<h1>🧭 Navigering</h1>
<div id="status" class="card">…</div>
<input id="q" placeholder="Sök adress eller plats" autocomplete="off" enterkeyhint="search">
<div id="results"></div><div id="choice"></div>
<h2>Favoriter</h2><div id="favs" class="card"></div>
<h2>Senaste</h2><div id="recents" class="card"></div>
<script>
const $=s=>document.querySelector(s);let picked=null,places={favorites:[],recents:[]},C=[];const reg=o=>(C.push(o),C.length-1);
const esc=s=>String(s??"").replace(/[&<>"]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
async function api(p,b){const r=await fetch(p,b?{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(b)}:{});
 const j=await r.json().catch(()=>({error:"HTTP "+r.status}));if(!r.ok)throw new Error(j.error||r.status);return j}
function fmtDist(m){return m>=1000?(m/1000).toFixed(m>=10000?0:1)+" km":Math.round(m/10)*10+" m"}
function fmtTime(s){s=Math.round(s/60);return s>=60?Math.floor(s/60)+" h "+(s%60)+" min":s+" min"}
async function refresh(){try{const j=await api("/api/state");places=j.places;drawPlaces();
 const n=j.nav||{},d=j.dest;let h="";
 if(!j.has_key)h+='<div class="err">Ingen Mapbox-nyckel på enheten.</div>';
 if(!j.enabled)h+='<div class="err">Navigation är avstängd i Settings → Toggles.</div>';
 if(!d){h+='<div class="sub">Ingen destination.</div>'}else{
  h+=`<div class="big">${esc(d.name||d.address)}</div><div class="sub">${esc(d.address||"")}</div>`;
  const st={active:"Navigerar",routing:"Hämtar rutt…",rerouting:"Räknar om…",waiting_gps:"Väntar på GPS (starta bilen)",arrived:"Framme!",error:"Fel",off:"Avstängd",idle:""}[n.state]||n.state||"";
  h+=`<div class="${n.state==="error"?"err":"ok"}">${esc(st)} ${esc(n.err||"")}</div>`;
  if(n.dist_remaining!=null)h+=`<div>${fmtDist(n.dist_remaining)} · ${fmtTime(n.time_remaining)} · framme ${new Date(n.eta*1000).toLocaleTimeString("sv",{hour:"2-digit",minute:"2-digit"})}</div>`;
  const m=(n.maneuvers||[])[0];if(m)h+=`<div class="sub">Nästa: ${esc(m.instruction||m.text)} om ${fmtDist(m.dist_m)}</div>`;
  h+='<button class="bad" onclick="cancelNav()">Avbryt</button>'}
 $("#status").innerHTML=h}catch(e){$("#status").innerHTML='<span class="err">'+esc(e.message)+'</span>'}}
function placeRow(p,i,kind){const lbl=p.slot==="home"?"🏠 Hem":p.slot==="work"?"💼 Jobb":esc(p.name||p.address);
 return `<div class="item row"><div onclick="choose(C[${reg(p)}])" style="flex:1"><div>${lbl}</div><div class="sub">${esc(p.address)}</div></div>`+
 (kind==="fav"?`<button onclick="delFav(${i})">✕</button>`:"")+`</div>`}
function drawPlaces(){$("#favs").innerHTML=places.favorites.map((p,i)=>placeRow(p,i,"fav")).join("")||'<div class="sub">Välj en plats och tryck ★ för att spara.</div>';
 $("#recents").innerHTML=places.recents.map((p,i)=>placeRow(p,i,"rec")).join("")||'<div class="sub">–</div>'}
let t=null;$("#q").addEventListener("input",e=>{clearTimeout(t);t=setTimeout(()=>search(e.target.value),350)});
async function search(q){if(q.trim().length<2){$("#results").innerHTML="";return}
 try{const j=await api("/api/search?q="+encodeURIComponent(q));
  $("#results").innerHTML='<div class="card">'+(j.results.map(r=>`<div class="item" onclick="choose(C[${reg(r)}])"><div>${esc(r.name)}</div><div class="sub">${esc(r.address)}</div></div>`).join("")||'<div class="sub">Inga träffar</div>')+'</div>'}
 catch(e){$("#results").innerHTML='<div class="err">'+esc(e.message)+'</div>'}}
async function choose(p){picked=p;$("#results").innerHTML="";$("#choice").innerHTML='<div class="card">Hämtar rutter…</div>';
 let h=`<div class="card"><div class="big">${esc(p.name||p.address)}</div><div class="sub">${esc(p.address||"")}</div>`;
 try{const j=await api("/api/routes",{lat:p.lat,lon:p.lon});
  j.routes.forEach(r=>{const late=r.min-r.typical_min;h+=`<div class="item row"><div><b>${fmtTime(r.min*60)}</b> · ${r.km} km`+
   (late>=3?` <span style="color:var(--warn)">+${late} min kö</span>`:"")+`<div class="sub">via ${esc(r.via)}</div></div><button class="go" onclick="go(C[${reg(p)}],'${esc(j.id)}',${Number(r.idx)})">Kör</button></div>`});
  if(!j.live_gps)h+='<div class="sub">Startpunkt = senaste kända position.</div>'}
 catch(e){h+=`<div class="err">${esc(e.message)}</div><button class="go" onclick="go(C[${reg(p)}],null,null)">Kör ändå</button>`}
 h+=`<div><button onclick="fav('home')">🏠 Spara som Hem</button><button onclick="fav('work')">💼 Jobb</button><button onclick="fav('fav')">★ Favorit</button></div></div>`;
 $("#choice").innerHTML=h}
async function go(p,cid,idx){await api("/api/go",{...p,choice:cid,idx});$("#choice").innerHTML="";$("#q").value="";refresh()}
async function cancelNav(){await api("/api/cancel",{});refresh()}
async function fav(slot){const j=await api("/api/fav",{...picked,slot});places=j.places;drawPlaces()}
async function delFav(i){const j=await api("/api/fav/delete",{index:i});places=j.places;drawPlaces()}
refresh();setInterval(refresh,3000);
</script></body></html>"""


def main():
  store.web_token()
  srv = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
  srv.daemon_threads = True
  cloudlog.info("navse web on :%d", PORT)
  srv.serve_forever()


if __name__ == "__main__":
  main()
