"""Sensors for comma logs."""
from __future__ import annotations

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity, SensorStateClass
from homeassistant.const import UnitOfInformation
from homeassistant.core import callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.util import dt as dt_util

from .const import DOMAIN, SIGNAL_UPDATE


async def async_setup_entry(hass, entry, async_add_entities):
    hub = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([LastUpload(hub), Pending(hub), Stored(hub), DeviceSeen(hub)])


class _Base(SensorEntity):
    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(self, hub, key: str, name: str) -> None:
        self.hub = hub
        self._attr_unique_id = f"{DOMAIN}_{key}"
        self._attr_name = name
        self._attr_device_info = DeviceInfo(identifiers={(DOMAIN, "comma")}, name="comma", manufacturer="comma.ai", model="comma 3X")

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(async_dispatcher_connect(self.hass, SIGNAL_UPDATE, self._update))

    @callback
    def _update(self) -> None:
        self.async_write_ha_state()


def _ts(s):
    return dt_util.parse_datetime(s) if s else None


class LastUpload(_Base):
    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _attr_icon = "mdi:cloud-upload"

    def __init__(self, hub):
        super().__init__(hub, "last_upload", "Last log upload")

    @property
    def native_value(self):
        return _ts(self.hub.data.get("last_upload"))

    @property
    def extra_state_attributes(self):
        recent = self.hub.data.get("recent", [])
        return {"recent": recent, "last_segment": recent[0]["segment"] if recent else None}


class Pending(_Base):
    _attr_icon = "mdi:progress-upload"
    _attr_native_unit_of_measurement = "files"
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, hub):
        super().__init__(hub, "pending", "Logs waiting on comma")

    @property
    def native_value(self):
        return self.hub.data.get("device", {}).get("pending_files")

    @property
    def extra_state_attributes(self):
        d = self.hub.data.get("device", {})
        return {"pending_mb": round(d.get("pending_bytes", 0) / 1e6, 1), "state": d.get("state"),
                "network": d.get("network"), "via": d.get("url"), "error": d.get("error"),
                "uploaded_since_boot": d.get("uploaded_session"), "version": d.get("version")}


class Stored(_Base):
    _attr_icon = "mdi:harddisk"
    _attr_device_class = SensorDeviceClass.DATA_SIZE
    _attr_native_unit_of_measurement = UnitOfInformation.GIGABYTES
    _attr_suggested_display_precision = 2
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, hub):
        super().__init__(hub, "stored", "Logs stored in HA")

    @property
    def native_value(self):
        return round(self.hub.bytes / 1e9, 3)

    @property
    def extra_state_attributes(self):
        return {"files": self.hub.files, "folder": "/media/comma"}


class DeviceSeen(_Base):
    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _attr_icon = "mdi:car-connected"

    def __init__(self, hub):
        super().__init__(hub, "device_seen", "comma last check-in")

    @property
    def native_value(self):
        return _ts(self.hub.data.get("device_seen"))
