"""The Dahua Face Recognition integration."""
from __future__ import annotations

import asyncio
import logging
import random

import aiohttp
import voluptuous as vol
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import ATTR_DEVICE_ID, CONF_HOST, CONF_PASSWORD, CONF_PORT, CONF_USERNAME
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.event import async_call_later
from homeassistant.helpers import device_registry as dr, entity_registry as er

from .api import DahuaAuthError, DahuaClient, DahuaConnectionError, OVERLAY_MAX_LINES
from .const import (
    CONF_CAMERA_FEATURES,
    CONF_CAMERAS,
    DOMAIN,
    FACE_CLEAR_TIMEOUT,
    RECONNECT_DELAY_MAX,
    RECONNECT_DELAY_MIN,
    SIGNAL_ALARM_INPUT,
    SIGNAL_ALARM_OUTPUT,
    SIGNAL_FACE_AVAILABILITY,
    SIGNAL_FACE_CLEAR,
    SIGNAL_FACE_EVENT,
    SIGNAL_FACE_SNAPSHOT,
)
from .features import (
    FEATURE_AUDIO_PLAYBACK,
    FEATURE_FACE,
    FEATURE_FACE_CODES,
    FEATURE_PRIVACY_MASK,
    FEATURE_PTZ,
    FEATURE_REGISTRY,
    FEATURE_SIREN,
    FEATURE_WHITE_LIGHT,
    codes_for_features,
)
from .button import build_button_entities
from .image import build_image_entities
from .select import build_select_entities
from .sensor import build_sensor_entities
from .switch import build_switch_entities
from .text import build_text_entities

_LOGGER = logging.getLogger(__name__)

PLATFORMS = ["sensor", "image", "switch", "select", "button", "text", "binary_sensor"]


def _camera_features(entry: ConfigEntry) -> dict[str, list[str]]:
    """{channel_str: [feature_key, ...]} with a backward-compatible
    default of ["Face"] for any camera predating the feature picker."""
    cameras: dict[str, str] = entry.data.get(CONF_CAMERAS, {})
    stored: dict[str, list[str]] = entry.data.get(CONF_CAMERA_FEATURES, {})
    return {ch: stored.get(ch, [FEATURE_FACE]) for ch in cameras}


def _device_info(entry: ConfigEntry, channel: int, name: str) -> DeviceInfo:
    return DeviceInfo(
        identifiers={(DOMAIN, f"{entry.entry_id}_{channel}")},
        name=name,
        manufacturer="Dahua",
        model="NVR Camera Channel",
        via_device=(DOMAIN, entry.entry_id),
    )


def _feature_key_for_unique_id(unique_id: str) -> str | None:
    """Recover which feature key (FEATURE_FACE, FEATURE_WHITE_LIGHT,
    FEATURE_SIREN, FEATURE_PTZ, FEATURE_AUDIO_PLAYBACK,
    FEATURE_PRIVACY_MASK, or a FEATURE_REGISTRY key) an entity's
    unique_id belongs to, from its suffix convention:
    "{entry_id}_{channel}_face_..." (including "..._face_snapshot")
    belongs to FEATURE_FACE; "..._white_light" / "..._siren" /
    "..._privacy_mask" belong to their matching pseudo feature;
    "..._ptz_{code}" / "..._ptz_goto_preset" / "..._ptz_set_preset"
    belong to FEATURE_PTZ; "..._audio_path" / "..._audio_file_name" /
    "..._play_audio_file" belong to FEATURE_AUDIO_PLAYBACK;
    "..._ivs_{code_lower}" belongs to whichever FEATURE_REGISTRY entry
    has that code. "..._armed" deliberately maps to nothing — it's
    channel-wide, not tied to any one feature, so it's only ever removed
    via full camera/device removal, never a single-feature toggle.
    entry_id (a HA-generated hex string) never contains underscores, so
    this split is unambiguous.
    """
    parts = unique_id.split("_", 2)
    if len(parts) < 3:
        return None
    suffix = parts[2]
    if suffix.startswith("face_"):
        return FEATURE_FACE
    if suffix == "white_light":
        return FEATURE_WHITE_LIGHT
    if suffix == "siren":
        return FEATURE_SIREN
    if suffix == "privacy_mask":
        return FEATURE_PRIVACY_MASK
    if suffix.startswith("ptz_"):
        return FEATURE_PTZ
    if suffix in ("audio_path", "audio_file_name", "play_audio_file"):
        return FEATURE_AUDIO_PLAYBACK
    if suffix.startswith("ivs_"):
        code_lower = suffix[len("ivs_") :]
        for key, feature in FEATURE_REGISTRY.items():
            if feature.code.lower() == code_lower:
                return key
    return None


SERVICE_PLAY_AUDIO_FILE = "play_audio_file"
PLAY_AUDIO_FILE_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_DEVICE_ID): vol.All(cv.ensure_list, [cv.string]),
        vol.Optional("path", default="/Audio_Path"): cv.string,
        vol.Required("file_name"): cv.string,
        vol.Optional("play_times", default=1): vol.All(
            vol.Coerce(int), vol.Range(min=1, max=10)
        ),
    }
)

SERVICE_SET_CUSTOM_OVERLAY = "set_custom_overlay"
SET_CUSTOM_OVERLAY_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_DEVICE_ID): vol.All(cv.ensure_list, [cv.string]),
        vol.Required("lines"): vol.All(
            cv.ensure_list, [cv.string], vol.Length(max=OVERLAY_MAX_LINES)
        ),
    }
)


def _entry_and_channel_for_device(device) -> tuple[str | None, int | None]:
    """Recover (entry_id, channel) from a camera device's identifiers
    (DOMAIN, f"{entry_id}_{channel}"). Returns (None, None) for the NVR
    hub device itself (identifier has no "_{channel}" suffix — entry_id
    is a HA-generated hex string with no underscores of its own) or any
    device this integration didn't create."""
    for domain, identifier in device.identifiers:
        if domain != DOMAIN:
            continue
        entry_id, sep, channel_str = identifier.rpartition("_")
        if not sep or not channel_str.isdigit():
            continue
        return entry_id, int(channel_str)
    return None, None


async def async_setup(hass: HomeAssistant, config: dict) -> bool:
    """Register services shared across every config entry (called once,
    regardless of how many NVRs/cameras are configured)."""

    async def _handle_play_audio_file(call: ServiceCall) -> None:
        # NOTE: the underlying Dahua API for this action
        # (/cgi-bin/api/Speak/startPlayByTime) has no per-channel
        # targeting at all — the target device just picks which
        # NVR/camera connection sends the request; on an NVR it plays
        # through whatever the device itself resolves it to, not
        # necessarily a specific camera's speaker. See the README.
        device_ids: list[str] = call.data[ATTR_DEVICE_ID]
        path: str = call.data["path"]
        file_name: str = call.data["file_name"]
        play_times: int = call.data["play_times"]

        device_registry = dr.async_get(hass)
        notified_entries: set[str] = set()
        for device_id in device_ids:
            device = device_registry.async_get(device_id)
            if device is None:
                continue
            entry_id = next(iter(device.config_entries), None)
            if entry_id is None or entry_id in notified_entries:
                continue
            store = hass.data.get(DOMAIN, {}).get(entry_id)
            if store is None:
                continue
            notified_entries.add(entry_id)
            client: DahuaClient = store["client"]
            try:
                await client.play_audio_file(path, file_name, play_times)
            except (DahuaAuthError, DahuaConnectionError) as err:
                raise HomeAssistantError(
                    f"Could not play audio file {file_name!r}: {err}"
                ) from err

        if not notified_entries:
            raise HomeAssistantError(
                "No Dahua NVR/camera found for the selected device(s)"
            )

    async def _handle_set_custom_overlay(call: ServiceCall) -> None:
        device_ids: list[str] = call.data[ATTR_DEVICE_ID]
        lines: list[str] = call.data["lines"]

        device_registry = dr.async_get(hass)
        acted_on = 0
        for device_id in device_ids:
            device = device_registry.async_get(device_id)
            if device is None:
                continue
            entry_id, channel = _entry_and_channel_for_device(device)
            if entry_id is None:
                # Either an unrelated device, or the NVR hub device
                # itself — overlay text is per-camera, so there's
                # nothing meaningful to do with the hub device.
                continue
            store = hass.data.get(DOMAIN, {}).get(entry_id)
            if store is None:
                continue
            client: DahuaClient = store["client"]
            try:
                await client.set_overlay_lines(channel, lines)
            except (DahuaAuthError, DahuaConnectionError, ValueError) as err:
                raise HomeAssistantError(
                    f"Could not set custom overlay: {err}"
                ) from err
            acted_on += 1

        if not acted_on:
            raise HomeAssistantError(
                "No Dahua camera found for the selected device(s) — select "
                "a specific camera, not the NVR itself"
            )

    hass.services.async_register(
        DOMAIN,
        SERVICE_PLAY_AUDIO_FILE,
        _handle_play_audio_file,
        schema=PLAY_AUDIO_FILE_SCHEMA,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_SET_CUSTOM_OVERLAY,
        _handle_set_custom_overlay,
        schema=SET_CUSTOM_OVERLAY_SCHEMA,
    )
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Dahua Face Recognition from a config entry."""
    session = async_get_clientsession(hass, verify_ssl=False)

    client = DahuaClient(
        host=entry.data[CONF_HOST],
        port=entry.data.get(CONF_PORT, 80),
        username=entry.data[CONF_USERNAME],
        password=entry.data[CONF_PASSWORD],
        session=session,
    )

    cameras: dict[str, str] = entry.data.get(CONF_CAMERAS, {})
    if not cameras:
        _LOGGER.error("No cameras configured for entry %s", entry.entry_id)
        return False

    # Every camera device declares via_device=(DOMAIN, entry.entry_id) —
    # that only works if a device with that exact identifier actually
    # exists. Register the NVR itself as that parent/hub device
    # explicitly, rather than relying on some entity happening to create
    # it as a side effect (which was never happening, hence the
    # "referencing a non existing 'via_device'" warning).
    device_registry = dr.async_get(hass)
    device_registry.async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, entry.entry_id)},
        manufacturer="Dahua",
        name=f"Dahua NVR ({entry.data[CONF_HOST]})",
        model="NVR",
    )

    # Device-wide alarm input/output (relay) capability, probed once at
    # setup — alarm.cgi has no channel parameter at all, so unlike
    # every other feature this isn't part of the per-camera picker; it
    # just appears automatically on the NVR hub device if the device
    # reports at least one slot. See binary_sensor.py / switch.py.
    try:
        alarm_in_count = await client.get_alarm_in_slots()
    except (DahuaAuthError, DahuaConnectionError) as err:
        _LOGGER.debug("Alarm input slot probe failed: %s", err)
        alarm_in_count = 0
    try:
        alarm_out_count = await client.get_alarm_out_slots()
    except (DahuaAuthError, DahuaConnectionError) as err:
        _LOGGER.debug("Alarm output slot probe failed: %s", err)
        alarm_out_count = 0

    camera_features = _camera_features(entry)
    stop_event = asyncio.Event()

    # One shared connection covers every selected channel on this NVR;
    # events are routed to the right camera client-side using each event's
    # own "index" (channel) field. This is deliberate: some Dahua firmware
    # does not actually honor a per-connection channel filter and instead
    # broadcasts every channel's events down every attach connection, which
    # previously caused every camera's sensors to show identical values.
    task = hass.loop.create_task(
        _listen_forever(
            hass,
            entry,
            client,
            cameras,
            camera_features,
            alarm_in_count,
            alarm_out_count,
            stop_event,
        )
    )

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = {
        "client": client,
        "stop_event": stop_event,
        "tasks": [task],
        "cameras": cameras,
        "camera_features": camera_features,
        "alarm_in_count": alarm_in_count,
        "alarm_out_count": alarm_out_count,
        # populated by each platform's async_setup_entry; used by
        # _async_entry_updated to add entities for a newly added camera
        # or a newly enabled feature without reloading the whole entry.
        "add_entities": {},
    }

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    # Adding a camera or enabling a feature later (via the config flow's
    # "merge into existing NVR" path or the options flow) updates
    # entry.data and relies on this listener to add just the new
    # entities/devices live — never a full reload, so every other
    # camera's entities, names, and history are left completely alone.
    entry.async_on_unload(entry.add_update_listener(_async_entry_updated))

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    data = hass.data.get(DOMAIN, {}).pop(entry.entry_id, None)
    if data:
        data["stop_event"].set()
        for task in data["tasks"]:
            task.cancel()
        await asyncio.gather(*data["tasks"], return_exceptions=True)
    return unload_ok


async def _async_entry_updated(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Called after entry.data changes (camera list and/or per-camera
    features) — e.g. from the config flow's "add a camera to this NVR"
    path or the options flow. Adds only what's new, live: no platform
    unload/reload, so cameras that didn't change never go unavailable and
    never lose their entity registry names."""
    store = hass.data.get(DOMAIN, {}).get(entry.entry_id)
    if store is None:
        return

    old_cameras: dict[str, str] = store["cameras"]
    old_features: dict[str, list[str]] = store["camera_features"]
    new_cameras: dict[str, str] = entry.data.get(CONF_CAMERAS, {})
    new_features = _camera_features(entry)

    # A camera deselected in the config/options flow (rare, but the flows
    # allow it): clean up its device and entities explicitly. Nothing in
    # this integration polls or reconnects for it after this point, so an
    # unremoved device would otherwise sit forever showing stale
    # last-known values instead of disappearing like the user expects.
    removed_channels = set(old_cameras) - set(new_cameras)
    if removed_channels:
        device_registry = dr.async_get(hass)
        for ch in removed_channels:
            channel = int(ch)
            device = device_registry.async_get_device(
                identifiers={(DOMAIN, f"{entry.entry_id}_{channel}")}
            )
            if device is not None:
                device_registry.async_remove_device(device.id)
        _LOGGER.debug(
            "Removed device(s) for deselected camera(s) on entry %s: %s",
            entry.entry_id,
            sorted(removed_channels),
        )

    # A feature disabled on a camera that's still selected (not the whole
    # camera removed, handled above): remove just those entities so they
    # don't linger forever showing stale data after their code drops out
    # of the stream subscription.
    entity_registry = er.async_get(hass)
    entities_to_remove: list[str] = []
    for ch in new_cameras:
        if ch in removed_channels:
            continue  # already fully handled above
        removed_features = [
            f for f in old_features.get(ch, []) if f not in new_features.get(ch, [])
        ]
        if not removed_features:
            continue
        channel = int(ch)
        prefix = f"{entry.entry_id}_{channel}_"
        for entity_entry in er.async_entries_for_config_entry(
            entity_registry, entry.entry_id
        ):
            if not entity_entry.unique_id.startswith(prefix):
                continue
            feature_key = _feature_key_for_unique_id(entity_entry.unique_id)
            if feature_key in removed_features:
                entities_to_remove.append(entity_entry.entity_id)

    for entity_id in entities_to_remove:
        entity_registry.async_remove(entity_id)
    if entities_to_remove:
        _LOGGER.debug(
            "Removed entities for disabled feature(s) on entry %s: %s",
            entry.entry_id,
            entities_to_remove,
        )

    # channel -> feature keys that are newly enabled for it (a brand new
    # camera gets its whole enabled set; an existing camera gets only
    # whatever was added since last time, so we never build a duplicate
    # entity for a feature it already had).
    to_add: dict[str, list[str]] = {}
    for ch in new_cameras:
        if ch not in old_cameras:
            to_add[ch] = list(new_features.get(ch, []))
        else:
            added = [
                f for f in new_features.get(ch, []) if f not in old_features.get(ch, [])
            ]
            if added:
                to_add[ch] = added

    if to_add:
        add_entities = store.get("add_entities", {})
        client: DahuaClient = store["client"]
        for ch, feature_keys in to_add.items():
            channel = int(ch)
            name = new_cameras[ch]
            device_info = _device_info(entry, channel, name)

            if "sensor" in add_entities:
                new_sensors = build_sensor_entities(
                    entry, channel, name, device_info, feature_keys
                )
                if new_sensors:
                    add_entities["sensor"](new_sensors)

            if "image" in add_entities:
                new_images = build_image_entities(
                    hass, entry, channel, name, device_info, feature_keys
                )
                if new_images:
                    add_entities["image"](new_images)

            # The Armed switch is channel-wide, not feature-specific, so
            # it's only included the first time (brand-new camera);
            # everything else here is feature-gated and gets added
            # whenever its feature is newly enabled, on a new or
            # existing camera.
            is_new_camera = ch not in old_cameras

            if "switch" in add_entities:
                new_switches = build_switch_entities(
                    client,
                    entry,
                    channel,
                    name,
                    device_info,
                    feature_keys,
                    include_armed=is_new_camera,
                )
                if new_switches:
                    add_entities["switch"](new_switches, True)

            if "text" in add_entities:
                new_texts = build_text_entities(
                    entry, channel, name, device_info, feature_keys
                )
                if new_texts:
                    add_entities["text"](new_texts)

            if "select" in add_entities:
                new_selects = build_select_entities(
                    client, entry, channel, name, device_info, feature_keys
                )
                if new_selects:
                    add_entities["select"](new_selects, True)

            if "button" in add_entities:
                new_buttons = build_button_entities(
                    hass, client, entry, channel, name, device_info, feature_keys
                )
                if new_buttons:
                    add_entities["button"](new_buttons)

        _LOGGER.debug(
            "Added entities for new camera(s)/feature(s) on entry %s: %s",
            entry.entry_id,
            to_add,
        )

    old_codes: set[str] = set()
    for feats in old_features.values():
        old_codes |= codes_for_features(feats)
    new_codes: set[str] = set()
    for feats in new_features.values():
        new_codes |= codes_for_features(feats)

    if set(old_cameras) != set(new_cameras) or old_codes != new_codes:
        await _restart_listener(hass, entry, new_cameras, new_features)
    else:
        store["cameras"] = new_cameras
        store["camera_features"] = new_features


async def _restart_listener(
    hass: HomeAssistant,
    entry: ConfigEntry,
    cameras: dict[str, str],
    camera_features: dict[str, list[str]],
) -> None:
    """Swap in a new event-stream listener task covering the updated
    camera/feature set. The old task is cancelled — which short-circuits
    straight past the "mark unavailable" step in _listen_forever (see the
    `except asyncio.CancelledError: raise` there) — so cameras that were
    already being tracked keep showing their last-known state throughout,
    with at most a brief gap in new events while the new connection comes
    up, never an "unavailable" flicker."""
    store = hass.data[DOMAIN][entry.entry_id]
    old_stop_event: asyncio.Event = store["stop_event"]
    old_tasks: list[asyncio.Task] = store["tasks"]
    old_stop_event.set()
    for task in old_tasks:
        task.cancel()
    await asyncio.gather(*old_tasks, return_exceptions=True)

    new_stop_event = asyncio.Event()
    client: DahuaClient = store["client"]
    new_task = hass.loop.create_task(
        _listen_forever(
            hass,
            entry,
            client,
            cameras,
            camera_features,
            store.get("alarm_in_count", 0),
            store.get("alarm_out_count", 0),
            new_stop_event,
        )
    )
    store.update(
        {
            "stop_event": new_stop_event,
            "tasks": [new_task],
            "cameras": cameras,
            "camera_features": camera_features,
        }
    )


async def _listen_forever(
    hass: HomeAssistant,
    entry: ConfigEntry,
    client: DahuaClient,
    cameras: dict[str, str],
    camera_features: dict[str, list[str]],
    alarm_in_count: int,
    alarm_out_count: int,
    stop_event: asyncio.Event,
) -> None:
    """Keep the shared event stream for this NVR open, routing each event
    to the right camera by its own channel index, reconnecting with
    exponential backoff on failure, until stop_event is set."""
    delay = RECONNECT_DELAY_MIN
    channels = [int(c) for c in cameras]

    codes: set[str] = set()
    for feats in camera_features.values():
        codes |= codes_for_features(feats)
    # Local alarm input/output aren't part of the per-camera feature
    # picker (alarm.cgi has no channel parameter at all — see
    # binary_sensor.py/switch.py), but they use this same shared event
    # stream for push updates, so their codes are folded in here too.
    if alarm_in_count:
        codes.add("AlarmLocal")
    if alarm_out_count:
        codes.add("AlarmOutput")
    if not codes:
        _LOGGER.warning(
            "No features enabled on any camera for this NVR; nothing to "
            "subscribe to. Use the integration's Configure option to "
            "enable at least one feature per camera."
        )
        return

    def _set_availability(available: bool) -> None:
        for channel in channels:
            signal = SIGNAL_FACE_AVAILABILITY.format(
                entry_id=entry.entry_id, channel=channel
            )
            async_dispatcher_send(hass, signal, available)

    # Per-channel pending "revert Face sensors to Clear" timer. Dahua's
    # FaceRecognition event is a Pulse with no matching "stop" — so we
    # simulate one: every new face event (re)starts a FACE_CLEAR_TIMEOUT
    # countdown, cancelling whatever was pending. If it ever fires (no
    # face seen since), the Face sensors are told to go back to "Clear".
    face_clear_cancel: dict[int, callable] = {}

    def _fire_face_clear(channel: int, _now) -> None:
        face_clear_cancel.pop(channel, None)
        signal = SIGNAL_FACE_CLEAR.format(entry_id=entry.entry_id, channel=channel)
        async_dispatcher_send(hass, signal, None)

    def _schedule_face_clear(channel: int) -> None:
        cancel = face_clear_cancel.pop(channel, None)
        if cancel is not None:
            cancel()
        face_clear_cancel[channel] = async_call_later(
            hass, FACE_CLEAR_TIMEOUT, lambda now, ch=channel: _fire_face_clear(ch, now)
        )

    def _on_event(event: dict, snapshot_bytes: bytes | None) -> None:
        code = event.get("code")
        raw_index = event.get("index")
        if raw_index is None:
            _LOGGER.debug("Dropping event with no channel index: %s", event)
            return
        try:
            index = int(raw_index)
        except (TypeError, ValueError):
            _LOGGER.debug("Dropping event with unparseable index %r", raw_index)
            return

        if code in ("AlarmLocal", "AlarmOutput"):
            # Not camera-channel-scoped — `index` here is the alarm
            # input/output slot number (see api.py's get_alarm_in_slots
            # etc.), a completely different index space from the video
            # channels below.
            signal_template = (
                SIGNAL_ALARM_INPUT if code == "AlarmLocal" else SIGNAL_ALARM_OUTPUT
            )
            signal = signal_template.format(entry_id=entry.entry_id, index=index)
            async_dispatcher_send(hass, signal, event)
            return

        channel = index
        channel_str = str(channel)
        if channel_str not in cameras:
            # Event belongs to a channel on this NVR we didn't select —
            # ignore it (this is expected/normal on shared connections).
            return

        signal = SIGNAL_FACE_EVENT.format(entry_id=entry.entry_id, channel=channel)
        async_dispatcher_send(hass, signal, event)

        if code in FEATURE_FACE_CODES:
            _schedule_face_clear(channel)

        if snapshot_bytes:
            snapshot_signal = SIGNAL_FACE_SNAPSHOT.format(
                entry_id=entry.entry_id, channel=channel
            )
            async_dispatcher_send(hass, snapshot_signal, snapshot_bytes)

    try:
        while not stop_event.is_set():
            try:
                _LOGGER.debug(
                    "Connecting to Dahua event stream for channels %s", channels
                )
                _set_availability(True)
                delay = RECONNECT_DELAY_MIN  # reset backoff on a fresh attempt
                await client.listen_events(channels, codes, _on_event, stop_event)
            except DahuaAuthError:
                _LOGGER.error(
                    "Authentication failed for Dahua NVR; check username/password "
                    "in the integration options"
                )
            except (
                DahuaConnectionError,
                aiohttp.ClientError,
                asyncio.TimeoutError,
            ) as err:
                _LOGGER.warning(
                    "Dahua event stream disconnected (%s); reconnecting in %ss",
                    err,
                    delay,
                )
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - keep the listener alive
                _LOGGER.exception("Unexpected error in Dahua event stream")

            _set_availability(False)

            if stop_event.is_set():
                return

            jitter = random.uniform(0, 1)
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=delay + jitter)
            except asyncio.TimeoutError:
                pass
            delay = min(delay * 2, RECONNECT_DELAY_MAX)
    finally:
        for cancel in face_clear_cancel.values():
            cancel()
        face_clear_cancel.clear()
