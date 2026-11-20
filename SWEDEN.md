# PNW Pilot — Sweden edition (`david/3testpnw-se`)

A personal branch of [dirkpetersen/pnw-pilot](https://github.com/dirkpetersen/pnw-pilot) `3testpnw` for a
**Tesla Model S (HW2 retrofitted to HW3, legacy CAN)** driven in Sweden. Dirk's branch is merged in daily by
CI; everything below is added on top and is guarded so it stays inert when unused.

```
commaai/openpilot
  └─ xnor-tech/openpilot          legacy Tesla HW1/2/3 support
       └─ dirkpetersen/pnw-pilot  3testpnw (PNW distribution)
            └─ davidadamjeff/pnw-pilot  david/3testpnw-se   ← this branch
```

## Hardware note (HW2 Model S)

The HW2 platform needs **two pandas**: the comma's internal panda on the chassis bus and an external black
(F4) panda on the powertrain bus (buses 4–7, `FLAG_EXTERNAL_PANDA`). With one panda openpilot shows
`canBusMissing` and the car may report regen/stability/AEB unavailable — do not drive like that.
Wiring used here: xnor box port 1 → comma main, port 2 → comma aux. pandad flashes both pandas
(the F4 from the prebuilt `selfdrive/pandad/fw/panda_f4.bin.signed`).

## Features

| Feature | What it does | Toggle / setting | Code |
|---|---|---|---|
| **Speed cameras (Trafikverket)** | Fixed cameras ahead in the lower-left box; slows to the posted limit ~20 s before a camera. List refreshes monthly on Wi-Fi. | Auto speed reduce ≥ Police | `system/location_services`, `speedadjust_controller._camera_cap` |
| **comma.ai connect backend** | Choose comma.ai (or PNW/Konik/Custom/Offline) for pairing and uploads; prime upsell hidden. | Settings → Network → Connect backend | `common/connect_backend.py`, `system/loggerd/uploader.py` |
| **Waze reports with your own key** | OpenWebNinja free plan (50 lookups/month): a lookup window opens on drives ≥ 30 min or ≥ 80 km/h for 10 min, then one lookup per hour. Police, accidents, hazards and road closures in the **same** lookup. | Police Reports, Accidents, Hazards, Road Closures (Waze) — usage `N/50` shown under Police Reports | `police_budget.py`, `location_servicesd.py` |
| **Navigation (Mapbox)** | Turn-by-turn with live traffic and Swedish instructions, rerouting, ETA; card top-left while driving. | Navigation (Mapbox) — on | `selfdrive/navse/`, `ui/onroad/nav_se_card.py` |
| **Phone page** | `http://<hostname>.local/<key>`: search, route choice with queue delay, home/work/favourites, recents, cancel, rename the comma. | key in `/data/pnw/nav/web_token` | `selfdrive/navse/web.py` |
| **Route speed control** | Reduce-only slowdown before turns (25 km/h), roundabouts (30), U-turns (18); a SET+ dismisses that maneuver; gas overrides. | Slow Down for Route Turns — on (needs Auto speed reduce ≠ Off) | `speedadjust_controller._nav_cap` |
| **Turn hints to the model** | `turnLeft/Right` below 50 km/h only with **your** matching blinker; `keepLeft/Right` at exits/forks only while you nudge the wheel; a confirmed turn blocks a blinker lane change. Mirrors StarPilot. | Route Turn Hints to Model (Experimental) — **off** | `desire_helper.nav_desire_for` |
| **Standby screen** | Clock, Swedish date and week; phone-page link and destination; calibration state with **Nollställ kalibrering**. | — | `ui/widgets/standby_se.py` |
| **Curve slowdown switches** | Turn VTSC off on its own (CES keeps switching), or let it ignore map curves and use only the camera. | Disable Curve Slowdown (VTSC) — off; Use Map Curves for Slowdown — on | `vtsc_controller._read_enabled` |
| **Logs to Home Assistant** | On unmetered Wi-Fi (never the iPhone hotspot) copies qlog and rlog (qcamera optional) of finished segments to HA, newest first, each verified by size + SHA-256. HA's file list is the record of what is there, so nothing goes twice. HA sensors show last upload, files waiting, stored GB and last check-in. | Upload Logs to Home Assistant — on; Include Low-Res Video in Upload — off | `selfdrive/haupload_se/`, HA side `tools/homeassistant/comma_logs` |
| **Upstream watcher** | Daily check of StarPilot, comma, sunnypilot, xnor-dev / xnor opendbc and Dirk's location services; a change behind one of these features opens an issue labelled `upstream-watch`. | `.github/upstream-watch.json` | `.github/workflows/watch-upstreams.yaml` |
| **Dirk sync** | Daily 03:17 UTC merge of `dirk/3testpnw`; pushes when `check_se.py` passes, otherwise opens a `dirk-sync/<sha>` PR. | — | `.github/workflows/sync-dirk.yaml` |

## Device files (outside git, survive updates)

| Path | Content |
|---|---|
| `/data/pnw/location/police_proxy.json` (600) | `{"source":"direct","key":"ak_…","budget":{"monthly_cap":50,…}}` |
| `/data/pnw/location/police_budget_usage.json` | lookups used this month |
| `/data/pnw/location/trafikverket.key`, `speed_cameras_se.json` | camera API key and list |
| `/data/pnw/nav/mapbox.key` (600) | Mapbox token |
| `/data/pnw/nav/web_token` | phone-page key |
| `/data/pnw/nav/destination.json`, `places.json`, `last_pos.json` | active destination, favourites/recents, last position |
| `/data/pnw/hostname` | chosen network name (re-applied at boot by `launch_chffrplus.sh`) |
| `/data/pnw/haupload/config.json` (600) | `{"webhook_id":"…","urls":["http://<ha-ip>:8123","https://<external>"]}` (first that answers wins) |
| `/data/pnw/haupload/state.json`, `status.json` | uploaded files (cache of HA's list), current status |
| `/data/pnw/dm.json` | unlocks the Relaxed driver-monitoring option |

`launch_chffrplus.sh` also redirects TCP 80 → 8088 (iptables-legacy, IPv4) so the phone page needs no port.

### Home Assistant receiver

Copy `tools/homeassistant/comma_logs` to HA's `config/custom_components/`, restart HA, add the **comma logs** integration
(it proposes a random webhook id), and put that id in the comma's `/data/pnw/haupload/config.json`. Files land in
HA's `/media/comma/<route--segment>/`.

## Tests

```bash
export PYTHONPATH=/data/openpilot:/data/pnw/agnos19-compat/site-packages
python3 -m pytest -c /dev/null -o addopts="" selfdrive/navse/tests \
  selfdrive/controls/lib/tests/test_navse_desires.py selfdrive/controls/lib/speedadjust_pnw/tests \
  selfdrive/controls/lib/tests/test_desire_helper.py selfdrive/haupload_se/tests
python3 system/location_services/tests/test_police_budget.py
python3 .github/scripts/check_se.py
```

## Credits

Navigation follows comma's "nav alpha" (`selfdrive/navigation`) and StarPilot's route engine and
driver-confirmed desires (credited there to discountchubbs / sunnypilot). Waze data via OpenWebNinja;
speed cameras from Trafikverket's open API; routing and search by Mapbox. See also [CREDITS.md](CREDITS.md).
