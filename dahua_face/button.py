"""Button platform for Dahua Face Recognition.

  * PTZ movement (Up/Down/Left/Right/Zoom In/Zoom Out) — created only
    if "PTZ (Pan/Tilt/Zoom)" is enabled for that camera. Each button
    issues a brief "nudge" (move then stop after ~0.5s) rather than a
    continuous move, since a button press has no natural "release"
    event to stop a continuous move on.
  * PTZ Go to Preset / PTZ Set Preset — act on whichever preset is
    currently chosen in the paired select entity (select.py), resolved
    at press time via the entity registry + state machine rather than
    a direct object reference, so these don't depend on platform setup
    order. Set Preset OVERWRITES that preset with the camera's current
    position — a deliberate, if destructive, documented action; the
    button press itself is the confirmation.
  * Play Audio File — created only if "Audio File Playback" is
    enabled. Plays whatever file name/path are currently set in the
    paired text entities (text.py), resolved the same way. See the
    README for why there's no file *picker* to choose from — no
    listing API exists to build one from.
"""
from __future__ import annotations

import logging

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .api import DahuaAuthError, DahuaClient, DahuaConnectionError
from .const import CONF_CAMERA_FEATURES, CONF_CAMERAS, DOMAIN
from .features import FEATURE_AUDIO_PLAYBACK, FEATURE_FACE, FEATURE_PTZ

_LOGGER = logging.getLogger(__name__)

_PTZ_NUDGE_SPEED = 5
_PTZ_NUDGE_DURATION = 0.5

# (label, ptz.cgi code, icon)
_PTZ_MOVE_BUTTONS = [
    ("Up", "Up", "mdi:arrow-up-bold"),
    ("Down", "Down", "mdi:arrow-down-bold"),
    ("Left", "Left", "mdi:arrow-left-bold"),
    ("Right", "Right", "mdi:arrow-right-bold"),
    ("Zoom In", "ZoomWide", "mdi:magnify-plus-outline"),
    ("Zoom Out", "ZoomTele", "mdi:magnify-minus-outline"),
]


def build_button_entities(
    hass: HomeAssistant,
    client: DahuaClient,
    entry: ConfigEntry,
    channel: int,
    camera_name: str,
    device_info: DeviceInfo,
    feature_keys: list[str],
) -> list[ButtonEntity]:
    """Entities for one camera given its currently-enabled feature keys.
    Shared by the initial platform setup and by __init__.py's live
    entity-add path."""
    entities: list[ButtonEntity] = []
    if FEATURE_PTZ in feature_keys:
        for label, code, icon in _PTZ_MOVE_BUTTONS:
            entities.append(
                DahuaPtzMoveButton(
                    client, entry, channel, camera_name, device_info, label, code, icon
                )
            )
        entities.append(
            DahuaPtzGotoPresetButton(hass, client, entry, channel, camera_name, device_info)
        )
        entities.append(
            DahuaPtzSetPresetButton(hass, client, entry, channel, camera_name, device_info)
        )
    if FEATURE_AUDIO_PLAYBACK in feature_keys:
        entities.append(
            DahuaPlayAudioFileButton(hass, client, entry, channel, camera_name, device_info)
        )
    return entities


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up buttons for every configured camera, per its enabled features."""
    store = hass.data[DOMAIN][entry.entry_id]
    store.setdefault("add_entities", {})["button"] = async_add_entities
    client: DahuaClient = store["client"]

    cameras: dict[str, str] = entry.data.get(CONF_CAMERAS, {})
    camera_features: dict[str, list[str]] = entry.data.get(CONF_CAMERA_FEATURES, {})

    entities: list[ButtonEntity] = []
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
            build_button_entities(hass, client, entry, channel, name, device_info, enabled)
        )

    async_add_entities(entities)


class DahuaPtzMoveButton(ButtonEntity):
    """A brief directional/zoom nudge."""

    def __init__(
        self,
        client: DahuaClient,
        entry: ConfigEntry,
        channel: int,
        camera_name: str,
        device_info: DeviceInfo,
        label: str,
        code: str,
        icon: str,
    ) -> None:
        self._client = client
        self._channel = channel
        self._camera_name = camera_name
        self._code = code
        self._label = label
        self._attr_icon = icon
        self._attr_device_info = device_info
        self._attr_unique_id = f"{entry.entry_id}_{channel}_ptz_{code.lower()}"

    @property
    def name(self) -> str:
        return f"{self._camera_name} PTZ {self._label}"

    async def async_press(self) -> None:
        try:
            await self._client.ptz_nudge(
                self._channel + 1,
                self._code,
                speed=_PTZ_NUDGE_SPEED,
                duration=_PTZ_NUDGE_DURATION,
            )
        except (DahuaAuthError, DahuaConnectionError) as err:
            raise HomeAssistantError(
                f"Could not move PTZ ({self._label}) on channel {self._channel}: {err}"
            ) from err


def _resolve_state(hass: HomeAssistant, platform: str, unique_id: str) -> str | None:
    """Look up an entity's current state by its unique_id, via the
    entity registry — decouples this platform from needing a direct
    object reference to an entity created by a different platform
    (select.py / text.py), and works regardless of platform setup
    order since it's resolved at press time, not at construction time.
    """
    registry = er.async_get(hass)
    entity_id = registry.async_get_entity_id(platform, DOMAIN, unique_id)
    if entity_id is None:
        return None
    state = hass.states.get(entity_id)
    if state is None or state.state in (None, "unknown", "unavailable"):
        return None
    return state.state


class DahuaPtzGotoPresetButton(ButtonEntity):
    """Go to whichever preset is selected in the paired PTZ Preset
    select entity."""

    _attr_icon = "mdi:crosshairs-gps"

    def __init__(
        self,
        hass: HomeAssistant,
        client: DahuaClient,
        entry: ConfigEntry,
        channel: int,
        camera_name: str,
        device_info: DeviceInfo,
    ) -> None:
        self._hass = hass
        self._client = client
        self._entry = entry
        self._channel = channel
        self._camera_name = camera_name
        self._attr_device_info = device_info
        self._attr_unique_id = f"{entry.entry_id}_{channel}_ptz_goto_preset"

    @property
    def name(self) -> str:
        return f"{self._camera_name} PTZ Go to Preset"

    def _selected_preset_index(self) -> int | None:
        select_unique_id = f"{self._entry.entry_id}_{self._channel}_ptz_preset"
        label = _resolve_state(self._hass, "select", select_unique_id)
        if label is None:
            return None
        try:
            return int(label.split(":", 1)[0].strip())
        except (ValueError, IndexError):
            return None

    async def async_press(self) -> None:
        preset_index = self._selected_preset_index()
        if preset_index is None:
            raise HomeAssistantError(
                "No PTZ preset is selected — choose one in the PTZ Preset "
                "selector first."
            )
        try:
            await self._client.ptz_goto_preset(self._channel + 1, preset_index)
        except (DahuaAuthError, DahuaConnectionError) as err:
            raise HomeAssistantError(
                f"Could not go to PTZ preset {preset_index}: {err}"
            ) from err


class DahuaPtzSetPresetButton(ButtonEntity):
    """Save the camera's CURRENT position as whichever preset is
    selected in the paired PTZ Preset select entity — overwrites it."""

    _attr_icon = "mdi:crosshairs-gps"

    def __init__(
        self,
        hass: HomeAssistant,
        client: DahuaClient,
        entry: ConfigEntry,
        channel: int,
        camera_name: str,
        device_info: DeviceInfo,
    ) -> None:
        self._hass = hass
        self._client = client
        self._entry = entry
        self._channel = channel
        self._camera_name = camera_name
        self._attr_device_info = device_info
        self._attr_unique_id = f"{entry.entry_id}_{channel}_ptz_set_preset"

    @property
    def name(self) -> str:
        return f"{self._camera_name} PTZ Set Preset"

    def _selected_preset_index(self) -> int | None:
        select_unique_id = f"{self._entry.entry_id}_{self._channel}_ptz_preset"
        label = _resolve_state(self._hass, "select", select_unique_id)
        if label is None:
            return None
        try:
            return int(label.split(":", 1)[0].strip())
        except (ValueError, IndexError):
            return None

    async def async_press(self) -> None:
        preset_index = self._selected_preset_index()
        if preset_index is None:
            raise HomeAssistantError(
                "No PTZ preset is selected — choose one in the PTZ Preset "
                "selector first (this OVERWRITES that preset with the "
                "camera's current position)."
            )
        try:
            await self._client.ptz_set_preset(self._channel + 1, preset_index)
        except (DahuaAuthError, DahuaConnectionError) as err:
            raise HomeAssistantError(
                f"Could not set PTZ preset {preset_index}: {err}"
            ) from err


class DahuaPlayAudioFileButton(ButtonEntity):
    """Play whatever file name/path are currently set in the paired
    Audio File Path / Audio File Name text entities."""

    _attr_icon = "mdi:play-circle-outline"

    def __init__(
        self,
        hass: HomeAssistant,
        client: DahuaClient,
        entry: ConfigEntry,
        channel: int,
        camera_name: str,
        device_info: DeviceInfo,
    ) -> None:
        self._hass = hass
        self._client = client
        self._entry = entry
        self._channel = channel
        self._camera_name = camera_name
        self._attr_device_info = device_info
        self._attr_unique_id = f"{entry.entry_id}_{channel}_play_audio_file"

    @property
    def name(self) -> str:
        return f"{self._camera_name} Play Audio File"

    async def async_press(self) -> None:
        path_unique_id = f"{self._entry.entry_id}_{self._channel}_audio_path"
        name_unique_id = f"{self._entry.entry_id}_{self._channel}_audio_file_name"
        path = _resolve_state(self._hass, "text", path_unique_id) or "/Audio_Path"
        file_name = _resolve_state(self._hass, "text", name_unique_id)

        if not file_name:
            raise HomeAssistantError(
                "No audio file name is set — enter one in the Audio File "
                "Name text entity first."
            )
        try:
            await self._client.play_audio_file(path, file_name)
        except (DahuaAuthError, DahuaConnectionError) as err:
            raise HomeAssistantError(
                f"Could not play audio file {file_name!r}: {err}"
            ) from err
