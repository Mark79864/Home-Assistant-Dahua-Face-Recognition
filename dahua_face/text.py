"""Text platform for Dahua Face Recognition.

  * Audio File Path / Audio File Name — created only if "Audio File
    Playback" is enabled for that camera. These hold the values the
    Play Audio File button (button.py) uses. There is no known Dahua
    API to list audio files already stored on the device (checked both
    the official API doc and the rroller/dahua integration's source —
    neither has one), so these are plain local text fields: type in the
    filename you already know exists there (e.g. from the camera's own
    web UI's Audio Management / alarm-audio page), not a browsable
    picker.

Custom overlay (OSD) text is deliberately NOT an entity here — use the
`dahua_face.set_custom_overlay` service instead (see __init__.py /
api.py's set_overlay_lines). That keeps arbitrary overlay text (spaces,
`%`, `°`, etc.) as a plain action-call argument rather than routed
through a Home Assistant text-input box, and supports up to 6 stacked
lines instead of just one.
"""
from __future__ import annotations

import logging
from datetime import timedelta

from homeassistant.components.text import TextEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import CONF_CAMERA_FEATURES, CONF_CAMERAS, DOMAIN
from .features import FEATURE_AUDIO_PLAYBACK, FEATURE_FACE

_LOGGER = logging.getLogger(__name__)


def build_text_entities(
    entry: ConfigEntry,
    channel: int,
    camera_name: str,
    device_info: DeviceInfo,
    feature_keys: list[str],
) -> list[TextEntity]:
    """Entities for one camera given its currently-enabled feature keys.
    Shared by the initial platform setup and by __init__.py's live
    entity-add path."""
    entities: list[TextEntity] = []
    if FEATURE_AUDIO_PLAYBACK in feature_keys:
        entities.append(DahuaAudioPathText(entry, channel, camera_name, device_info))
        entities.append(
            DahuaAudioFileNameText(entry, channel, camera_name, device_info)
        )
    return entities


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up text entities for every configured camera."""
    store = hass.data[DOMAIN][entry.entry_id]
    store.setdefault("add_entities", {})["text"] = async_add_entities

    cameras: dict[str, str] = entry.data.get(CONF_CAMERAS, {})
    camera_features: dict[str, list[str]] = entry.data.get(CONF_CAMERA_FEATURES, {})

    entities: list[TextEntity] = []
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
        entities.extend(build_text_entities(entry, channel, name, device_info, enabled))

    async_add_entities(entities)


class DahuaAudioPathText(TextEntity):
    """Directory on the device to look for the audio file in, used by
    the Play Audio File button. Purely local state — not read from or
    validated against the device, since there's no listing API to check
    it against."""

    _attr_should_poll = False
    _attr_icon = "mdi:folder-outline"
    _attr_mode = "text"

    def __init__(
        self,
        entry: ConfigEntry,
        channel: int,
        camera_name: str,
        device_info: DeviceInfo,
    ) -> None:
        self._camera_name = camera_name
        self._attr_device_info = device_info
        self._attr_unique_id = f"{entry.entry_id}_{channel}_audio_path"
        self._attr_native_value = "/Audio_Path"
        self._attr_available = True

    @property
    def name(self) -> str:
        return f"{self._camera_name} Audio File Path"

    async def async_set_value(self, value: str) -> None:
        self._attr_native_value = value
        self.async_write_ha_state()


class DahuaAudioFileNameText(TextEntity):
    """Name of the audio file to play, used by the Play Audio File
    button. Purely local state — type the filename you already know
    exists on the device (see the module docstring: there's no listing
    API to build a picker from)."""

    _attr_should_poll = False
    _attr_icon = "mdi:file-music-outline"
    _attr_mode = "text"

    def __init__(
        self,
        entry: ConfigEntry,
        channel: int,
        camera_name: str,
        device_info: DeviceInfo,
    ) -> None:
        self._camera_name = camera_name
        self._attr_device_info = device_info
        self._attr_unique_id = f"{entry.entry_id}_{channel}_audio_file_name"
        self._attr_native_value = ""
        self._attr_available = True

    @property
    def name(self) -> str:
        return f"{self._camera_name} Audio File Name"

    async def async_set_value(self, value: str) -> None:
        self._attr_native_value = value
        self.async_write_ha_state()
