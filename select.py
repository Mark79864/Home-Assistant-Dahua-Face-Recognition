"""Select platform for Dahua Face Recognition.

One PTZ preset selector per camera with PTZ enabled — lists presets
actually configured on the device (via ptz.cgi's getPresets, refreshed
every 5 minutes), paired with the "PTZ Go to Preset" / "PTZ Set Preset"
buttons in button.py, which act on whichever preset is currently
selected here.
"""
from __future__ import annotations

import logging
from datetime import timedelta

from homeassistant.components.select import SelectEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .api import DahuaAuthError, DahuaClient, DahuaConnectionError
from .const import CONF_CAMERA_FEATURES, CONF_CAMERAS, DOMAIN
from .features import FEATURE_FACE, FEATURE_PTZ

_LOGGER = logging.getLogger(__name__)

# Presets change rarely — poll far less often than the switches/text
# entities (60s) to avoid hammering ptz.cgi for something that's
# usually static.
SCAN_INTERVAL = timedelta(seconds=300)


def build_select_entities(
    client: DahuaClient,
    entry: ConfigEntry,
    channel: int,
    camera_name: str,
    device_info: DeviceInfo,
    feature_keys: list[str],
) -> list[SelectEntity]:
    """Entities for one camera given its currently-enabled feature keys.
    Shared by the initial platform setup and by __init__.py's live
    entity-add path."""
    if FEATURE_PTZ not in feature_keys:
        return []
    return [DahuaPtzPresetSelect(client, entry, channel, camera_name, device_info)]


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the PTZ preset selector for every camera with PTZ enabled."""
    store = hass.data[DOMAIN][entry.entry_id]
    store.setdefault("add_entities", {})["select"] = async_add_entities
    client: DahuaClient = store["client"]

    cameras: dict[str, str] = entry.data.get(CONF_CAMERAS, {})
    camera_features: dict[str, list[str]] = entry.data.get(CONF_CAMERA_FEATURES, {})

    entities: list[SelectEntity] = []
    for channel_str, name in cameras.items():
        channel = int(channel_str)
        enabled = camera_features.get(channel_str, [FEATURE_FACE])
        device_info = DeviceInfo(
            identifiers={(DOMAIN, f"{entry.entry_id}_{channel}")},
            name=name,
            manufacturer="Dahua",
            model="NVR Camera Channel",
            via_device=(DOMAIN, entry.entry_id),
        )
        entities.extend(
            build_select_entities(client, entry, channel, name, device_info, enabled)
        )

    async_add_entities(entities, True)


def _preset_label(preset: dict) -> str:
    index = preset["index"]
    name = preset.get("name") or f"Preset {index}"
    return f"{index}: {name}"


class DahuaPtzPresetSelect(SelectEntity):
    """Which PTZ preset is currently selected for the paired Go to
    Preset / Set Preset buttons (button.py) to act on. The option label
    format is "{index}: {name}" — the paired buttons parse the leading
    number back out; see button.py."""

    _attr_should_poll = True
    _attr_icon = "mdi:crosshairs-gps"

    def __init__(
        self,
        client: DahuaClient,
        entry: ConfigEntry,
        channel: int,
        camera_name: str,
        device_info: DeviceInfo,
    ) -> None:
        self._client = client
        self._channel = channel
        self._camera_name = camera_name
        self._attr_device_info = device_info
        self._attr_unique_id = f"{entry.entry_id}_{channel}_ptz_preset"
        self._attr_current_option: str | None = None
        self._attr_options: list[str] = []
        self._attr_available = True

    @property
    def name(self) -> str:
        return f"{self._camera_name} PTZ Preset"

    async def async_update(self) -> None:
        try:
            presets = await self._client.get_ptz_presets(self._channel + 1)
        except DahuaAuthError:
            _LOGGER.error(
                "Authentication failed reading PTZ presets for channel %s",
                self._channel,
            )
            self._attr_available = False
            return
        except DahuaConnectionError as err:
            _LOGGER.warning(
                "Could not read PTZ presets for channel %s: %s", self._channel, err
            )
            self._attr_available = False
            return

        self._attr_available = True
        options = [_preset_label(preset) for preset in presets]
        self._attr_options = options
        if self._attr_current_option not in options:
            self._attr_current_option = options[0] if options else None

    async def async_select_option(self, option: str) -> None:
        self._attr_current_option = option
        self.async_write_ha_state()
