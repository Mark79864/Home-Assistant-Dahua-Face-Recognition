"""Sensor platform for Dahua Face Recognition.

Two kinds of sensors are created per configured camera (channel), based
on which features were enabled for it during setup:

  * If "Face Detection & Recognition" is enabled: the four dedicated
    sensors below (FaceName, FaceGender, FaceList, FaceExpression,
    FaceConfidence — plus a snapshot image entity, see image.py).
  * For every other enabled feature (LeftDetection, WanderDetection,
    HumanTrait, CrossLineDetection, ...): one generic
    DahuaIVSFeatureSensor, built from that feature's entry in
    features.FEATURE_REGISTRY. Adding a new feature to that registry
    automatically makes it available here — no new sensor class needed.

Each camera/channel is represented as its own Home Assistant device.
"""
from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.sensor import SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import (
    ATTR_AGE,
    ATTR_BEARD,
    ATTR_CANDIDATES,
    ATTR_EYE,
    ATTR_FEATURES,
    ATTR_GROUP_ID,
    ATTR_LAST_EVENT_CODE,
    ATTR_MASK,
    ATTR_MOUTH,
    ATTR_SIMILARITY,
    ATTR_UID,
    CLEAR_STATE,
    CONF_CAMERA_FEATURES,
    CONF_CAMERAS,
    DOMAIN,
    SIGNAL_FACE_AVAILABILITY,
    SIGNAL_FACE_CLEAR,
    SIGNAL_FACE_EVENT,
    UNKNOWN_GENDER,
    UNKNOWN_NAME,
)
from .features import FEATURE_FACE, FEATURE_REGISTRY, Feature

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up sensors for each configured camera, per its enabled features."""
    hass.data[DOMAIN][entry.entry_id].setdefault("add_entities", {})[
        "sensor"
    ] = async_add_entities

    cameras: dict[str, str] = entry.data.get(CONF_CAMERAS, {})
    camera_features: dict[str, list[str]] = entry.data.get(CONF_CAMERA_FEATURES, {})

    entities: list[SensorEntity] = []
    for channel_str, name in cameras.items():
        channel = int(channel_str)
        # Backward compatible default: cameras added before the feature
        # picker existed keep behaving exactly as before (Face only).
        enabled = camera_features.get(channel_str, [FEATURE_FACE])
        device_info = _device_info(entry, channel, name)
        entities.extend(build_sensor_entities(entry, channel, name, device_info, enabled))

    async_add_entities(entities)


def _device_info(entry: ConfigEntry, channel: int, name: str) -> DeviceInfo:
    return DeviceInfo(
        identifiers={(DOMAIN, f"{entry.entry_id}_{channel}")},
        name=name,
        manufacturer="Dahua",
        model="NVR Camera Channel",
        via_device=(DOMAIN, entry.entry_id),
    )


def build_sensor_entities(
    entry: ConfigEntry,
    channel: int,
    camera_name: str,
    device_info: DeviceInfo,
    feature_keys: list[str],
) -> list[SensorEntity]:
    """Entities for one camera given its currently-enabled feature keys.
    Shared by the initial platform setup and by __init__.py's live
    entity-add path when a camera/feature is added after setup without
    a reload — so pass only the NEWLY enabled feature_keys there, not the
    camera's full enabled set, to avoid creating duplicate entities."""
    entities: list[SensorEntity] = []

    if FEATURE_FACE in feature_keys:
        entities.extend(
            [
                DahuaFaceNameSensor(entry, channel, camera_name, device_info),
                DahuaFaceGenderSensor(entry, channel, camera_name, device_info),
                DahuaFaceListSensor(entry, channel, camera_name, device_info),
                DahuaFaceExpressionSensor(entry, channel, camera_name, device_info),
                DahuaFaceConfidenceSensor(entry, channel, camera_name, device_info),
            ]
        )

    for feature_key in feature_keys:
        feature = FEATURE_REGISTRY.get(feature_key)
        if feature is not None:
            entities.append(
                DahuaIVSFeatureSensor(entry, channel, camera_name, device_info, feature)
            )

    return entities


class _DahuaFaceBaseSensor(SensorEntity):
    """Common wiring: dispatcher subscription, availability, device info.

    All sensors default to the "Clear" resting state from the moment
    they're added — before the first matching event arrives — rather
    than showing as unknown/unavailable. Subclasses with a non-text
    native value (e.g. the numeric Face Confidence sensor) override
    _clear_value() to provide their own equivalent "nothing going on"
    value.
    """

    _attr_should_poll = False
    _sensor_suffix = "base"
    # Only the Face sensors listen for the simulated-timeout clear signal
    # (see __init__.py); generic IVS sensors already get a real Stop event
    # from the NVR and don't need it.
    _listens_for_face_clear = False

    def __init__(
        self,
        entry: ConfigEntry,
        channel: int,
        camera_name: str,
        device_info: DeviceInfo,
        unique_id_suffix: str | None = None,
    ) -> None:
        self._entry = entry
        self._channel = channel
        self._camera_name = camera_name
        self._attr_device_info = device_info
        suffix = unique_id_suffix or self._sensor_suffix
        self._attr_unique_id = f"{entry.entry_id}_{channel}_{suffix}"
        # Optimistic: the listener sets availability True right when it
        # starts attempting to connect (see __init__.py), so defaulting to
        # available here just means the "Clear" resting state is visible
        # immediately on setup instead of showing as unavailable.
        self._attr_available = True
        self._attr_native_value: Any = self._clear_value()
        self._extra_attrs: dict[str, Any] = {}

    def _clear_value(self) -> Any:
        """The value this sensor shows when nothing is currently going on
        (on first setup, and — for Face sensors — after the clear timeout).
        Override for non-text sensors (e.g. a numeric confidence score)."""
        return CLEAR_STATE

    async def async_added_to_hass(self) -> None:
        event_signal = SIGNAL_FACE_EVENT.format(
            entry_id=self._entry.entry_id, channel=self._channel
        )
        avail_signal = SIGNAL_FACE_AVAILABILITY.format(
            entry_id=self._entry.entry_id, channel=self._channel
        )
        self.async_on_remove(
            async_dispatcher_connect(self.hass, event_signal, self._handle_event)
        )
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass, avail_signal, self._handle_availability
            )
        )
        if self._listens_for_face_clear:
            clear_signal = SIGNAL_FACE_CLEAR.format(
                entry_id=self._entry.entry_id, channel=self._channel
            )
            self.async_on_remove(
                async_dispatcher_connect(self.hass, clear_signal, self._handle_clear)
            )

    @callback
    def _handle_availability(self, available: bool) -> None:
        self._attr_available = available
        self.async_write_ha_state()

    @callback
    def _handle_event(self, event: dict) -> None:
        """Override in subclasses."""
        raise NotImplementedError

    @callback
    def _handle_clear(self, _payload: Any = None) -> None:
        """Revert to the resting state. Only wired up for sensors with
        _listens_for_face_clear = True; harmless no-op otherwise."""
        self._attr_native_value = self._clear_value()
        self._extra_attrs = {}
        self.async_write_ha_state()

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return self._extra_attrs


def _top_candidate(event_data: dict) -> dict | None:
    candidates = event_data.get("Candidates") or []
    if not candidates:
        return None
    # Pick the highest-similarity candidate; fall back to first if
    # similarity is missing/non-numeric.
    def _sim(c: dict) -> float:
        try:
            return float(c.get("Similarity", 0))
        except (TypeError, ValueError):
            return 0.0

    return max(candidates, key=_sim)


def _primary_face(code: str, event_data: dict) -> dict:
    """Return the single face dict relevant to this event.

    FaceDetection events carry a "Faces" *array* (one entry per face seen
    in frame); FaceRecognition events carry a single "Face" *object* (the
    face that was matched against the face group). When multiple faces are
    detected at once, the first one is used for these single-value sensors.
    """
    if code == "FaceDetection":
        faces = event_data.get("Faces") or []
        return faces[0] if faces else {}
    if code == "FaceRecognition":
        return event_data.get("Face") or {}
    return {}


class DahuaFaceNameSensor(_DahuaFaceBaseSensor):
    """Name of the last recognized person."""

    _sensor_suffix = "face_name"
    _attr_icon = "mdi:account"
    _listens_for_face_clear = True

    @property
    def name(self) -> str:
        return f"{self._camera_name} Face Name"

    @callback
    def _handle_event(self, event: dict) -> None:
        code = event.get("code")
        data = event.get("data") or {}
        self._attr_available = True

        if code == "FaceRecognition":
            top = _top_candidate(data)
            if top:
                person = top.get("Person", {})
                self._attr_native_value = person.get("Name") or UNKNOWN_NAME
                self._extra_attrs = {
                    ATTR_UID: person.get("UID"),
                    ATTR_GROUP_ID: person.get("GroupID"),
                    ATTR_LAST_EVENT_CODE: code,
                }
            else:
                self._attr_native_value = UNKNOWN_NAME
                self._extra_attrs = {ATTR_LAST_EVENT_CODE: code}
        elif code == "FaceDetection":
            # A face was seen but not (yet) matched against a face group.
            self._attr_native_value = UNKNOWN_NAME
            self._extra_attrs = {ATTR_LAST_EVENT_CODE: code}
        else:
            return
        self.async_write_ha_state()


class DahuaFaceGenderSensor(_DahuaFaceBaseSensor):
    """Gender ("Man"/"Woman") from the last detected face."""

    _sensor_suffix = "face_gender"
    _attr_icon = "mdi:gender-male-female"
    _listens_for_face_clear = True

    @property
    def name(self) -> str:
        return f"{self._camera_name} Face Gender"

    @callback
    def _handle_event(self, event: dict) -> None:
        code = event.get("code")
        if code not in ("FaceDetection", "FaceRecognition"):
            return
        data = event.get("data") or {}
        face = _primary_face(code, data)
        self._attr_native_value = face.get("Sex") or UNKNOWN_GENDER
        self._extra_attrs = {
            ATTR_AGE: face.get("Age"),
            ATTR_LAST_EVENT_CODE: code,
        }
        self._attr_available = True
        self.async_write_ha_state()


class DahuaFaceListSensor(_DahuaFaceBaseSensor):
    """Face group / database (GroupID) the person was matched in.

    Only FaceRecognition events carry a match, so this reflects the
    best-matching candidate's GroupID. A plain FaceDetection (no match
    attempted/found) reports "Unknown".
    """

    _sensor_suffix = "face_list"
    _attr_icon = "mdi:database-outline"
    _listens_for_face_clear = True

    @property
    def name(self) -> str:
        return f"{self._camera_name} Face List"

    @callback
    def _handle_event(self, event: dict) -> None:
        code = event.get("code")
        data = event.get("data") or {}
        self._attr_available = True

        if code == "FaceRecognition":
            top = _top_candidate(data)
            if top:
                person = top.get("Person", {})
                self._attr_native_value = person.get("GroupID") or UNKNOWN_NAME
                self._extra_attrs = {
                    ATTR_UID: person.get("UID"),
                    "name": person.get("Name"),
                    ATTR_SIMILARITY: top.get("Similarity"),
                    ATTR_LAST_EVENT_CODE: code,
                }
            else:
                self._attr_native_value = UNKNOWN_NAME
                self._extra_attrs = {ATTR_LAST_EVENT_CODE: code}
        elif code == "FaceDetection":
            self._attr_native_value = UNKNOWN_NAME
            self._extra_attrs = {ATTR_LAST_EVENT_CODE: code}
        else:
            return
        self.async_write_ha_state()


class DahuaFaceExpressionSensor(_DahuaFaceBaseSensor):
    """Facial attribute/feature list from the last detected face
    (e.g. WearGlasses, Smile, Anger, Mask...)."""

    _sensor_suffix = "face_expression"
    _attr_icon = "mdi:emoticon-outline"
    _listens_for_face_clear = True

    @property
    def name(self) -> str:
        return f"{self._camera_name} Face Expression"

    @callback
    def _handle_event(self, event: dict) -> None:
        code = event.get("code")
        if code not in ("FaceDetection", "FaceRecognition"):
            return
        data = event.get("data") or {}
        face = _primary_face(code, data)
        features = face.get("Feature") or []
        if isinstance(features, dict):
            # key=value multipart style yields {"0": "SunGlasses", "1": "Smile"}
            features = [features[k] for k in sorted(features, key=lambda x: int(x))]
        self._attr_native_value = ", ".join(features) if features else "None"
        self._extra_attrs = {
            ATTR_FEATURES: features,
            ATTR_EYE: face.get("Eye"),
            ATTR_MOUTH: face.get("Mouth"),
            ATTR_MASK: face.get("Mask"),
            ATTR_BEARD: face.get("Beard"),
            ATTR_LAST_EVENT_CODE: code,
        }
        self._attr_available = True
        self.async_write_ha_state()


class DahuaFaceConfidenceSensor(_DahuaFaceBaseSensor):
    """Similarity score of the best-matching candidate (1-100)."""

    _sensor_suffix = "face_confidence"
    _attr_icon = "mdi:percent"
    _attr_native_unit_of_measurement = "%"
    _attr_state_class = "measurement"
    _listens_for_face_clear = True

    def _clear_value(self) -> int:
        # Numeric sensor (%) — 0 is its "nothing detected" equivalent of
        # the text "Clear" state other Face sensors use.
        return 0

    @property
    def name(self) -> str:
        return f"{self._camera_name} Face Confidence"

    @callback
    def _handle_event(self, event: dict) -> None:
        code = event.get("code")
        data = event.get("data") or {}
        self._attr_available = True

        if code == "FaceRecognition":
            top = _top_candidate(data)
            try:
                self._attr_native_value = int(top.get("Similarity")) if top else 0
            except (TypeError, ValueError):
                self._attr_native_value = 0
            self._extra_attrs = {
                ATTR_CANDIDATES: data.get("Candidates"),
                ATTR_LAST_EVENT_CODE: code,
            }
        elif code == "FaceDetection":
            self._attr_native_value = 0
            self._extra_attrs = {ATTR_LAST_EVENT_CODE: code}
        else:
            return
        self.async_write_ha_state()


class DahuaIVSFeatureSensor(_DahuaFaceBaseSensor):
    """Generic sensor for any feature declared in features.FEATURE_REGISTRY
    (LeftDetection, WanderDetection, HumanTrait, CrossLineDetection, ...).

    Adding a new event type to that registry is enough to get a working
    sensor here — no new class needed. See features.py for the extension
    point.
    """

    def __init__(
        self,
        entry: ConfigEntry,
        channel: int,
        camera_name: str,
        device_info: DeviceInfo,
        feature: Feature,
    ) -> None:
        super().__init__(
            entry,
            channel,
            camera_name,
            device_info,
            unique_id_suffix=f"ivs_{feature.code.lower()}",
        )
        self._feature = feature
        self._attr_icon = feature.icon

    @property
    def name(self) -> str:
        return f"{self._camera_name} {self._feature.name}"

    @callback
    def _handle_event(self, event: dict) -> None:
        if event.get("code") != self._feature.code:
            return
        data = event.get("data") or {}
        action = (event.get("action") or "").strip().lower()

        if self._feature.state_kind == "pulse":
            if self._feature.value_fn:
                self._attr_native_value = self._feature.value_fn(data)
            else:
                self._attr_native_value = "Detected"
        else:
            if action == "start":
                self._attr_native_value = "Detected"
            elif action == "stop":
                self._attr_native_value = CLEAR_STATE
            else:
                return

        try:
            attrs = dict(self._feature.attributes_fn(data))
        except Exception:  # noqa: BLE001 - never let a bad payload break the sensor
            _LOGGER.exception(
                "Error building attributes for %s event on channel %s",
                self._feature.code,
                self._channel,
            )
            attrs = {}
        attrs[ATTR_LAST_EVENT_CODE] = event.get("code")
        self._extra_attrs = attrs

        self._attr_available = True
        self.async_write_ha_state()
