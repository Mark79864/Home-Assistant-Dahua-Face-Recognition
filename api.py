"""Minimal Dahua NVR HTTP CGI client.

Handles:
  * Digest-authenticated requests (Dahua devices require HTTP Digest auth
    on the cgi-bin endpoints; aiohttp has no built-in digest support so a
    small RFC 2617 implementation is included below).
  * Reading the channel/camera list from the device.
  * Opening the long-lived "eventManager.cgi?action=attach" multipart
    stream for a single channel and yielding parsed FaceDetection /
    FaceRecognition events.

The event stream format handled here is the one documented in Dahua's
"Video Analyse APIs" reference (section 9.2.15 / 9.2.16):

    --<boundary>
    Content-Type: text/plain
    Content-Length: <length>

    Code=FaceRecognition;action=Pulse;index=0;data={ ...json... }
    --<boundary>

Some firmware / subscription paths instead emit the older
"Events[0].EventBaseInfo.Code=..." key/value style multipart body. Both
are parsed and normalized to the same dict shape by parse_event_block().
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Optional
from urllib.parse import quote

import aiohttp
from yarl import URL

_LOGGER = logging.getLogger(__name__)

# Number of stacked lines set_overlay_lines supports, and the vertical
# space (out of the 0-8191 proportional coordinate range VideoWidget
# uses) reserved per line when auto-computing each line's Rect.
OVERLAY_MAX_LINES = 6
_OVERLAY_LINE_HEIGHT = 1200


class DahuaAuthError(Exception):
    """Raised when credentials are rejected."""


class DahuaConnectionError(Exception):
    """Raised when the device cannot be reached, or returns an
    unexpected/error response. status_code is set when this came from an
    HTTP status other than 200 (e.g. 501 = the device doesn't implement
    this endpoint at all — a permanent condition, not a transient one);
    None when it came from a malformed/error response body instead."""

    def __init__(self, message: str, status_code: Optional[int] = None) -> None:
        super().__init__(message)
        self.status_code = status_code


# ---------------------------------------------------------------------------
# Digest auth (RFC 2617 - MD5, qop=auth)
# ---------------------------------------------------------------------------


def _md5(text: str) -> str:
    return hashlib.md5(text.encode("utf-8")).hexdigest()


@dataclass
class _DigestChallenge:
    realm: str
    nonce: str
    qop: Optional[str] = None
    opaque: Optional[str] = None
    algorithm: str = "MD5"

    @classmethod
    def parse(cls, header_value: str) -> "_DigestChallenge":
        # header_value looks like: Digest realm="...", nonce="...", qop="auth", ...
        params: dict[str, str] = {}
        for match in re.finditer(r'(\w+)=(?:"([^"]*)"|([^,]+))', header_value):
            key = match.group(1)
            value = match.group(2) if match.group(2) is not None else match.group(3)
            params[key.lower()] = value.strip()
        return cls(
            realm=params.get("realm", ""),
            nonce=params.get("nonce", ""),
            qop=params.get("qop"),
            opaque=params.get("opaque"),
            algorithm=params.get("algorithm", "MD5") or "MD5",
        )


class DahuaDigestAuth:
    """Builds Authorization headers for successive requests to the same host."""

    def __init__(self, username: str, password: str) -> None:
        self._username = username
        self._password = password
        self._nc = 0

    def build_header(
        self, method: str, uri: str, challenge: _DigestChallenge, cnonce: str
    ) -> str:
        self._nc += 1
        nc_value = f"{self._nc:08x}"
        ha1 = _md5(f"{self._username}:{challenge.realm}:{self._password}")
        ha2 = _md5(f"{method}:{uri}")
        if challenge.qop:
            response = _md5(
                f"{ha1}:{challenge.nonce}:{nc_value}:{cnonce}:{challenge.qop}:{ha2}"
            )
        else:
            response = _md5(f"{ha1}:{challenge.nonce}:{ha2}")

        parts = [
            f'username="{self._username}"',
            f'realm="{challenge.realm}"',
            f'nonce="{challenge.nonce}"',
            f'uri="{uri}"',
            f'response="{response}"',
            f'algorithm={challenge.algorithm}',
        ]
        if challenge.qop:
            parts.append(f'qop={challenge.qop}')
            parts.append(f'nc={nc_value}')
            parts.append(f'cnonce="{cnonce}"')
        if challenge.opaque:
            parts.append(f'opaque="{challenge.opaque}"')
        return "Digest " + ", ".join(parts)


# ---------------------------------------------------------------------------
# Event parsing
# ---------------------------------------------------------------------------


_NESTED_TOKEN_RE = re.compile(r"([^.\[\]]+)|\[(\d+)\]")


def _set_nested(root: dict, dotted_key: str, value: str) -> None:
    """Assign value into a nested dict/list structure from a key like
    'Events[0].Candidates[1].Person.Name'.
    """
    tokens = _NESTED_TOKEN_RE.findall(dotted_key)
    node: Any = root
    for i, (name, index) in enumerate(tokens):
        is_last = i == len(tokens) - 1
        if name:
            key = name
            if is_last:
                node[key] = value
            else:
                nxt = tokens[i + 1]
                node = node.setdefault(key, [] if nxt[1] else {})
        else:
            idx = int(index)
            while len(node) <= idx:
                node.append(None)
            if is_last:
                node[idx] = value
            else:
                nxt = tokens[i + 1]
                want_list = bool(nxt[1])
                if want_list:
                    if not isinstance(node[idx], list):
                        node[idx] = []
                else:
                    if not isinstance(node[idx], dict):
                        node[idx] = {}
                node = node[idx]


def _try_parse_json(raw: str) -> dict:
    raw = raw.strip()
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        # Trailing bytes from the multipart boundary sometimes leak in;
        # retry against the substring up to the last closing brace.
        end = raw.rfind("}")
        if end != -1:
            try:
                return json.loads(raw[: end + 1])
            except json.JSONDecodeError:
                pass
    return {}


def parse_event_block(text: str) -> Optional[dict]:
    """Normalize either event wire format into:
    {"code": str, "action": str, "index": str|None, "data": dict}
    """
    text = text.strip()
    if not text:
        return None

    if text.startswith("Code="):
        header, _, data_part = text.partition(";data=")
        fields: dict[str, str] = {}
        for kv in header.split(";"):
            if "=" in kv:
                k, v = kv.split("=", 1)
                fields[k.strip().lower()] = v.strip()
        payload = _try_parse_json(data_part) if data_part else {}
        return {
            "code": fields.get("code"),
            "action": fields.get("action"),
            "index": fields.get("index"),
            "data": payload,
        }

    if text.startswith("Events["):
        root: dict = {}
        for line in text.splitlines():
            line = line.strip()
            if not line or "=" not in line:
                continue
            key, _, value = line.partition("=")
            _set_nested(root, key.strip(), value.strip())
        events = root.get("Events") or []
        if not events:
            return None
        ev = events[0]
        base = ev.get("EventBaseInfo", {}) if isinstance(ev, dict) else {}
        return {
            "code": base.get("Code"),
            "action": base.get("Action"),
            "index": base.get("Index"),
            "data": ev,
        }

    return None


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------


@dataclass
class DahuaClient:
    host: str
    port: int
    username: str
    password: str
    session: aiohttp.ClientSession
    use_ssl: bool = False
    _auth: DahuaDigestAuth = field(init=False)
    # Set True the first time ANY channel on this connection gets a 501
    # from coaxialControlIO — a 501 means the NVR/device doesn't
    # implement that endpoint at all, which is a property of the
    # NVR/firmware, not of a specific channel, so every other channel on
    # the same connection is essentially certain to 501 too. Once set,
    # further calls short-circuit locally instead of making a request
    # that's already known to fail — cuts both API load and repeated
    # per-channel 501 log noise down to a single occurrence overall.
    _coaxial_control_unsupported: bool = field(default=False, init=False)

    def __post_init__(self) -> None:
        self._auth = DahuaDigestAuth(self.username, self.password)

    @property
    def base_url(self) -> str:
        scheme = "https" if self.use_ssl else "http"
        return f"{scheme}://{self.host}:{self.port}"

    async def _digest_request(
        self,
        path: str,
        timeout: Optional[aiohttp.ClientTimeout] = None,
        method: str = "GET",
        json_body: Optional[dict] = None,
    ) -> aiohttp.ClientResponse:
        """Perform a request against `path`, handling the Digest challenge.

        Returns the *second* (authenticated) response, left open for the
        caller to read/stream. Caller is responsible for closing it.

        `path` must already be fully percent-encoded wherever it embeds
        a dynamic/free-text value (e.g. via urllib.parse.quote — see
        set_overlay_lines). The resulting URL is built with
        encoded=True, which tells yarl to trust that encoding exactly
        as-is rather than re-parsing and potentially re-encoding it —
        this is what guarantees arbitrary text (spaces, "%", "°", other
        Unicode) survives the round trip correctly instead of risking
        double-encoding, regardless of aiohttp/yarl version differences
        in how aggressively they'd otherwise normalize a plain string.
        """
        url = URL(f"{self.base_url}{path}", encoded=True)
        uri = url.raw_path_qs
        kwargs: dict = {"timeout": timeout}
        if json_body is not None:
            kwargs["json"] = json_body

        first = await self.session.request(method, url, **kwargs)
        if first.status != 401:
            return first
        www_auth = first.headers.get("WWW-Authenticate", "")
        first.close()
        if "digest" not in www_auth.lower():
            raise DahuaAuthError(
                "Device did not request Digest auth as expected; "
                f"got: {www_auth!r}"
            )
        challenge = _DigestChallenge.parse(www_auth)
        cnonce = hashlib.md5(str(id(self)).encode() + str(asyncio.get_running_loop().time()).encode()).hexdigest()[:16]
        header = self._auth.build_header(method, uri, challenge, cnonce)

        second = await self.session.request(
            method, url, headers={"Authorization": header}, **kwargs
        )
        if second.status == 401:
            second.close()
            raise DahuaAuthError("Username or password rejected by device")
        return second

    async def test_connection(self) -> None:
        """Raise DahuaAuthError/DahuaConnectionError, or return on success."""
        timeout = aiohttp.ClientTimeout(total=10)
        try:
            resp = await self._digest_request(
                "/cgi-bin/magicBox.cgi?action=getDeviceType", timeout=timeout
            )
        except DahuaAuthError:
            raise
        except aiohttp.ClientError as err:
            raise DahuaConnectionError(str(err)) from err
        try:
            if resp.status != 200:
                raise DahuaConnectionError(f"Unexpected status {resp.status}")
        finally:
            resp.close()

    async def get_channels(self) -> dict[int, str]:
        """Return {channel_index: channel_name} for the NVR's cameras."""
        timeout = aiohttp.ClientTimeout(total=10)
        resp = await self._digest_request(
            "/cgi-bin/configManager.cgi?action=getConfig&name=ChannelTitle",
            timeout=timeout,
        )
        try:
            if resp.status != 200:
                raise DahuaConnectionError(f"Unexpected status {resp.status}")
            text = await resp.text(errors="ignore")
        finally:
            resp.close()

        channels: dict[int, str] = {}
        # Lines look like: table.ChannelTitle[0].Name=Front Door
        pattern = re.compile(r"table\.ChannelTitle\[(\d+)\]\.Name=(.*)")
        for line in text.splitlines():
            match = pattern.match(line.strip())
            if match:
                idx = int(match.group(1))
                name = match.group(2).strip() or f"Channel {idx}"
                channels[idx] = name

        if not channels:
            # Fall back to a single-channel device (e.g. talking directly
            # to a camera rather than an NVR).
            channels[0] = "Channel 0"
        return channels

    async def _digest_get_text(self, path: str, timeout_seconds: int = 15) -> str:
        """GET `path` (digest-authenticated) and return the response body
        as text. Raises DahuaAuthError / DahuaConnectionError on failure.

        On a non-200 status, the response body is read and included in
        the raised error (and always logged at debug level) rather than
        discarded — Dahua devices commonly return a explanatory text
        body even on error statuses (e.g. "Invalid channel" vs "Not
        supported"), and without it every such failure just says
        "Unexpected status 400" with no way to tell what actually went
        wrong short of another round of guessing.
        """
        timeout = aiohttp.ClientTimeout(total=timeout_seconds)
        resp = await self._digest_request(path, timeout=timeout)
        try:
            body = await resp.text(errors="ignore")
            if resp.status != 200:
                _LOGGER.debug(
                    "Non-200 status %s for %s; response body: %r",
                    resp.status,
                    path,
                    body,
                )
                raise DahuaConnectionError(
                    f"Unexpected status {resp.status} for {path}: {body.strip()[:200]!r}",
                    status_code=resp.status,
                )
            return body
        finally:
            resp.close()

    async def get_disable_linkage(self, channel: int) -> Optional[bool]:
        """Current DisableLinkage.Enable for a channel, via
        configManager.cgi's getConfig — the actual channel arm/disarm
        mechanism (confirmed against real TiOC camera hardware): when
        Enable is true, smart-event linkage/alarms are DISABLED for that
        channel (disarmed); when false, linkage is active (armed).

        Accepts BOTH response shapes: a per-channel indexed table
        ("table.DisableLinkage[N].Enable=...", channel 0-indexed same as
        everywhere else in this integration) AND a bare, non-indexed
        single value ("table.DisableLinkage.Enable=..."). Some real-world
        responses also insert a stray space after "table." (observed:
        "table. DisableLinkage.Enable=true") — tolerated here too.

        If a channel-specific indexed value exists, it's used. Otherwise,
        if the device only ever reports the bare form, that single value
        is treated as applying to EVERY channel — some NVRs genuinely
        don't support per-channel arming at all and just report one
        device-wide value regardless of which channel is asked about; in
        that case every camera's Armed switch reflects (and controls)
        the same underlying state, which matches the actual hardware
        rather than inventing per-channel granularity that isn't there.

        Returns None if the device doesn't report a value at all (e.g.
        no TiOC-style linkage-disable support whatsoever) — callers
        should treat that as "unknown/unsupported", not as a false.
        """
        text = await self._digest_get_text(
            "/cgi-bin/configManager.cgi?action=getConfig&name=DisableLinkage"
        )
        _LOGGER.debug("DisableLinkage getConfig raw response: %r", text)

        # \s* tolerates a stray space some devices insert after "table."
        indexed_pattern = re.compile(
            r"table\.\s*DisableLinkage\[(\d+)\]\.Enable=(\w+)", re.IGNORECASE
        )
        bare_pattern = re.compile(
            r"table\.\s*DisableLinkage\.Enable=(\w+)", re.IGNORECASE
        )
        bare_value: Optional[bool] = None
        for line in text.splitlines():
            line = line.strip()
            match = indexed_pattern.match(line)
            if match and int(match.group(1)) == channel:
                return match.group(2).strip().lower() == "true"
            match = bare_pattern.match(line)
            if match:
                bare_value = match.group(1).strip().lower() == "true"

        if bare_value is not None:
            return bare_value

        _LOGGER.warning(
            "No DisableLinkage value found for channel %s in response: %r",
            channel,
            text,
        )
        return None

    async def set_disable_linkage(self, channel: int, disable: bool) -> None:
        """Set DisableLinkage.Enable for a channel via configManager.cgi's
        setConfig. disable=True disarms (linkage disabled); disable=False
        arms (linkage active).

        Tries the per-channel indexed form ("DisableLinkage[N].Enable=...")
        first, and falls back to the bare, non-indexed form
        ("DisableLinkage.Enable=...") only if that fails — this exact
        try-then-fallback order (not a channel==0 guess) matches the
        approach the rroller/dahua Home Assistant integration uses in
        production across a wide range of real devices: some cameras
        (their example: DH-P3D-3F-PV-P) don't support the indexed form at
        all and only accept the bare one, regardless of channel number.
        """
        value = "true" if disable else "false"
        indexed_path = (
            "/cgi-bin/configManager.cgi?action=setConfig"
            f"&DisableLinkage[{channel}].Enable={value}"
        )
        try:
            text = await self._digest_get_text(indexed_path)
            _LOGGER.debug("DisableLinkage setConfig (indexed) raw response: %r", text)
            stripped = text.strip()
            if stripped and not stripped.upper().startswith("OK"):
                raise DahuaConnectionError(
                    f"Unexpected response setting DisableLinkage: {stripped[:200]}"
                )
            return
        except DahuaConnectionError:
            pass  # fall through to the bare form below

        bare_path = (
            f"/cgi-bin/configManager.cgi?action=setConfig&DisableLinkage.Enable={value}"
        )
        text = await self._digest_get_text(bare_path)
        _LOGGER.debug("DisableLinkage setConfig (bare) raw response: %r", text)
        stripped = text.strip()
        if stripped and not stripped.upper().startswith("OK"):
            raise DahuaConnectionError(
                f"Unexpected response setting DisableLinkage: {stripped[:200]}"
            )

    async def get_coaxial_status(self, channel_1indexed: int) -> dict[str, str]:
        """{"whitelight": "on"/"off", "speaker": "on"/"off"} for a channel,
        via coaxialControlIO.cgi's getStatus.

        NOTE: the action name is "getStatus" (capital S) — not the
        lowercase "getstatus" this integration used to send, which is
        the confirmed root cause of every device returning 501 Not
        Implemented regardless of hardware. Matched byte-for-byte
        against the rroller/dahua integration's production, working
        request: "/cgi-bin/coaxialControlIO.cgi?action=getStatus&channel=1".

        The response format is also case- and prefix-different from what
        the general API doc's own text implied: real devices return a
        DOUBLE "status." prefix with capitalized field names —
        "status.status.WhiteLight=Off" / "status.status.Speaker=Off" —
        not the single-prefix lowercase form previously assumed. Parsed
        case-insensitively and tolerant of either a single or double
        "status." prefix, to be robust to whichever a given device sends.

        NOTE the channel numbering here is 1-indexed per the API doc's
        explicit statement for this endpoint ("starts from 1") — unlike
        most other channel numbers in this integration.

        Missing keys mean the device didn't report that output at all
        (e.g. no white light hardware on this channel).

        Raises DahuaConnectionError with status_code=501 immediately
        (without making a request) once ANY channel on this connection
        has confirmed 501 — see _coaxial_control_unsupported.
        """
        if self._coaxial_control_unsupported:
            raise DahuaConnectionError(
                "coaxialControlIO is not supported on this connection "
                "(confirmed by an earlier 501 on another channel)",
                status_code=501,
            )
        try:
            text = await self._digest_get_text(
                f"/cgi-bin/coaxialControlIO.cgi?action=getStatus&channel={channel_1indexed}"
            )
        except DahuaConnectionError as err:
            if err.status_code == 501:
                self._coaxial_control_unsupported = True
            raise
        _LOGGER.debug(
            "coaxialControlIO getStatus (channel=%s) raw response: %r",
            channel_1indexed,
            text,
        )
        status: dict[str, str] = {}
        # (status\.)? makes the second "status." prefix optional, so both
        # "status.WhiteLight=" and "status.status.WhiteLight=" match.
        pattern = re.compile(
            r"status\.(?:status\.)?(whitelight|speaker|redbluelight)=(\w+)",
            re.IGNORECASE,
        )
        for line in text.splitlines():
            match = pattern.match(line.strip())
            if match:
                status[match.group(1).lower()] = match.group(2).strip().lower()
        if not status:
            _LOGGER.warning(
                "No white light/speaker/red-blue-light status found for "
                "channel %s in response: %r",
                channel_1indexed,
                text,
            )
        return status

    async def set_coaxial_output(
        self, channel_1indexed: int, output_type: int, turn_on: bool
    ) -> None:
        """Control an output via coaxialControlIO.cgi's control.

        NOTE: no TriggerMode parameter is sent, matching the
        rroller/dahua integration's production, working request exactly:
        "/cgi-bin/coaxialControlIO.cgi?action=control&channel={ch}
        &info[0].Type={type}&info[0].IO={io}". An earlier version of
        this integration added "&info[0].TriggerMode=2" (not present in
        that reference implementation, and not required for a manual
        user action) — plausibly contributing to devices rejecting the
        request outright.

        output_type: 1 = white light, 2 = siren (both documented). Note
        that on TiOC / active-deterrence camera models, Type=1 is the
        SAME physical output as the red/blue flashing warning light —
        there is no separate "Type=3" for it, despite that seeming like
        the natural extrapolation. Confirmed against the rroller/dahua
        Home Assistant integration's production implementation: which
        light actually responds to Type=1 (plain white spotlight vs.
        red/blue strobe) depends entirely on the camera hardware, not on
        which Type value is sent. Similarly Type=2, despite the API doc
        calling it "Speaker", behaves as a siren in practice — it
        triggers for roughly 10-15 seconds and then the device turns
        itself back off.

        Same 1-indexed channel numbering caveat as get_coaxial_status.
        """
        io_value = 1 if turn_on else 2
        path = (
            "/cgi-bin/coaxialControlIO.cgi?action=control"
            f"&channel={channel_1indexed}&info[0].Type={output_type}"
            f"&info[0].IO={io_value}"
        )
        text = await self._digest_get_text(path)
        _LOGGER.debug("coaxialControlIO control raw response: %r", text)
        stripped = text.strip()
        if stripped and not stripped.upper().startswith("OK"):
            raise DahuaConnectionError(
                f"Unexpected response controlling coaxial output: {stripped[:200]}"
            )

    async def get_event_caps(self) -> dict[str, str]:
        """Raw key=value capabilities from eventManager.cgi's getCaps.
        Device-wide call (not channel-scoped in the URL), but the
        response itself contains per-channel arrays for some fields,
        e.g. caps.SupportDisableLinkage[N]."""
        text = await self._digest_get_text("/cgi-bin/eventManager.cgi?action=getCaps")
        caps: dict[str, str] = {}
        for line in text.splitlines():
            line = line.strip()
            if not line or "=" not in line:
                continue
            key, _, value = line.partition("=")
            caps[key.strip()] = value.strip().rstrip(",")
        return caps

    # -----------------------------------------------------------------
    # Alarm input / alarm output (relay) — §4.9.2-4.9.7
    # -----------------------------------------------------------------
    # NOTE ON SCOPE: alarm.cgi's endpoints are device-wide — they take
    # no channel parameter at all, unlike everything else in this
    # integration. This is why alarm input/output entities are attached
    # to the NVR hub device rather than any one camera's device.
    #
    # I could not find a documented endpoint or index convention in the
    # API doc that separately addresses "IPC EXT" (a connected camera's
    # own alarm terminals) from the NVR's own local alarm ports — no
    # channel-scoped alarm.cgi variant, no distinct config table name,
    # nothing under a "RemoteDevice"/"ExtAlarm" style name. It's
    # possible (based on how Dahua's own NVR web UI groups "Local" and
    # "IPC External" alarm tabs over what may just be one underlying
    # flat list) that getInSlots/getOutSlots' counts and the getInState/
    # getOutState bitmasks already include both local AND IPC EXT ports
    # combined in one index space — but this is UNCONFIRMED. See the
    # README for what to check on real hardware.

    async def get_alarm_in_slots(self) -> int:
        """Number of alarm input channels the device reports, via
        alarm.cgi's getInSlots."""
        text = await self._digest_get_text("/cgi-bin/alarm.cgi?action=getInSlots")
        _LOGGER.debug("alarm.cgi getInSlots raw response: %r", text)
        return self._parse_alarm_result(text)

    async def get_alarm_out_slots(self) -> int:
        """Number of alarm output (relay) channels the device reports,
        via alarm.cgi's getOutSlots."""
        text = await self._digest_get_text("/cgi-bin/alarm.cgi?action=getOutSlots")
        _LOGGER.debug("alarm.cgi getOutSlots raw response: %r", text)
        return self._parse_alarm_result(text)

    async def get_alarm_in_state(self) -> set[int]:
        """0-based indices of currently-active (triggered) alarm input
        channels, via alarm.cgi's getInState — a bitmask where bit N
        set means channel N currently has an alarm."""
        text = await self._digest_get_text("/cgi-bin/alarm.cgi?action=getInState")
        _LOGGER.debug("alarm.cgi getInState raw response: %r", text)
        return self._bitmask_to_indexes(self._parse_alarm_result(text))

    async def get_alarm_out_state(self) -> set[int]:
        """0-based indices of currently-active alarm output channels,
        via alarm.cgi's getOutState — same bitmask convention as
        get_alarm_in_state."""
        text = await self._digest_get_text("/cgi-bin/alarm.cgi?action=getOutState")
        _LOGGER.debug("alarm.cgi getOutState raw response: %r", text)
        return self._bitmask_to_indexes(self._parse_alarm_result(text))

    @staticmethod
    def _parse_alarm_result(text: str) -> int:
        for line in text.splitlines():
            line = line.strip()
            if line.lower().startswith("result="):
                try:
                    return int(line.split("=", 1)[1].strip())
                except ValueError:
                    return 0
        return 0

    @staticmethod
    def _bitmask_to_indexes(bitmask: int) -> set[int]:
        return {i for i in range(32) if bitmask & (1 << i)}

    async def get_alarm_in_names(self) -> dict[int, str]:
        """{index: name} for configured alarm input channels, via
        configManager.cgi's getConfig&name=Alarm. Best-effort — returns
        {} on failure; callers should fall back to a generic label."""
        text = await self._digest_get_text(
            "/cgi-bin/configManager.cgi?action=getConfig&name=Alarm"
        )
        _LOGGER.debug("Alarm getConfig raw response: %r", text)
        return self._parse_indexed_names(text, "Alarm")

    async def get_alarm_out_names(self) -> dict[int, str]:
        """{index: name} for configured alarm output channels, via
        configManager.cgi's getConfig&name=AlarmOut."""
        text = await self._digest_get_text(
            "/cgi-bin/configManager.cgi?action=getConfig&name=AlarmOut"
        )
        _LOGGER.debug("AlarmOut getConfig raw response: %r", text)
        return self._parse_indexed_names(text, "AlarmOut")

    @staticmethod
    def _parse_indexed_names(text: str, table_name: str) -> dict[int, str]:
        names: dict[int, str] = {}
        pattern = re.compile(rf"table\.{table_name}\[(\d+)\]\.Name=(.*)")
        for line in text.splitlines():
            match = pattern.match(line.strip())
            if match:
                names[int(match.group(1))] = match.group(2).strip()
        return names

    async def set_alarm_out_mode(self, index: int, mode: int) -> None:
        """Set an alarm output channel's Mode via configManager.cgi's
        setConfig: 0 = automatic (rule-driven), 1 = force alarm (on),
        2 = force close (off)."""
        text = await self._digest_get_text(
            f"/cgi-bin/configManager.cgi?action=setConfig&AlarmOut[{index}].Mode={mode}"
        )
        _LOGGER.debug("AlarmOut setConfig raw response: %r", text)
        stripped = text.strip()
        if stripped and not stripped.upper().startswith("OK"):
            raise DahuaConnectionError(
                f"Unexpected response setting AlarmOut mode: {stripped[:200]}"
            )

    async def probe_camera_capabilities(self, channel: int) -> set[str]:
        """Best-effort detection of which selectable features (see
        features.py) a channel actually supports, used only to pick
        sensible pre-selected defaults in the setup feature picker.

        Never raises — any individual probe that fails or the device
        doesn't support just means that feature isn't pre-selected;
        this never blocks setup, and the user can still enable/disable
        anything manually regardless of what this finds. The IVS
        analytics features (LeftDetection, etc.) and
        FEATURE_AUDIO_PLAYBACK aren't probed — no reliable capability
        signal exists for either in the API doc (see play_audio_file's
        docstring for the audio case), so they keep their default of
        being unselected unless the user picks them manually.
        """
        from .features import (
            FEATURE_FACE,
            FEATURE_PRIVACY_MASK,
            FEATURE_PTZ,
            FEATURE_SIREN,
            FEATURE_WHITE_LIGHT,
        )

        detected: set[str] = set()

        try:
            # A successful call (even an empty group list) means this
            # channel has face-recognition search support; a failure
            # means it doesn't. Only used for this probe — arm/disarm
            # uses the separate, simpler DisableLinkage mechanism.
            await self._digest_get_text(
                "/cgi-bin/faceRecognitionServer.cgi?action=getGroup"
                f"&channel={channel + 1}"
            )
        except (DahuaAuthError, DahuaConnectionError) as err:
            _LOGGER.debug(
                "Capability probe: face recognition failed for channel %s: %s",
                channel,
                err,
            )
        else:
            detected.add(FEATURE_FACE)

        try:
            status = await self.get_coaxial_status(channel + 1)
        except (DahuaAuthError, DahuaConnectionError) as err:
            _LOGGER.debug(
                "Capability probe: coaxial status failed for channel %s: %s",
                channel,
                err,
            )
        else:
            if "whitelight" in status:
                detected.add(FEATURE_WHITE_LIGHT)
            if "speaker" in status:
                detected.add(FEATURE_SIREN)

        try:
            # A successful call (even zero configured presets) means the
            # channel has PTZ hardware; a failure means it doesn't.
            await self.get_ptz_presets(channel + 1)
        except (DahuaAuthError, DahuaConnectionError) as err:
            _LOGGER.debug(
                "Capability probe: PTZ presets failed for channel %s: %s",
                channel,
                err,
            )
        else:
            detected.add(FEATURE_PTZ)

        try:
            await self.get_privacy_mask_enabled(channel + 1)
        except (DahuaAuthError, DahuaConnectionError) as err:
            _LOGGER.debug(
                "Capability probe: privacy masking failed for channel %s: %s",
                channel,
                err,
            )
        else:
            detected.add(FEATURE_PRIVACY_MASK)

        return detected

    # -----------------------------------------------------------------
    # PTZ (§8.1 of the API doc)
    # -----------------------------------------------------------------
    # NOTE: ptz.cgi's channel numbering is 1-indexed per the doc's
    # explicit statements throughout this section ("starting from 1") —
    # same convention as coaxialControlIO.cgi, different from most of
    # the rest of this integration (0-indexed). Callers pass channel + 1.

    async def ptz_move(
        self,
        channel_1indexed: int,
        code: str,
        arg1: int = 0,
        arg2: int = 0,
        arg3: int = 0,
    ) -> None:
        """Start a PTZ movement via ptz.cgi's action=start. `code` is one
        of the documented operation codes: Up, Down, Left, Right,
        LeftUp, RightUp, LeftDown, RightDown, ZoomWide, ZoomTele,
        FocusNear, FocusFar, IrisLarge, IrisSmall. Per the doc's
        worked examples, Up/Down/Left/Right take arg2=speed (1-8) with
        arg1=arg3=0; Zoom/Focus/Iris codes take arg1=arg2=arg3=0.
        """
        path = (
            f"/cgi-bin/ptz.cgi?action=start&channel={channel_1indexed}"
            f"&code={code}&arg1={arg1}&arg2={arg2}&arg3={arg3}"
        )
        text = await self._digest_get_text(path)
        _LOGGER.debug("ptz.cgi start (code=%s) raw response: %r", code, text)
        stripped = text.strip()
        if stripped and not stripped.upper().startswith("OK"):
            raise DahuaConnectionError(
                f"Unexpected response starting PTZ move ({code}): {stripped[:200]}"
            )

    async def ptz_stop(self, channel_1indexed: int, code: str) -> None:
        """Stop a PTZ movement via ptz.cgi's action=stop. `code` should
        match whatever was passed to ptz_move — Dahua tracks in-progress
        movement per code."""
        path = (
            f"/cgi-bin/ptz.cgi?action=stop&channel={channel_1indexed}"
            f"&code={code}&arg1=0&arg2=0&arg3=0"
        )
        text = await self._digest_get_text(path)
        _LOGGER.debug("ptz.cgi stop (code=%s) raw response: %r", code, text)
        stripped = text.strip()
        if stripped and not stripped.upper().startswith("OK"):
            raise DahuaConnectionError(
                f"Unexpected response stopping PTZ move ({code}): {stripped[:200]}"
            )

    async def ptz_nudge(
        self,
        channel_1indexed: int,
        code: str,
        speed: int = 5,
        duration: float = 0.5,
    ) -> None:
        """The "press a direction button" pattern: start moving, wait
        briefly, then stop — since a single button press has no natural
        "release" event of its own to stop on. speed only applies to
        Up/Down/Left/Right (1-8 per the doc); ignored (sent as 0) for
        Zoom/Focus/Iris codes, which don't take a speed argument."""
        arg2 = speed if code in ("Up", "Down", "Left", "Right") else 0
        await self.ptz_move(channel_1indexed, code, arg1=0, arg2=arg2, arg3=0)
        await asyncio.sleep(duration)
        await self.ptz_stop(channel_1indexed, code)

    async def get_ptz_presets(self, channel_1indexed: int) -> list[dict]:
        """[{"index": int, "name": str}, ...] presets configured for a
        channel, via ptz.cgi's getPresets. Empty list if the channel
        supports PTZ but has no presets configured yet (as opposed to a
        raised exception, which means the channel doesn't support PTZ
        at all — see probe_camera_capabilities)."""
        text = await self._digest_get_text(
            f"/cgi-bin/ptz.cgi?action=getPresets&channel={channel_1indexed}"
        )
        _LOGGER.debug("ptz.cgi getPresets raw response: %r", text)
        presets: dict[int, dict] = {}
        index_pattern = re.compile(r"presets\[(\d+)\]\.Index=(\d+)")
        name_pattern = re.compile(r"presets\[(\d+)\]\.Name=(.*)")
        for line in text.splitlines():
            line = line.strip()
            match = index_pattern.match(line)
            if match:
                presets.setdefault(int(match.group(1)), {})["index"] = int(
                    match.group(2)
                )
                continue
            match = name_pattern.match(line)
            if match:
                presets.setdefault(int(match.group(1)), {})["name"] = match.group(
                    2
                ).strip()
        return [presets[i] for i in sorted(presets) if "index" in presets[i]]

    async def ptz_goto_preset(self, channel_1indexed: int, preset_index: int) -> None:
        """Move to a preset via ptz.cgi's action=start&code=GotoPreset."""
        path = (
            "/cgi-bin/ptz.cgi?action=start&code=GotoPreset"
            f"&channel={channel_1indexed}&arg1=0&arg2={preset_index}&arg3=0"
        )
        text = await self._digest_get_text(path)
        _LOGGER.debug("ptz.cgi GotoPreset raw response: %r", text)
        stripped = text.strip()
        if stripped and not stripped.upper().startswith("OK"):
            raise DahuaConnectionError(
                f"Unexpected response going to PTZ preset {preset_index}: {stripped[:200]}"
            )

    async def ptz_set_preset(self, channel_1indexed: int, preset_index: int) -> None:
        """Save the camera's CURRENT position as preset_index via
        ptz.cgi's action=start&code=SetPreset — overwrites whatever was
        saved there before, without confirmation (the button that calls
        this is the confirmation)."""
        path = (
            "/cgi-bin/ptz.cgi?action=start&code=SetPreset"
            f"&channel={channel_1indexed}&arg1=0&arg2={preset_index}&arg3=0"
        )
        text = await self._digest_get_text(path)
        _LOGGER.debug("ptz.cgi SetPreset raw response: %r", text)
        stripped = text.strip()
        if stripped and not stripped.upper().startswith("OK"):
            raise DahuaConnectionError(
                f"Unexpected response setting PTZ preset {preset_index}: {stripped[:200]}"
            )

    # -----------------------------------------------------------------
    # Overlay (OSD) text (§4.5.11 VideoWidget)
    # -----------------------------------------------------------------
    # NOTE: unlike PTZ/coaxial, VideoWidget's channel array is explicitly
    # documented as 0-indexed ("the array index represents the channel
    # number which starts from 0") — same convention as most of the rest
    # of this integration. No +1 offset here.

    async def set_overlay_lines(self, channel: int, lines: list[str]) -> None:
        """Write up to OVERLAY_MAX_LINES separate lines of custom
        overlay (OSD) text, each as its own
        VideoWidget[channel].UserDefinedTitle[N] box (N = 0..max-1),
        stacked top-to-bottom with an auto-computed Rect.

        The API doc documents a single UserDefinedTitle box as
        supporting up to 2 lines via a '|' separator in its Text field
        — this deliberately doesn't use that. One line per box (rather
        than packing 2 lines per box) keeps "N lines in, N boxes
        positioned" simple and predictable regardless of how many lines
        are requested, and avoids relying on the doc's stated 2-line
        cap being accurate for every firmware.

        Any box index from len(lines) up to OVERLAY_MAX_LINES-1 is
        cleared (empty text, EncodeBlend=false) so shrinking from e.g.
        6 lines to 3 properly removes the stale ones rather than
        leaving them rendered on screen.

        Text values are percent-encoded (via urllib.parse.quote, which
        correctly handles arbitrary Unicode — spaces, "%", "°", etc. —
        by encoding their UTF-8 bytes), and the final request URL is
        built with encoded=True (see _digest_request) so nothing
        re-encodes that already-correct encoding a second time.

        Raises ValueError if more than OVERLAY_MAX_LINES lines are given.
        """
        if len(lines) > OVERLAY_MAX_LINES:
            raise ValueError(
                f"set_overlay_lines supports at most {OVERLAY_MAX_LINES} "
                f"lines, got {len(lines)}"
            )
        params: list[str] = []
        for index in range(OVERLAY_MAX_LINES):
            prefix = f"VideoWidget[{channel}].UserDefinedTitle[{index}]"
            if index < len(lines) and lines[index]:
                top = index * _OVERLAY_LINE_HEIGHT
                bottom = top + _OVERLAY_LINE_HEIGHT - 100
                params.append(f"{prefix}.Text={quote(lines[index], safe='')}")
                params.append(f"{prefix}.EncodeBlend=true")
                params.append(f"{prefix}.Rect[0]=0")
                params.append(f"{prefix}.Rect[1]={top}")
                params.append(f"{prefix}.Rect[2]=8191")
                params.append(f"{prefix}.Rect[3]={bottom}")
            else:
                params.append(f"{prefix}.Text=")
                params.append(f"{prefix}.EncodeBlend=false")

        path = "/cgi-bin/configManager.cgi?action=setConfig&" + "&".join(params)
        text = await self._digest_get_text(path)
        _LOGGER.debug(
            "VideoWidget setConfig (multi-line overlay) raw response: %r", text
        )
        stripped = text.strip()
        if stripped and not stripped.upper().startswith("OK"):
            raise DahuaConnectionError(
                f"Unexpected response setting overlay lines: {stripped[:200]}"
            )

    # -----------------------------------------------------------------
    # Privacy masking (§4.5.27 / §4.5.28)
    # -----------------------------------------------------------------
    # NOTE: like ptz.cgi/coaxialControlIO.cgi, PrivacyMasking.cgi's
    # channel is 1-indexed per the doc's explicit statement ("starts
    # from 1"). Callers pass channel + 1.

    async def get_privacy_mask_enabled(self, channel_1indexed: int) -> Optional[bool]:
        """Whether privacy masking is currently enabled for a channel,
        via PrivacyMasking.cgi's getPrivacyMaskingEnable. This is a
        single master on/off for whatever mask shapes are already
        configured (via the camera's own web UI) — it doesn't
        create/edit/delete individual shapes. Returns None if the
        device doesn't report a value (no privacy masking support)."""
        text = await self._digest_get_text(
            "/cgi-bin/PrivacyMasking.cgi?action=getPrivacyMaskingEnable"
            f"&channel={channel_1indexed}"
        )
        _LOGGER.debug(
            "PrivacyMasking getPrivacyMaskingEnable raw response: %r", text
        )
        for line in text.splitlines():
            line = line.strip()
            if line.lower().startswith("enable="):
                return line.split("=", 1)[1].strip().lower() == "true"
        return None

    async def set_privacy_mask_enabled(
        self, channel_1indexed: int, enable: bool
    ) -> None:
        """Enable/disable ALL configured privacy masking covers for a
        channel at once, via PrivacyMasking.cgi's
        setPrivacyMaskingEnable."""
        value = "true" if enable else "false"
        text = await self._digest_get_text(
            "/cgi-bin/PrivacyMasking.cgi?action=setPrivacyMaskingEnable"
            f"&channel={channel_1indexed}&Enable={value}"
        )
        _LOGGER.debug(
            "PrivacyMasking setPrivacyMaskingEnable raw response: %r", text
        )
        stripped = text.strip()
        if stripped and not stripped.upper().startswith("OK"):
            raise DahuaConnectionError(
                f"Unexpected response setting privacy masking: {stripped[:200]}"
            )

    async def play_audio_file(
        self, path: str, file_name: str, play_times: int = 1
    ) -> str:
        """POST /cgi-bin/api/Speak/startPlayByTime — play an audio file
        already stored on the device through its speaker.

        IMPORTANT: per the API doc, this endpoint takes NO channel
        parameter at all — it's device-level, matching a standalone TiOC
        camera addressed directly (one camera = one speaker). Pointed at
        an NVR instead, there's no documented way to target one specific
        channel's speaker with this call; it will play through whatever
        the device itself resolves it to.

        Returns the device's reported Status ("OK" on success). Raises
        DahuaConnectionError if the device reports "Busy" (already
        broadcasting) or "Error", or on any other unexpected response.
        """
        timeout = aiohttp.ClientTimeout(total=15)
        body = {"Path": path, "FileName": file_name, "PlayTimes": play_times}
        resp = await self._digest_request(
            "/cgi-bin/api/Speak/startPlayByTime",
            timeout=timeout,
            method="POST",
            json_body=body,
        )
        try:
            if resp.status != 200:
                raise DahuaConnectionError(
                    f"Unexpected status {resp.status} playing audio file"
                )
            try:
                data = await resp.json(content_type=None)
            except (json.JSONDecodeError, aiohttp.ContentTypeError):
                data = {}
        finally:
            resp.close()

        status = data.get("Status", "Error") if isinstance(data, dict) else "Error"
        if status == "Busy":
            raise DahuaConnectionError("Device is already broadcasting audio")
        if status != "OK":
            raise DahuaConnectionError(
                f"Device reported an error playing the audio file: {data}"
            )
        return status

    def _extract_boundary(self, content_type: str) -> bytes:
        match = re.search(r"boundary=([^\s;]+)", content_type or "")
        boundary = match.group(1) if match else "myboundary"
        boundary = boundary.strip('"')
        if not boundary.startswith("--"):
            boundary = "--" + boundary
        return boundary.encode()

    async def _read_one_part(
        self, reader: aiohttp.StreamReader, boundary: bytes
    ) -> Optional[tuple[dict[str, str], bytes]]:
        """Read a single multipart part (headers, body). Returns None at
        end of stream. Unlike an async generator, this can be called
        on-demand — which lets listen_events look one part ahead (to check
        whether an event's text/plain part is immediately followed by an
        image/jpeg snapshot) without losing anything if it isn't."""
        while True:
            line = await reader.readline()
            if not line:
                return None  # connection closed
            if boundary in line:
                break

        headers: dict[str, str] = {}
        while True:
            hline = await reader.readline()
            if not hline or hline in (b"\r\n", b"\n"):
                break
            if b":" in hline:
                key, _, value = hline.partition(b":")
                headers[key.strip().decode(errors="ignore").lower()] = (
                    value.strip().decode(errors="ignore")
                )

        length = headers.get("content-length")
        if length and length.strip().isdigit():
            body = await reader.readexactly(int(length))
            await reader.readline()  # trailing CRLF before next boundary
        else:
            chunks = []
            while True:
                part_line = await reader.readline()
                if not part_line or boundary in part_line:
                    break
                chunks.append(part_line)
            body = b"".join(chunks)

        return headers, body

    async def listen_events(
        self,
        channels: list[int],
        codes: set[str],
        on_event: Callable[[dict, Optional[bytes]], None],
        stop_event: asyncio.Event,
    ) -> None:
        """Open ONE event stream covering all of `channels`, subscribed to
        `codes` (the set of Dahua Event Codes any enabled camera feature
        needs — see features.py), and call on_event(event, snapshot_bytes)
        for every matching event received. Runs until stop_event is set or
        an unrecoverable error occurs; the caller (in __init__.py) is
        responsible for reconnect/backoff.

        Each event dict includes an "index" field (the channel that
        actually generated it) — the caller must use that to route the
        event to the right camera rather than assuming everything on this
        connection belongs to a single channel. Some Dahua firmware does
        not actually restrict the stream to the requested channel list, so
        relying on the request-side filter alone causes every camera to
        show the same (most-recent) event.

        When an event's text/plain part is immediately followed by an
        image/jpeg part (the last-seen-face snapshot, per the API doc's
        "snap subscription" example), snapshot_bytes is that jpeg;
        otherwise it's None. If the next part is NOT an image, it is not
        discarded — it's processed as its own part on the following loop
        iteration.
        """
        codes_param = "[" + ",".join(sorted(codes)) + "]"
        channel_list = ",".join(str(c) for c in channels)
        path = (
            "/cgi-bin/eventManager.cgi?action=attach"
            f"&codes={codes_param}&channel=[{channel_list}]&heartbeat=5"
        )
        timeout = aiohttp.ClientTimeout(total=None, sock_connect=15, sock_read=90)
        response = await self._digest_request(path, timeout=timeout)
        try:
            if response.status != 200:
                raise DahuaConnectionError(
                    f"Event subscription failed with status {response.status}"
                )
            boundary = self._extract_boundary(response.headers.get("Content-Type", ""))
            reader = response.content
            pending: Optional[tuple[dict[str, str], bytes]] = None

            while not stop_event.is_set():
                if pending is not None:
                    headers, body = pending
                    pending = None
                else:
                    part = await self._read_one_part(reader, boundary)
                    if part is None:
                        return  # stream closed
                    headers, body = part

                content_type = headers.get("content-type", "")
                if "text/plain" not in content_type:
                    # A stray image (or other part) with no preceding event
                    # we recognized — nothing to attach it to.
                    continue

                text = body.decode("utf-8", errors="ignore")
                event = parse_event_block(text)
                if not event or not event.get("code"):
                    continue

                snapshot_bytes: Optional[bytes] = None
                next_part = await self._read_one_part(reader, boundary)
                if next_part is not None:
                    next_headers, next_body = next_part
                    if "image/jpeg" in next_headers.get("content-type", ""):
                        snapshot_bytes = next_body
                    else:
                        # Not an image — it's the start of the next part,
                        # process it on the next loop iteration.
                        pending = next_part

                on_event(event, snapshot_bytes)
        finally:
            response.close()
