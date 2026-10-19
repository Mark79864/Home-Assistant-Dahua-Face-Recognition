"""Image platform for Dahua Face Recognition.

Exposes one image entity per configured camera: a snapshot of the last
detected/recognized face, taken from the image/jpeg part that Dahua
attaches immediately after a FaceDetection/FaceRecognition event's
text/plain part on the event stream (see the API doc's "snap
subscription" example).
"""
from __future__ import annotations

import logging

from homeassistant.components.image import ImageEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.util import dt as dt_util

from .const import (
    CONF_CAMERA_FEATURES,
    CONF_CAMERAS,
    DOMAIN,
    SIGNAL_FACE_AVAILABILITY,
    SIGNAL_FACE_SNAPSHOT,
)
from .features import FEATURE_FACE

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the last-seen-face snapshot image entity for each camera
    that has the Face Detection & Recognition feature enabled."""
    hass.data[DOMAIN][entry.entry_id].setdefault("add_entities", {})[
        "image"
    ] = async_add_entities

    cameras: dict[str, str] = entry.data.get(CONF_CAMERAS, {})
    camera_features: dict[str, list[str]] = entry.data.get(CONF_CAMERA_FEATURES, {})

    entities = []
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
        entities.extend(build_image_entities(hass, entry, channel, name, device_info, enabled))

    async_add_entities(entities)


def build_image_entities(
    hass: HomeAssistant,
    entry: ConfigEntry,
    channel: int,
    camera_name: str,
    device_info: DeviceInfo,
    feature_keys: list[str],
) -> list["DahuaFaceSnapshotImage"]:
    """Entities for one camera given its currently-enabled feature keys.
    Shared by the initial platform setup and by __init__.py's live
    entity-add path when a camera/feature is added after setup without
    a reload."""
    if FEATURE_FACE not in feature_keys:
        return []
    return [DahuaFaceSnapshotImage(hass, entry, channel, camera_name, device_info)]


class DahuaFaceSnapshotImage(ImageEntity):
    """Snapshot of the last detected/recognized face for one camera."""

    _attr_should_poll = False
    _attr_content_type = "image/jpeg"

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        channel: int,
        camera_name: str,
        device_info: DeviceInfo,
    ) -> None:
        super().__init__(hass, verify_ssl=False)
        self._entry = entry
        self._channel = channel
        self._camera_name = camera_name
        self._attr_device_info = device_info
        self._attr_unique_id = f"{entry.entry_id}_{channel}_face_snapshot"
        self._attr_available = False
        self._image_bytes: bytes | None = None

    @property
    def name(self) -> str:
        return f"{self._camera_name} Last Face Snapshot"

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        snapshot_signal = SIGNAL_FACE_SNAPSHOT.format(
            entry_id=self._entry.entry_id, channel=self._channel
        )
        avail_signal = SIGNAL_FACE_AVAILABILITY.format(
            entry_id=self._entry.entry_id, channel=self._channel
        )
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass, snapshot_signal, self._handle_snapshot
            )
        )
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass, avail_signal, self._handle_availability
            )
        )

    @callback
    def _handle_availability(self, available: bool) -> None:
        self._attr_available = available
        self.async_write_ha_state()

    @callback
    def _handle_snapshot(self, image_bytes: bytes) -> None:
        self._image_bytes = image_bytes
        self._attr_image_last_updated = dt_util.utcnow()
        self._attr_available = True
        self.async_write_ha_state()

    async def async_image(self) -> bytes | None:
        return self._image_bytes
