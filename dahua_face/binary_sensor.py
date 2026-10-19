"""Binary sensor platform for Dahua Face Recognition.

Alarm input contacts, via alarm.cgi (§4.9.4/§4.9.6 of the API doc).
One entity per input channel the device reports (via getInSlots),
auto-created if the count is at least 1 — no per-camera feature
toggle, since alarm.cgi is device-wide (no channel parameter at all),
not tied to any specific camera. Attached to the NVR hub device rather
than any one camera's device for that reason.

State is primarily PUSH-based, via the "AlarmLocal" event on the same
shared event stream Face/IVS events already use (see __init__.py's
_listen_forever) — this is what gets these entities to near-real-time
responsiveness (matching the DMSS app / rroller/dahua) instead of
trailing behind by up to one poll interval. Polling (SCAN_INTERVAL)
is kept as a periodic reconciliation safety net — e.g. in case an
event was missed during a brief stream reconnect — not as the primary
update path.

IMPORTANT SCOPE NOTE: alarm.cgi's endpoints don't distinguish "local"
NVR alarm ports from "IPC EXT" (a connected camera's own alarm
terminals) — there's no separate, channel-scoped endpoint for the
latter documented anywhere I could find. It's possible the flat index
space these entities enumerate already includes both combined (a
common Dahua NVR web-UI convention groups them into separate tabs over
what may be one underlying list) — but this is unconfirmed. See the
README for what's worth checking on real hardware.
"""
from __future__ import annotations

import logging
from datetime import timedelta

from homeassistant.components.binary_sensor import BinarySensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .api import DahuaAuthError, DahuaClient, DahuaConnectionError
from .const import DOMAIN, SIGNAL_ALARM_INPUT

_LOGGER = logging.getLogger(__name__)

# Reconciliation-only now that push updates (via the event stream)
# handle real-time responsiveness — this just catches anything a
# missed/dropped event might have left stale.
SCAN_INTERVAL = timedelta(seconds=60)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up one binary sensor per alarm input channel the device
    reports, if any."""
    store = hass.data[DOMAIN][entry.entry_id]
    store.setdefault("add_entities", {})["binary_sensor"] = async_add_entities
    client: DahuaClient = store["client"]

    alarm_in_count: int = store.get("alarm_in_count", 0)
    if not alarm_in_count:
        return

    names: dict[int, str] = {}
    try:
        names = await client.get_alarm_in_names()
    except (DahuaAuthError, DahuaConnectionError) as err:
        _LOGGER.debug("Could not fetch alarm input names: %s", err)

    device_info = DeviceInfo(identifiers={(DOMAIN, entry.entry_id)})

    entities = [
        DahuaAlarmInputBinarySensor(client, entry, index, names.get(index), device_info)
        for index in range(alarm_in_count)
    ]
    async_add_entities(entities, True)


def _log_poll_error(
    logger: logging.Logger,
    already_logged: bool,
    message: str,
    *args,
) -> bool:
    """Log a polling failure at WARNING the first time, then DEBUG for
    as long as the identical condition keeps repeating on every
    subsequent poll — mirrors switch.py's helper of the same name."""
    if already_logged:
        logger.debug(message, *args)
    else:
        logger.warning(message, *args)
    return True


class DahuaAlarmInputBinarySensor(BinarySensorEntity):
    """Whether an alarm input channel is currently triggered."""

    _attr_should_poll = True
    _attr_icon = "mdi:electric-switch-closed"

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
        self._attr_unique_id = f"{entry.entry_id}_alarm_in_{index}"
        self._attr_name = name or f"Alarm Input {index + 1}"
        self._attr_is_on: bool | None = None
        self._attr_available = True
        self._logged_unsupported = False

    async def async_added_to_hass(self) -> None:
        signal = SIGNAL_ALARM_INPUT.format(
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
            active = await self._client.get_alarm_in_state()
        except DahuaAuthError:
            _LOGGER.error("Authentication failed reading alarm input state")
            self._attr_available = False
            return
        except DahuaConnectionError as err:
            self._logged_unsupported = _log_poll_error(
                _LOGGER,
                self._logged_unsupported,
                "Could not read alarm input state: %s",
                err,
            )
            self._attr_available = False
            return

        self._logged_unsupported = False

        self._attr_available = True
        self._attr_is_on = self._index in active
