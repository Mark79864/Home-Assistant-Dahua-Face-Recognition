"""Switch platform for Dahua Face Recognition.

Per-camera switches:

  * Armed — always created, regardless of enabled features (arming is
    channel-wide, not feature-specific). See DahuaArmedSwitch.
  * White / Security Light — created only if the "White / Security
    Light (TiOC)" feature is enabled. Controlled via
    coaxialControlIO.cgi's Type=1. IMPORTANT: on TiOC / active-
    deterrence camera models, this is the SAME physical output as the
    red/blue flashing warning light — there is no separate control for
    it. Which light actually responds (plain white spotlight vs.
    red/blue strobe) depends on your camera's hardware, not on
    anything this integration sends differently. Confirmed against the
    rroller/dahua Home Assistant integration's production
    implementation (https://github.com/rroller/dahua), which treats it
    as one control the same way.
  * Siren — created only if the "Siren (TiOC)" feature is enabled.
    Controlled via the same endpoint's Type=2. The API doc calls this
    "Speaker", but per the same rroller/dahua reference it behaves as
    a siren in practice: it triggers for roughly 10-15 seconds and the
    device turns itself back off — the 60s poll will pick up that
    auto-off on its next cycle.
  * Privacy Mask — created only if that feature is enabled.

Plus device-wide switches, one per alarm output (relay) channel the
NVR reports at all (via alarm.cgi's getOutSlots) — attached to the NVR
hub device rather than any camera, since alarm.cgi takes no channel
parameter. See DahuaAlarmOutputSwitch and binary_sensor.py's module
docstring for the local-vs-IPC-EXT scope caveat.

Both light and siren show unavailable rather than guessing if the
device doesn't report a status for that output at all (not every
camera/channel has this TiOC hardware).
"""
from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .api import DahuaAuthError, DahuaClient, DahuaConnectionError
from .const import CONF_CAMERA_FEATURES, CONF_CAMERAS, DOMAIN, SIGNAL_ALARM_OUTPUT
from .features import (
    FEATURE_FACE,
    FEATURE_PRIVACY_MASK,
    FEATURE_SIREN,
    FEATURE_WHITE_LIGHT,
)

_LOGGER = logging.getLogger(__name__)

SCAN_INTERVAL = timedelta(seconds=60)


def _log_poll_error(
    logger: logging.Logger,
    already_logged: bool,
    message: str,
    *args: Any,
) -> bool:
    """Log a polling failure at WARNING the first time, then DEBUG for
    as long as the identical condition keeps repeating on every
    subsequent poll — a stable/permanent error (unsupported hardware,
    a consistent 400/501, etc.) shouldn't repeat the same WARNING every
    poll cycle forever. Returns the new already_logged state (always
    True) for the caller to store.
    """
    if already_logged:
        logger.debug(message, *args)
    else:
        logger.warning(message, *args)
    return True


def build_switch_entities(
    client: DahuaClient,
    entry: ConfigEntry,
    channel: int,
    camera_name: str,
    device_info: DeviceInfo,
    feature_keys: list[str],
    *,
    include_armed: bool = False,
) -> list[SwitchEntity]:
    """Entities for one camera given its currently-enabled feature keys.

    include_armed controls whether the (feature-independent) Armed
    switch is included — pass True only the first time a camera is set
    up (initial platform setup, or the moment it's newly added live),
    never when just adding a newly-enabled feature to a camera that
    already has its Armed switch, or it would be built a second time.
    """
    entities: list[SwitchEntity] = []
    if include_armed:
        entities.append(DahuaArmedSwitch(client, entry, channel, camera_name, device_info))
    if FEATURE_WHITE_LIGHT in feature_keys:
        entities.append(
            DahuaWhiteLightSwitch(client, entry, channel, camera_name, device_info)
        )
    if FEATURE_SIREN in feature_keys:
        entities.append(
            DahuaSirenSwitch(client, entry, channel, camera_name, device_info)
        )
    if FEATURE_PRIVACY_MASK in feature_keys:
        entities.append(
            DahuaPrivacyMaskSwitch(client, entry, channel, camera_name, device_info)
        )
    return entities


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up switches for every configured camera."""
    store = hass.data[DOMAIN][entry.entry_id]
    store.setdefault("add_entities", {})["switch"] = async_add_entities
    client: DahuaClient = store["client"]

    cameras: dict[str, str] = entry.data.get(CONF_CAMERAS, {})
    camera_features: dict[str, list[str]] = entry.data.get(CONF_CAMERA_FEATURES, {})

    entities: list[SwitchEntity] = []
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
            build_switch_entities(
                client, entry, channel, name, device_info, enabled, include_armed=True
            )
        )

    # Device-wide alarm output (relay) switches — not tied to any
    # camera, since alarm.cgi takes no channel parameter.
    alarm_out_count: int = store.get("alarm_out_count", 0)
    if alarm_out_count:
        names: dict[int, str] = {}
        try:
            names = await client.get_alarm_out_names()
        except (DahuaAuthError, DahuaConnectionError) as err:
            _LOGGER.debug("Could not fetch alarm output names: %s", err)
        hub_device_info = DeviceInfo(identifiers={(DOMAIN, entry.entry_id)})
        entities.extend(
            DahuaAlarmOutputSwitch(client, entry, index, names.get(index), hub_device_info)
            for index in range(alarm_out_count)
        )

    # update_before_add=True: poll each switch's real state once before it
    # first appears, so it shows the NVR's actual current state rather
    # than a guess.
    async_add_entities(entities, True)


class DahuaArmedSwitch(SwitchEntity):
    """Whether smart-event linkage (alarms/AI events) is currently armed
    for this camera's channel, via configManager.cgi's DisableLinkage."""

    _attr_should_poll = True
    _attr_icon = "mdi:shield-lock-outline"

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
        self._attr_unique_id = f"{entry.entry_id}_{channel}_armed"
        self._attr_is_on: bool | None = None
        self._attr_available = True
        # See _CoaxialOutputSwitch — same one-warning-then-debug pattern
        # for a permanent 501 (endpoint not implemented on this channel).
        self._logged_unsupported = False

    @property
    def name(self) -> str:
        return f"{self._camera_name} Armed"

    async def async_update(self) -> None:
        try:
            disabled = await self._client.get_disable_linkage(self._channel)
        except DahuaAuthError:
            _LOGGER.error(
                "Authentication failed reading arm state for channel %s",
                self._channel,
            )
            self._attr_available = False
            return
        except DahuaConnectionError as err:
            self._logged_unsupported = _log_poll_error(
                _LOGGER,
                self._logged_unsupported,
                "Could not read arm state for channel %s: %s",
                self._channel,
                err,
            )
            self._attr_available = False
            return

        self._logged_unsupported = False
        if disabled is None:
            # This channel doesn't report a DisableLinkage value at all —
            # not every camera/channel supports it (it's a TiOC feature).
            self._attr_available = False
            return

        self._attr_available = True
        self._attr_is_on = not disabled

    async def async_turn_on(self, **kwargs: Any) -> None:
        try:
            await self._client.set_disable_linkage(self._channel, disable=False)
        except (DahuaAuthError, DahuaConnectionError) as err:
            raise HomeAssistantError(
                f"Could not arm channel {self._channel}: {err}"
            ) from err
        self._attr_is_on = True
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs: Any) -> None:
        try:
            await self._client.set_disable_linkage(self._channel, disable=True)
        except (DahuaAuthError, DahuaConnectionError) as err:
            raise HomeAssistantError(
                f"Could not disarm channel {self._channel}: {err}"
            ) from err
        self._attr_is_on = False
        self.async_write_ha_state()


class _CoaxialOutputSwitch(SwitchEntity):
    """Shared logic for the White/Security Light and Siren switches —
    both are controlled via coaxialControlIO.cgi, differing only in the
    `Type` value and which status key to read. Subclasses set
    _output_type, _status_key, and _unique_id_suffix as class
    attributes."""

    _attr_should_poll = True
    _output_type: int  # 1 = white/security light, 2 = siren
    _status_key: str  # "whitelight" or "speaker"
    _unique_id_suffix: str

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
        self._attr_unique_id = f"{entry.entry_id}_{channel}_{self._unique_id_suffix}"
        self._attr_is_on: bool | None = None
        self._attr_available = True
        # Once a poll comes back 501 ("Not Implemented"), that's a
        # permanent fact about this device/channel, not a transient
        # blip — log it at WARNING once so it's visible, then drop to
        # DEBUG for the identical failure on every subsequent poll
        # instead of repeating the same warning forever.
        self._logged_unsupported = False

    async def async_update(self) -> None:
        try:
            status = await self._client.get_coaxial_status(self._channel + 1)
        except DahuaAuthError:
            _LOGGER.error(
                "Authentication failed reading coaxial status for channel %s",
                self._channel,
            )
            self._attr_available = False
            return
        except DahuaConnectionError as err:
            self._logged_unsupported = _log_poll_error(
                _LOGGER,
                self._logged_unsupported,
                "Could not read coaxial status for channel %s (%s): %s",
                self._channel,
                self.name,
                err,
            )
            self._attr_available = False
            return

        self._logged_unsupported = False
        value = status.get(self._status_key)
        if value is None:
            # Device didn't report this output at all for this channel —
            # likely no such hardware here (e.g. no white/security light).
            self._attr_available = False
            return

        self._attr_available = True
        self._attr_is_on = value == "on"

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self._set(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._set(False)

    async def _set(self, turn_on: bool) -> None:
        try:
            await self._client.set_coaxial_output(
                self._channel + 1, self._output_type, turn_on
            )
        except (DahuaAuthError, DahuaConnectionError) as err:
            action = "on" if turn_on else "off"
            raise HomeAssistantError(
                f"Could not turn {action} {self.name} (channel {self._channel}): {err}"
            ) from err
        self._attr_is_on = turn_on
        self.async_write_ha_state()


class DahuaWhiteLightSwitch(_CoaxialOutputSwitch):
    """The camera's white/security light (TiOC active deterrence).

    On camera models with a red/blue flashing warning light instead of
    a plain white spotlight, this same switch controls that light — see
    the module docstring.
    """

    _attr_icon = "mdi:flashlight"
    _output_type = 1
    _status_key = "whitelight"
    _unique_id_suffix = "white_light"

    @property
    def name(self) -> str:
        return f"{self._camera_name} White/Security Light"


class DahuaSirenSwitch(_CoaxialOutputSwitch):
    """The camera's siren (TiOC active deterrence). Auto-off after
    roughly 10-15 seconds — the next 60s poll will reflect that."""

    _attr_icon = "mdi:bullhorn-outline"
    _output_type = 2
    _status_key = "speaker"
    _unique_id_suffix = "siren"

    @property
    def name(self) -> str:
        return f"{self._camera_name} Siren"


class DahuaPrivacyMaskSwitch(SwitchEntity):
    """Whether privacy masking is enabled for this camera's channel —
    a single master on/off for whatever mask shapes are already
    configured (via the camera's own web UI); this switch doesn't
    create/edit/delete individual shapes."""

    _attr_should_poll = True
    _attr_icon = "mdi:eye-off-outline"

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
        self._attr_unique_id = f"{entry.entry_id}_{channel}_privacy_mask"
        self._attr_is_on: bool | None = None
        self._attr_available = True
        self._logged_unsupported = False

    @property
    def name(self) -> str:
        return f"{self._camera_name} Privacy Mask"

    async def async_update(self) -> None:
        try:
            enabled = await self._client.get_privacy_mask_enabled(self._channel + 1)
        except DahuaAuthError:
            _LOGGER.error(
                "Authentication failed reading privacy mask state for channel %s",
                self._channel,
            )
            self._attr_available = False
            return
        except DahuaConnectionError as err:
            self._logged_unsupported = _log_poll_error(
                _LOGGER,
                self._logged_unsupported,
                "Could not read privacy mask state for channel %s: %s",
                self._channel,
                err,
            )
            self._attr_available = False
            return

        self._logged_unsupported = False
        if enabled is None:
            self._attr_available = False
            return

        self._attr_available = True
        self._attr_is_on = enabled

    async def async_turn_on(self, **kwargs: Any) -> None:
        try:
            await self._client.set_privacy_mask_enabled(self._channel + 1, True)
        except (DahuaAuthError, DahuaConnectionError) as err:
            raise HomeAssistantError(
                f"Could not enable privacy mask on channel {self._channel}: {err}"
            ) from err
        self._attr_is_on = True
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs: Any) -> None:
        try:
            await self._client.set_privacy_mask_enabled(self._channel + 1, False)
        except (DahuaAuthError, DahuaConnectionError) as err:
            raise HomeAssistantError(
                f"Could not disable privacy mask on channel {self._channel}: {err}"
            ) from err
        self._attr_is_on = False
        self.async_write_ha_state()


class DahuaAlarmOutputSwitch(SwitchEntity):
    """A local (or IPC-EXT — see binary_sensor.py's module docstring
    for the scope caveat) alarm output / relay channel on the NVR
    itself, not tied to any specific camera. Controlled via
    configManager.cgi's AlarmOut[N].Mode: 1 = force on, 2 = force off
    (0 = automatic/rule-driven, not exposed here — turning the switch
    off always means "force closed", not "hand control back to
    automatic rules").

    State is primarily PUSH-based, via the "AlarmOutput" event on the
    same shared event stream Face/IVS events already use (see
    __init__.py's _listen_forever) — near-real-time responsiveness
    instead of trailing behind by up to one poll interval. Polling
    (SCAN_INTERVAL) is kept as a periodic reconciliation safety net.
    """

    _attr_should_poll = True
    _attr_icon = "mdi:electric-switch"

    def __init__(
        self,
        client: DahuaClient,
        entry: ConfigEntry,
        index: int,
        name: str | None,
        device_info: DeviceInfo,
    ) -> None:
        self._client = client
        self._entry = entry
        self._index = index
        self._attr_device_info = device_info
        self._attr_unique_id = f"{entry.entry_id}_alarm_out_{index}"
        self._attr_name = name or f"Alarm Output {index + 1}"
        self._attr_is_on: bool | None = None
        self._attr_available = True
        self._logged_unsupported = False

    async def async_added_to_hass(self) -> None:
        signal = SIGNAL_ALARM_OUTPUT.format(
            entry_id=self._entry.entry_id, index=self._index
        )
        self.async_on_remove(
            async_dispatcher_connect(self.hass, signal, self._handle_event)
        )

    @callback
    def _handle_event(self, event: dict) -> None:
        action = (event.get("action") or "").strip().lower()
        if action == "start":
            self._attr_is_on = True
        elif action == "stop":
            self._attr_is_on = False
        else:
            return
        self._attr_available = True
        self._logged_unsupported = False
        self.async_write_ha_state()

    async def async_update(self) -> None:
        try:
            active = await self._client.get_alarm_out_state()
        except DahuaAuthError:
            _LOGGER.error("Authentication failed reading alarm output state")
            self._attr_available = False
            return
        except DahuaConnectionError as err:
            self._logged_unsupported = _log_poll_error(
                _LOGGER,
                self._logged_unsupported,
                "Could not read alarm output state: %s",
                err,
            )
            self._attr_available = False
            return

        self._logged_unsupported = False
        self._attr_available = True
        self._attr_is_on = self._index in active

    async def async_turn_on(self, **kwargs: Any) -> None:
        try:
            await self._client.set_alarm_out_mode(self._index, 1)
        except (DahuaAuthError, DahuaConnectionError) as err:
            raise HomeAssistantError(
                f"Could not turn on alarm output {self._index}: {err}"
            ) from err
        self._attr_is_on = True
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs: Any) -> None:
        try:
            await self._client.set_alarm_out_mode(self._index, 2)
        except (DahuaAuthError, DahuaConnectionError) as err:
            raise HomeAssistantError(
                f"Could not turn off alarm output {self._index}: {err}"
            ) from err
        self._attr_is_on = False
        self.async_write_ha_state()
