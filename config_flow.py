"""Config flow for Dahua Face Recognition."""
from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.const import CONF_HOST, CONF_PASSWORD, CONF_PORT, CONF_USERNAME
from homeassistant.core import callback
from homeassistant.data_entry_flow import FlowResult
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import DahuaAuthError, DahuaClient, DahuaConnectionError
from .const import CONF_CAMERA_FEATURES, CONF_CAMERAS, DEFAULT_PORT, DOMAIN
from .features import feature_options

_LOGGER = logging.getLogger(__name__)

STEP_USER_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_HOST): str,
        vol.Required(CONF_PORT, default=DEFAULT_PORT): int,
        vol.Required(CONF_USERNAME): str,
        vol.Required(CONF_PASSWORD): str,
    }
)


def cv_multi_select(options: dict[str, str]):
    """Small local wrapper so we don't need to import homeassistant.helpers
    .config_validation just for multi_select (kept explicit for clarity)."""
    from homeassistant.helpers import config_validation as cv

    return cv.multi_select(options)


async def _detect_default_features(
    client: DahuaClient, channel_str: str
) -> list[str]:
    """Best-effort auto-detection of sensible pre-selected defaults for a
    camera that's never been configured before, using DahuaClient
    .probe_camera_capabilities. Every option in the picker — including
    Face Detection & Recognition — is only pre-checked if actually
    detected as supported on that channel; nothing is defaulted on
    blindly (a camera without face-recognition search support won't
    have Face pre-ticked, matching the same logic already applied to
    White Light / Siren / PTZ / Privacy Mask).

    Never raises — a total probe failure (rare; basic connectivity was
    already confirmed earlier in the flow) falls back to an empty list
    rather than guessing, consistent with "don't default what isn't
    confirmed"; the "select at least one feature" validation on the
    page that follows means this never silently skips the camera, it
    just requires the user to pick manually in the rare case nothing
    could be probed.
    """
    try:
        detected = await client.probe_camera_capabilities(int(channel_str))
    except Exception:  # noqa: BLE001 - a failed probe must never block setup
        _LOGGER.debug(
            "Capability probe failed for channel %s", channel_str, exc_info=True
        )
        return []
    return sorted(detected)


class DahuaFaceConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Dahua Face Recognition."""

    VERSION = 1

    def __init__(self) -> None:
        self._conn_data: dict[str, Any] = {}
        self._available_channels: dict[int, str] = {}
        self._existing_entry: config_entries.ConfigEntry | None = None
        # Camera picker result and the per-camera feature wizard state.
        self._pending_cameras: dict[str, str] = {}
        self._existing_features: dict[str, list[str]] = {}
        self._feature_selection: dict[str, list[str]] = {}
        self._remaining_channels: list[str] = []
        # Reused across every async_step_features call in this flow (not
        # recreated per channel) so the client's NVR-wide "coaxial
        # control unsupported" cache (see api.py) actually helps: once
        # one channel confirms the endpoint isn't implemented, every
        # other channel's probe during the same setup short-circuits
        # instead of repeating a request already known to fail.
        self._client: DahuaClient | None = None

    def _find_existing_entry(
        self, host: str, port: int
    ) -> config_entries.ConfigEntry | None:
        """An NVR is one config entry with up to 64 camera channels. If the
        host/port entered already belongs to a configured entry, we merge
        newly-selected cameras into it instead of trying to create a
        second, colliding entry."""
        for entry in self._async_current_entries():
            if entry.data.get(CONF_HOST) == host and entry.data.get(CONF_PORT) == port:
                return entry
        return None

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        errors: dict[str, str] = {}

        if user_input is not None:
            session = async_get_clientsession(self.hass, verify_ssl=False)
            client = DahuaClient(
                host=user_input[CONF_HOST],
                port=user_input[CONF_PORT],
                username=user_input[CONF_USERNAME],
                password=user_input[CONF_PASSWORD],
                session=session,
            )
            try:
                await client.test_connection()
                channels = await client.get_channels()
            except DahuaAuthError:
                errors["base"] = "invalid_auth"
            except DahuaConnectionError:
                errors["base"] = "cannot_connect"
            except Exception:  # noqa: BLE001
                _LOGGER.exception("Unexpected error validating Dahua connection")
                errors["base"] = "unknown"
            else:
                self._client = client
                self._existing_entry = self._find_existing_entry(
                    user_input[CONF_HOST], user_input[CONF_PORT]
                )
                # Only claim the unique_id for genuinely new entries; if we
                # found an existing entry we're merging into it instead, so
                # the (already-registered) unique_id is expected here.
                if self._existing_entry is None:
                    await self.async_set_unique_id(
                        f"{user_input[CONF_HOST]}:{user_input[CONF_PORT]}"
                    )
                    self._abort_if_unique_id_configured()
                self._conn_data = user_input
                self._available_channels = channels
                return await self.async_step_cameras()

        return self.async_show_form(
            step_id="user", data_schema=STEP_USER_SCHEMA, errors=errors
        )

    async def async_step_cameras(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        errors: dict[str, str] = {}
        options = {
            str(idx): name for idx, name in sorted(self._available_channels.items())
        }
        already_configured: dict[str, str] = {}
        if self._existing_entry is not None:
            already_configured = self._existing_entry.data.get(CONF_CAMERAS, {})
        default_selection = list(already_configured.keys())

        if user_input is not None:
            selected = user_input.get(CONF_CAMERAS, [])
            if not selected:
                errors["base"] = "no_cameras_selected"
            elif len(selected) > 64:
                errors["base"] = "too_many_cameras"
            else:
                self._pending_cameras = {idx: options[idx] for idx in selected}
                self._existing_features = (
                    self._existing_entry.data.get(CONF_CAMERA_FEATURES, {})
                    if self._existing_entry is not None
                    else {}
                )
                self._feature_selection = {}
                self._remaining_channels = list(self._pending_cameras.keys())
                return await self.async_step_features()

        schema = vol.Schema(
            {
                vol.Required(
                    CONF_CAMERAS, default=default_selection
                ): cv_multi_select(options)
            }
        )

        return self.async_show_form(
            step_id="cameras",
            data_schema=schema,
            errors=errors,
            description_placeholders={
                "already_configured": ", ".join(already_configured.values()) or "none"
            },
        )

    async def async_step_features(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Ask which features to enable, one camera at a time — so each
        page can clearly say which camera it's asking about."""
        errors: dict[str, str] = {}

        if user_input is not None:
            selected = user_input.get("features", [])
            if not selected:
                errors["base"] = "no_features_selected"
            else:
                current_channel = self._remaining_channels[0]
                self._feature_selection[current_channel] = selected
                self._remaining_channels.pop(0)
                if not self._remaining_channels:
                    return self._finalize()

        if not self._remaining_channels:
            return self._finalize()

        current_channel = self._remaining_channels[0]
        if current_channel in self._existing_features:
            default = self._existing_features[current_channel]
        elif self._client is not None:
            default = await _detect_default_features(self._client, current_channel)
        else:
            default = []
        schema = vol.Schema(
            {
                vol.Required("features", default=default): cv_multi_select(
                    feature_options()
                )
            }
        )
        return self.async_show_form(
            step_id="features",
            data_schema=schema,
            errors=errors,
            description_placeholders={
                "camera_name": self._pending_cameras[current_channel],
                "channel": current_channel,
            },
        )

    def _finalize(self) -> FlowResult:
        if self._existing_entry is not None:
            new_data = {
                **self._existing_entry.data,
                CONF_CAMERAS: self._pending_cameras,
                CONF_CAMERA_FEATURES: self._feature_selection,
            }
            # No reload here: updating entry.data fires the registered
            # update listener (see __init__.py's _async_entry_updated),
            # which adds the new camera's/feature's entities live and
            # only restarts the shared event stream — every other
            # camera's entities, availability, and history are left
            # completely untouched.
            self.hass.config_entries.async_update_entry(
                self._existing_entry, data=new_data
            )
            return self.async_abort(reason="reconfigure_successful")

        data = {
            **self._conn_data,
            CONF_CAMERAS: self._pending_cameras,
            CONF_CAMERA_FEATURES: self._feature_selection,
        }
        return self.async_create_entry(
            title=f"Dahua NVR ({self._conn_data[CONF_HOST]})", data=data
        )

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: config_entries.ConfigEntry,
    ) -> "DahuaFaceOptionsFlow":
        return DahuaFaceOptionsFlow(config_entry)


class DahuaFaceOptionsFlow(config_entries.OptionsFlow):
    """Allow changing which cameras are tracked, and which features are
    enabled per camera, after setup."""

    def __init__(self, config_entry: config_entries.ConfigEntry) -> None:
        self.config_entry = config_entry
        self._available_channels: dict[int, str] = {}
        self._pending_cameras: dict[str, str] = {}
        self._existing_features: dict[str, list[str]] = {}
        self._feature_selection: dict[str, list[str]] = {}
        self._remaining_channels: list[str] = []
        self._client: DahuaClient | None = None

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        errors: dict[str, str] = {}
        data = self.config_entry.data
        session = async_get_clientsession(self.hass, verify_ssl=False)
        client = DahuaClient(
            host=data[CONF_HOST],
            port=data.get(CONF_PORT, DEFAULT_PORT),
            username=data[CONF_USERNAME],
            password=data[CONF_PASSWORD],
            session=session,
        )
        self._client = client

        if not self._available_channels:
            try:
                self._available_channels = await client.get_channels()
            except (DahuaAuthError, DahuaConnectionError):
                self._available_channels = {
                    int(k): v for k, v in data.get(CONF_CAMERAS, {}).items()
                }

        options = {
            str(idx): name for idx, name in sorted(self._available_channels.items())
        }
        current = list(data.get(CONF_CAMERAS, {}).keys())

        if user_input is not None:
            selected = user_input.get(CONF_CAMERAS, [])
            if not selected:
                errors["base"] = "no_cameras_selected"
            elif len(selected) > 64:
                errors["base"] = "too_many_cameras"
            else:
                self._pending_cameras = {idx: options[idx] for idx in selected}
                self._existing_features = data.get(CONF_CAMERA_FEATURES, {})
                self._feature_selection = {}
                self._remaining_channels = list(self._pending_cameras.keys())
                return await self.async_step_features()

        schema = vol.Schema(
            {
                vol.Required(
                    CONF_CAMERAS, default=current
                ): cv_multi_select(options)
            }
        )
        return self.async_show_form(
            step_id="init", data_schema=schema, errors=errors
        )

    async def async_step_features(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        errors: dict[str, str] = {}

        if user_input is not None:
            selected = user_input.get("features", [])
            if not selected:
                errors["base"] = "no_features_selected"
            else:
                current_channel = self._remaining_channels[0]
                self._feature_selection[current_channel] = selected
                self._remaining_channels.pop(0)
                if not self._remaining_channels:
                    return self._finalize()

        if not self._remaining_channels:
            return self._finalize()

        current_channel = self._remaining_channels[0]
        if current_channel in self._existing_features:
            default = self._existing_features[current_channel]
        elif self._client is not None:
            default = await _detect_default_features(self._client, current_channel)
        else:
            default = []
        schema = vol.Schema(
            {
                vol.Required("features", default=default): cv_multi_select(
                    feature_options()
                )
            }
        )
        return self.async_show_form(
            step_id="features",
            data_schema=schema,
            errors=errors,
            description_placeholders={
                "camera_name": self._pending_cameras[current_channel],
                "channel": current_channel,
            },
        )

    def _finalize(self) -> FlowResult:
        new_data = {
            **self.config_entry.data,
            CONF_CAMERAS: self._pending_cameras,
            CONF_CAMERA_FEATURES: self._feature_selection,
        }
        self.hass.config_entries.async_update_entry(self.config_entry, data=new_data)
        return self.async_create_entry(title="", data={})
