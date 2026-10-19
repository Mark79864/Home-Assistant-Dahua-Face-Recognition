"""Registry of supported Dahua IVS (video analyse) event types.

This is the extension point for the integration. To add support for a
new event type from the Dahua API doc, add ONE entry to FEATURE_REGISTRY
below — nothing else needs to change. The generic IVS sensor platform
(see sensor.py's DahuaIVSFeatureSensor) reads this registry to build the
entity: its friendly name, icon, how to compute its state, and how to
turn the event's raw data into HA attributes.

To remove a feature, delete its entry — any camera that had it enabled
will simply stop getting that sensor after a reload/restart (the
config entry itself is untouched).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Optional

# Special pseudo-feature key for the existing Face Detection/Recognition
# sensors + snapshot image entity (sensor.py / image.py), which are richer
# than the generic single-sensor-per-feature model below and so keep
# their own dedicated entity classes. It still goes through the same
# per-camera feature picker in the config flow.
FEATURE_FACE = "Face"
FEATURE_FACE_NAME = "Face Detection & Recognition"
FEATURE_FACE_CODES = ("FaceDetection", "FaceRecognition")

# Two more pseudo-feature keys, same idea as FEATURE_FACE: not backed by
# an event-stream Code at all (so they're absent from FEATURE_REGISTRY
# and codes_for_features() below), but selectable per-camera in the same
# picker. These gate the White Light / Siren switches (switch.py), which
# are controlled directly (coaxialControlIO.cgi), not driven by events
# on the stream.
#
# White Light is Type=1 on that endpoint. On TiOC / active-deterrence
# camera models, this is the SAME physical output as the red/blue
# flashing warning light — confirmed against the rroller/dahua Home
# Assistant integration's production implementation, which treats it as
# one control regardless of which light the hardware actually has
# behind it. There's no separate control for it; this one switch is it.
FEATURE_WHITE_LIGHT = "WhiteLight"
FEATURE_WHITE_LIGHT_NAME = "White / Security Light (TiOC — white or red/blue depending on your camera)"
# Type=2 on the same endpoint. The API doc calls this "Speaker", but in
# practice (same source) it behaves as a siren: triggers for roughly
# 10-15 seconds and the device turns itself back off.
FEATURE_SIREN = "Siren"
FEATURE_SIREN_NAME = "Siren (TiOC)"

# PTZ (pan/tilt/zoom + presets) — gates the movement/preset buttons
# (button.py), the preset selector (select.py). Auto-detected: a
# successful ptz.cgi getPresets call (even with zero presets configured)
# means the channel has PTZ hardware.
FEATURE_PTZ = "PTZ"
FEATURE_PTZ_NAME = "PTZ (Pan/Tilt/Zoom)"

# Audio file playback — gates the Audio File Path / Audio File Name text
# entities and the Play Audio File button (text.py / button.py). NOT
# auto-detected: no reliable capability signal exists for it (see
# DahuaClient.play_audio_file's docstring), so it's manual opt-in only,
# same as FEATURE_FACE.
FEATURE_AUDIO_PLAYBACK = "AudioPlayback"
FEATURE_AUDIO_PLAYBACK_NAME = "Audio File Playback (speaker required)"

# Privacy Mask — gates a switch that toggles ALL of a channel's already-
# configured privacy-masking covers on/off at once (doesn't create/edit
# individual mask shapes). Auto-detected: a successful
# PrivacyMasking.cgi getPrivacyMaskingEnable call means the channel
# supports it.
FEATURE_PRIVACY_MASK = "PrivacyMask"
FEATURE_PRIVACY_MASK_NAME = "Privacy Mask"


def _bbox(obj: dict | None) -> Optional[list[int]]:
    """Normalize a BoundingBox field from either wire format into a plain
    [x1, y1, x2, y2] list."""
    if not obj:
        return None
    bb = obj.get("BoundingBox")
    if isinstance(bb, dict):
        # key=value multipart style parses BoundingBox[0..3] into a dict
        # like {"0": "4392", "1": "4136", ...}
        try:
            return [int(bb[k]) for k in sorted(bb, key=lambda x: int(x))]
        except (KeyError, ValueError):
            return None
    if isinstance(bb, list):
        try:
            return [int(v) for v in bb]
        except (TypeError, ValueError):
            return bb
    return None


def _object_attrs(data: dict) -> dict[str, Any]:
    """Common attribute shape for the many events keyed on a single
    Object plus an optional Objects[] array (Left/TakenAway/CrossLine/
    CrossRegion/Stay/QueueStay all follow this shape)."""
    attrs: dict[str, Any] = {}
    obj = data.get("Object")
    if obj:
        attrs["bounding_box"] = _bbox(obj)
    objects = data.get("Objects")
    if objects:
        attrs["object_count"] = len(objects)
        attrs["bounding_boxes"] = [_bbox(o) for o in objects]
    return attrs


def _wander_attrs(data: dict) -> dict[str, Any]:
    attrs = _object_attrs(data)
    if data.get("Tracks"):
        attrs["tracks"] = data.get("Tracks")
    if data.get("DetectRegion"):
        attrs["detect_region"] = data.get("DetectRegion")
    return attrs


def _stay_attrs(data: dict) -> dict[str, Any]:
    attrs = _object_attrs(data)
    attrs["area_id"] = data.get("AreaID")
    attrs["preset_id"] = data.get("PresetID")
    if data.get("DetectRegion"):
        attrs["detect_region"] = data.get("DetectRegion")
    return attrs


def _rule_id(data: dict) -> Any:
    """Best-effort extraction of a rule identifier from an event's data.

    The API doc pages for CrossLineDetection/CrossRegionDetection don't
    show an explicit rule-ID field, but Dahua's broader event set commonly
    identifies which configured rule fired via one of these key names —
    check them in order and use whichever is actually present on this
    device's events.
    """
    for key in ("Name", "RuleId", "RuleID", "Id", "ID"):
        value = data.get(key)
        if value not in (None, ""):
            return value
    return None


def _line_attrs(data: dict) -> dict[str, Any]:
    attrs = _object_attrs(data)
    attrs["direction"] = data.get("Direction")
    if data.get("DetectLine"):
        attrs["detect_line"] = data.get("DetectLine")
    rule_id = _rule_id(data)
    if rule_id is not None:
        attrs["rule_id"] = rule_id
    return attrs


def _region_attrs(data: dict) -> dict[str, Any]:
    attrs = _object_attrs(data)
    attrs["action"] = data.get("Action")
    attrs["direction"] = data.get("Direction")
    if data.get("DetectRegion"):
        attrs["detect_region"] = data.get("DetectRegion")
    rule_id = _rule_id(data)
    if rule_id is not None:
        attrs["rule_id"] = rule_id
    return attrs


def _human_trait_value(data: dict) -> str:
    human = data.get("HumanAttributes") or {}
    sex = human.get("Sex", "Unknown")
    age = human.get("Age")
    return f"{sex}, {age}" if age not in (None, "") else sex


def _human_trait_attrs(data: dict) -> dict[str, Any]:
    human = dict(data.get("HumanAttributes") or {})
    face = dict(data.get("FaceAttributes") or {})
    if "BoundingBox" in human:
        human["BoundingBox"] = _bbox({"BoundingBox": human["BoundingBox"]})
    if "BoundingBox" in face:
        face["BoundingBox"] = _bbox({"BoundingBox": face["BoundingBox"]})
    attrs: dict[str, Any] = {}
    if human:
        attrs["human_attributes"] = human
    if face:
        attrs["face_attributes"] = face
    return attrs


@dataclass
class Feature:
    """Declares one selectable camera feature backed by a single Dahua
    event Code."""

    code: str  # Event Code exactly as the NVR sends it — used both in
    # the "codes=[...]" stream subscription and to match incoming events.
    name: str  # Human-friendly name; used in the setup UI and as the
    # entity name suffix (e.g. "{camera} {name}").
    icon: str = "mdi:motion-sensor"
    # "start_stop": the event's Action is Start/Stop -> sensor state is
    #   "Detected" / "Clear".
    # "pulse": the event's Action is Pulse (no start/stop) -> sensor
    #   state comes from value_fn, defaulting to "Detected" if unset.
    state_kind: str = "start_stop"
    value_fn: Optional[Callable[[dict], Any]] = None
    attributes_fn: Callable[[dict], dict[str, Any]] = field(
        default=lambda data: {}
    )


FEATURE_REGISTRY: dict[str, Feature] = {
    "LeftDetection": Feature(
        code="LeftDetection",
        name="Left Object Detection",
        icon="mdi:bag-personal-off",
        attributes_fn=_object_attrs,
    ),
    "TakenAwayDetection": Feature(
        code="TakenAwayDetection",
        name="Taken Away Detection",
        icon="mdi:bag-personal-off-outline",
        attributes_fn=_object_attrs,
    ),
    "WanderDetection": Feature(
        code="WanderDetection",
        name="Wander Detection",
        icon="mdi:walk",
        attributes_fn=_wander_attrs,
    ),
    "StayDetection": Feature(
        code="StayDetection",
        name="Stay Detection",
        icon="mdi:human-male-height-variant",
        attributes_fn=_stay_attrs,
    ),
    "HumanTrait": Feature(
        code="HumanTrait",
        name="Human Trait",
        icon="mdi:account-search",
        state_kind="pulse",
        value_fn=_human_trait_value,
        attributes_fn=_human_trait_attrs,
    ),
    "CrossLineDetection": Feature(
        code="CrossLineDetection",
        name="Cross Line Detection",
        icon="mdi:arrow-decision",
        attributes_fn=_line_attrs,
    ),
    "CrossRegionDetection": Feature(
        code="CrossRegionDetection",
        name="Cross Region Detection",
        icon="mdi:vector-square",
        attributes_fn=_region_attrs,
    ),
    "QueueStayDetection": Feature(
        code="QueueStayDetection",
        name="Queue Stay Detection",
        icon="mdi:human-queue",
        attributes_fn=_stay_attrs,
    ),
}


def feature_options() -> dict[str, str]:
    """{feature_key: display_name} for every selectable feature, Face
    first, in the order used by the config flow's picker."""
    options = {
        FEATURE_FACE: FEATURE_FACE_NAME,
        FEATURE_WHITE_LIGHT: FEATURE_WHITE_LIGHT_NAME,
        FEATURE_SIREN: FEATURE_SIREN_NAME,
        FEATURE_PTZ: FEATURE_PTZ_NAME,
        FEATURE_AUDIO_PLAYBACK: FEATURE_AUDIO_PLAYBACK_NAME,
        FEATURE_PRIVACY_MASK: FEATURE_PRIVACY_MASK_NAME,
    }
    options.update({key: feat.name for key, feat in FEATURE_REGISTRY.items()})
    return options


def codes_for_features(feature_keys: list[str]) -> set[str]:
    """Event Codes the NVR stream needs to subscribe to for a list of
    selected feature keys (handles the FEATURE_FACE special case)."""
    codes: set[str] = set()
    for key in feature_keys:
        if key == FEATURE_FACE:
            codes.update(FEATURE_FACE_CODES)
        elif key in FEATURE_REGISTRY:
            codes.add(FEATURE_REGISTRY[key].code)
    return codes
