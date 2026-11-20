"""comma logs: receive drive logs from a comma device over a webhook.

GET  -> manifest of stored files {"route--seg/file": size}
PUT  ?path=route--seg/file, headers X-Size + X-Sha256 -> store the file (streamed, verified, atomic)
POST JSON status from the device (pending files/bytes, network, version)
Files land in /media/comma/<route--seg>/.
"""
from __future__ import annotations

import hashlib
import logging
import os

from aiohttp import web

from homeassistant.components import webhook
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .const import DOMAIN, MAX_FILE, PATH_RE, RECENT_MAX, ROOT, SIGNAL_UPDATE

_LOGGER = logging.getLogger(__name__)
PLATFORMS = [Platform.SENSOR]
STATUS_KEYS = {"pending_files": int, "pending_bytes": int, "network": str, "version": str,
               "uploaded_session": int, "state": str, "url": str, "error": str}


def _scan(root: str) -> dict[str, int]:
    out: dict[str, int] = {}
    if not os.path.isdir(root):
        return out
    for seg in os.listdir(root):
        d = os.path.join(root, seg)
        if not os.path.isdir(d):
            continue
        for name in os.listdir(d):
            rel = f"{seg}/{name}"
            if PATH_RE.match(rel):
                try:
                    out[rel] = os.path.getsize(os.path.join(d, name))
                except OSError:
                    pass
    return out


class Hub:
    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.hass = hass
        self.entry = entry
        self.store = Store(hass, 1, f"{DOMAIN}.{entry.entry_id}")
        self.data: dict = {"recent": [], "last_upload": None, "device": {}, "device_seen": None}
        self.files = 0
        self.bytes = 0

    async def async_load(self) -> None:
        saved = await self.store.async_load()
        if saved:
            self.data.update(saved)
        manifest = await self.hass.async_add_executor_job(_scan, ROOT)
        self.files, self.bytes = len(manifest), sum(manifest.values())

    def _changed(self) -> None:
        self.store.async_delay_save(lambda: self.data, 10)
        async_dispatcher_send(self.hass, SIGNAL_UPDATE)

    async def handle(self, hass: HomeAssistant, webhook_id: str, request: web.Request) -> web.Response:
        if request.method == "GET":
            manifest = await hass.async_add_executor_job(_scan, ROOT)
            return web.json_response({"files": manifest})
        if request.method == "POST":
            try:
                body = await request.json()
            except ValueError:
                return web.json_response({"error": "bad json"}, status=400)
            dev = {}
            for k, typ in STATUS_KEYS.items():
                if k in body:
                    try:
                        dev[k] = typ(body[k])[:200] if typ is str else typ(body[k])
                    except (TypeError, ValueError):
                        pass
            self.data["device"] = dev
            self.data["device_seen"] = dt_util.utcnow().isoformat()
            self._changed()
            return web.json_response({"ok": True})
        return await self._put(request)

    async def _put(self, request: web.Request) -> web.Response:
        rel = request.query.get("path", "")
        if not PATH_RE.match(rel):
            return web.json_response({"error": "bad path"}, status=400)
        try:
            size = int(request.headers.get("X-Size", "-1"))
        except ValueError:
            size = -1
        sha = request.headers.get("X-Sha256", "").lower()
        if size < 0 or size > MAX_FILE or len(sha) != 64:
            return web.json_response({"error": "bad size/sha"}, status=400)
        dest = os.path.join(ROOT, rel)
        tmp = dest + ".part"
        await self.hass.async_add_executor_job(lambda: os.makedirs(os.path.dirname(dest), exist_ok=True))
        h = hashlib.sha256()
        n = 0
        f = await self.hass.async_add_executor_job(open, tmp, "wb")
        try:
            async for chunk in request.content.iter_chunked(1 << 20):
                n += len(chunk)
                if n > size:
                    break
                h.update(chunk)
                await self.hass.async_add_executor_job(f.write, chunk)
        finally:
            await self.hass.async_add_executor_job(f.close)
        if n != size or h.hexdigest() != sha:
            await self.hass.async_add_executor_job(os.remove, tmp)
            _LOGGER.warning("comma upload %s rejected: got %d of %d bytes or sha mismatch", rel, n, size)
            return web.json_response({"error": "size/sha mismatch", "got": n}, status=422)
        existed = await self.hass.async_add_executor_job(os.path.exists, dest)
        await self.hass.async_add_executor_job(os.replace, tmp, dest)
        if not existed:
            self.files += 1
            self.bytes += size
        now = dt_util.utcnow().isoformat()
        seg, name = rel.split("/")
        self.data["last_upload"] = now
        self.data["recent"] = ([{"time": now, "segment": seg, "file": name, "size": size}] + self.data["recent"])[:RECENT_MAX]
        self._changed()
        return web.json_response({"ok": True, "path": rel, "size": size})


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    hub = Hub(hass, entry)
    await hub.async_load()
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = hub
    webhook.async_register(hass, DOMAIN, "comma logs", entry.data["webhook_id"], hub.handle,
                           local_only=False, allowed_methods=["GET", "POST", "PUT"])
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    webhook.async_unregister(hass, entry.data["webhook_id"])
    ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if ok:
        hass.data[DOMAIN].pop(entry.entry_id)
    return ok
