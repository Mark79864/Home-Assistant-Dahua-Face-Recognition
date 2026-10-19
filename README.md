# Dahua Face Recognition — Home Assistant Custom Integration

Subscribes to a Dahua NVR's video-analyse event stream (Dahua's "Video
Analyse APIs" reference, §9.1 IVS events + §9.2.15/9.2.16 face events)
and exposes them as Home Assistant sensors, one device per camera.

Which events a camera reports is picked during setup, per camera, from
an extensible **feature registry** — adding support for a new Dahua
event type is a one-entry addition to `features.py`, nothing else needs
to change.

## Setup

1. Copy `dahua_face/` into your Home Assistant `config/custom_components/`
   directory (so you end up with `config/custom_components/dahua_face/manifest.json`).
2. Restart Home Assistant.
3. **Settings → Devices & Services → Add Integration → "Dahua Face
   Recognition"**.
4. Enter the NVR's IP, port (default 80), username, password.
5. Select the camera channels you want tracked.
6. **For each selected camera**, you'll be asked which features to
   enable — pick from the list below. Each camera can have a different
   set (e.g. an entrance camera might want Face + CrossLineDetection,
   a warehouse camera might want LeftDetection + TakenAwayDetection).
   Face is always pre-checked; White/Security Light and Siren are
   **auto-detected and pre-checked** for cameras that actually report
   that TiOC hardware (see "Capability auto-detection" below) — you
   can still tick/untick anything regardless of what was detected.

**Adding more cameras from the same NVR later:** run "Add Integration"
again with the same NVR IP/port/credentials — since an NVR is a single
device with up to 64 channels, the flow detects the existing NVR and
takes you straight to the camera/feature pickers instead of creating a
duplicate entry. The same pickers (cameras, then features per camera)
are also available any time via the integration's **Configure** button.

## Capability auto-detection

When a camera is being configured for the first time (new camera, no
prior selection saved for it), the feature picker's defaults aren't
just a fixed guess — the integration probes each selectable feature's
real endpoint before showing the page, and only pre-checks a feature
if that probe actually succeeds on that channel:

- **Face Detection & Recognition** — `faceRecognitionServer.cgi`'s
  `getGroup` (a successful response, even with zero groups deployed,
  means the channel supports face-recognition search; a failure means
  it doesn't).
- **White/Security Light, Siren** — `coaxialControlIO.cgi`'s
  `getStatus`, checked per-output.
- **PTZ** — `ptz.cgi`'s `getPresets`.
- **Privacy Mask** — `PrivacyMasking.cgi`'s `getPrivacyMaskingEnable`.

Nothing is defaulted on blindly — a fixed-lens camera with no face
recognition hardware won't have Face pre-ticked, the same way a camera
with no TiOC light doesn't get White/Security Light pre-ticked. A
camera you've already configured before keeps showing its
previously-saved selection as the default instead of re-probing, so
your own choices (including deliberately turning something off) are
never silently overridden.

This is intentionally lightweight, not exhaustive: it never blocks or
fails setup — a probe error just means that feature isn't pre-checked
(same outcome as if the hardware genuinely weren't there), and if
*every* probe happens to fail (rare, since basic connectivity was
already confirmed earlier in the flow), the picker just opens with
nothing pre-checked rather than guessing, and the existing "select at
least one feature" validation makes sure that doesn't slip through
unnoticed. The IVS analytics features (LeftDetection, WanderDetection,
etc.) still aren't auto-detected — there's no reliable per-rule-type
capability signal for those in the API doc, so they stay manual. This
mirrors the design tradeoff the
[rroller/dahua](https://github.com/rroller/dahua) Home Assistant
integration (see "Credit" below) made deliberately, for the same
reason: reliable, comprehensive capability detection across Dahua's
very wide device range turned out to be more trouble than it's worth,
so the safety net is "you can always adjust the picker yourself" —
this integration follows the same principle, just applied a bit more
thoroughly (every probeable feature, not only some).

**There's no dedicated "list device capabilities" endpoint to call
instead** — I looked specifically for one (per a direct ask), across
both the official API doc and the rroller/dahua integration's own
production source. Neither has one: the doc's `getProductDefinition`
only accepts a small fixed set of unrelated `name` values (stream
counts, channel limits — nothing like "does this support a white
light"), there's no `SupportCoaxialControl`-style capability flag
anywhere, and `coaxialControlIO.cgi` itself has no `getcaps` action.
rroller/dahua's own client doesn't have this either — it leans on
matching the device's *model name string* against known patterns in
places, not a formal capability query. So the try-the-real-call-and-
see-if-it-works approach this integration already uses (immediately
above) genuinely is the best signal available, not a placeholder for
something better that exists.

Two things this version adds to make that approach hold up better on
NVRs, though:

- **NVR-wide short-circuit**: a genuine `501 Not Implemented` from
  `coaxialControlIO` (i.e. after fixing the `getStatus` casing bug —
  see "White/Security Light & Siren switches" below — a device that
  *still* 501s really doesn't implement the endpoint) means the NVR
  doesn't implement that endpoint at all — a property of the
  NVR/firmware, not of any one channel. Once any channel confirms
  this, every other channel's probe (during setup *and* ongoing
  polling) short-circuits locally instead of repeating a request
  that's already known to fail — cuts both NVR load and log noise down
  close to one occurrence total instead of one per channel per poll.
- The per-switch warning-then-debug log downgrading (see
  "Troubleshooting switches" below) also means a permanently-501'd
  switch settles down to silence in the logs after its first
  appearance, rather than repeating forever.

If a camera's Siren/White Light was left enabled from before this fix
and it turns out your hardware genuinely doesn't support
`coaxialControlIO` (rare — most of what looked like "not supported"
before was actually the casing bug), auto-detection only applies to
*new* cameras being added for the first time — it won't retroactively
change an existing camera's saved selection, since your own choices
are never silently overridden. Quickest fix: **Configure** → reselect
that camera → untick White/Security Light and/or Siren for it.

## Available features

| Feature | Sensor(s) it adds | State | Key attributes |
|---|---|---|---|
| **Face Detection & Recognition** | Face Name, Face Gender, Face List (matched database/group), Face Expression, Face Confidence, a **Last Face Snapshot** image entity | see below | UID, GroupID, similarity, eye/mouth/mask/beard status |
| **Left Object Detection** (`LeftDetection`) | Left Object Detection | Detected / Clear | bounding_box |
| **Taken Away Detection** (`TakenAwayDetection`) | Taken Away Detection | Detected / Clear | bounding_box |
| **Wander Detection** (`WanderDetection`) | Wander Detection | Detected / Clear | bounding_boxes, tracks, detect_region |
| **Stay Detection** (`StayDetection`) | Stay Detection | Detected / Clear | bounding_box(es), area_id, preset_id, detect_region |
| **Human Trait** (`HumanTrait`, video metadata) | Human Trait | `"<Sex>, <Age>"` | full human_attributes (clothing, hat, bag...) + face_attributes objects |
| **Cross Line Detection** (`CrossLineDetection`) | Cross Line Detection | Detected / Clear | bounding_box, direction, detect_line, rule_id* |
| **Cross Region Detection** (`CrossRegionDetection`) | Cross Region Detection | Detected / Clear | bounding_box, action (Appear/Disappear/Cross/Inside), direction, detect_region, rule_id* |
| **Queue Stay Detection** (`QueueStayDetection`) | Queue Stay Detection | Detected / Clear | bounding_box(es), area_id, preset_id |
| **White/Security Light (TiOC)** (`WhiteLight`) | White/Security Light switch | on/off | On TiOC/active-deterrence camera models, this switch controls the red/blue flashing light — same control, no separate switch (see below). |
| **Siren (TiOC)** (`Siren`) | Siren switch | on/off | Auto-off after ~10-15s; the 60s poll reflects that. |
| **PTZ (Pan/Tilt/Zoom)** (`PTZ`) | 6 movement buttons, a preset selector, 2 preset action buttons | see "PTZ" below | — |
| **Audio File Playback** (`AudioPlayback`) | 2 text fields, a Play button | see "Audio File Playback" below | — |
| **Privacy Mask** (`PrivacyMask`) | Privacy Mask switch | on/off | Master toggle for shapes already configured on the camera — doesn't create/edit them. |

Face Detection & Recognition keeps its own dedicated sensors because
it's richer than a single state+attributes sensor. Every other feature
is generated generically from its entry in `features.py` — see "Adding
a new feature" below.

For the `start_stop`-style features (everything except Face and Human
Trait), state is `"Detected"` while the NVR reports the condition
active (`Action=Start`) and reverts to `"Clear"` once it reports
`Action=Stop`. Human Trait is a `Pulse` event (no start/stop) so its
sensor just reflects the most recently seen person's summary.

## "Clear" state

Every sensor shows `"Clear"` (or `0` for the numeric Face Confidence
sensor) from the moment it's added — before setup has seen its first
event — rather than showing as unavailable/unknown.

For the `start_stop` features, it reverts to `"Clear"` when the NVR
sends that event's `Action=Stop`, same as always.

Face Detection & Recognition has no such stop signal — `FaceRecognition`
is a `Pulse` event — so it's simulated: each new face event (re)starts
a 5-second countdown, cancelling whatever was pending. If 5 seconds
pass with no further face seen on that camera, all five Face sensors
revert to `Clear` (Face Confidence to `0`). A new face detected before
the countdown ends immediately updates the sensors to the new
value — which also cancels/restarts the countdown — so a steady stream
of faces never shows `Clear` in between. Note this is separate from
the existing `"Unknown"` state Face Name/Face List already use for "a
face was seen but didn't match anyone" — `Clear` specifically means "no
face currently in view."

## Armed switch

One `switch.<camera>_armed` entity per configured camera, regardless of
which features are enabled on it — arming is channel-wide, not tied to
a specific feature. Backed by the actual Dahua arm/disarm mechanism
(confirmed against real TiOC camera hardware): `configManager.cgi`'s
`DisableLinkage` config table.

```
DisableLinkage[N].Enable = true   ->  smart-event linkage disabled  ->  DISARMED
DisableLinkage[N].Enable = false  ->  smart-event linkage active    ->  ARMED
```

- Polls the real state every 60 seconds, so it reflects the NVR's
  actual state even if changed from the NVR's own UI, and shows the
  correct state immediately on setup (`update_before_add`).
- Not every camera/channel supports this — it's specifically a
  TiOC-camera feature. If a channel's config doesn't report a
  `DisableLinkage` value at all, the switch shows **unavailable**
  rather than guessing at a state.
- **Reads both response shapes**: a per-channel indexed table
  (`DisableLinkage[N].Enable=...`) and a bare, non-indexed single
  value (`DisableLinkage.Enable=...`, treated as channel 0) — some
  single-channel TiOC devices only expose the bare form.
- **Writing** tries the indexed form first and falls back to the bare
  form only if that fails, regardless of channel number. This
  try-then-fallback order (not a channel-based guess) matches the
  approach the [rroller/dahua](https://github.com/rroller/dahua) Home
  Assistant integration uses in production across a wide range of real
  devices — some cameras only accept the bare form no matter which
  channel they're on.

Channel numbering for this one is 0-indexed, same as everywhere else
in this integration (this is the *opposite* of the caveat that used to
apply to an earlier, incorrect implementation of this switch based on
`faceRecognitionServer.cgi`'s face-group deployment endpoints, which
are 1-indexed — that approach has been replaced entirely by the
`DisableLinkage` mechanism above).

## White/Security Light & Siren switches

`switch.<camera>_white_light` and `switch.<camera>_siren`, created
only for cameras with the matching feature enabled. Both use
`coaxialControlIO.cgi` (§8.5 of the API doc), with the exact request
shape matched byte-for-byte against the
[rroller/dahua](https://github.com/rroller/dahua) integration's
production, working implementation — an earlier version of this
integration got several details of this endpoint wrong, all fixed now:

- **Turn on/off** → `action=control` with `info[0].Type=1` (light) or
  `=2` (siren), `info[0].IO=1`/`2`. **No `TriggerMode` parameter** —
  an earlier version added `&info[0].TriggerMode=2`, which isn't part
  of the working reference request and plausibly caused some devices
  to reject it outright.
- **Poll state** (every 60s, plus once before the entity first
  appears) → `action=getStatus` (capital S — the lowercase
  `action=getstatus` an earlier version sent is, per real-world
  testing, the actual cause of a `501 Not Implemented` on every
  channel regardless of hardware, not a genuine "this device doesn't
  support it" signal). Reads `status.whitelight` / `status.speaker`ish
  keys case-insensitively and tolerates either a single or double
  `status.` prefix (real devices were observed sending
  `status.status.WhiteLight=Off` / `status.status.Speaker=Off`, not
  the single-prefix lowercase form the general API doc's own text
  implied).
- Shows **unavailable** rather than guessing if the device doesn't
  report a status for that output at all (e.g. no such hardware on
  this channel) — now a much more trustworthy signal than before,
  since it no longer 501s on working hardware due to the casing bug.
- This same fix is what makes **capability auto-detection during
  setup** actually work now too: it calls this same `getStatus`
  request to decide whether to pre-check White/Security Light and
  Siren for a new camera, so it was silently failing to detect
  anything the whole time this bug was present.

**Important — the White/Security Light switch IS the red/blue TiOC
light, not a separate control.** The API doc's own naming calls
`Type=1` "white light", and only lists `Type=1`/`Type=2` for this
endpoint at all — there's no `Type=3`. An earlier version of this
integration guessed at an experimental `Type=3` for the red/blue
light; that was wrong. Checking the
[rroller/dahua](https://github.com/rroller/dahua) integration's
production client (`client.py`) settled it: `Type=1` is the *same*
physical output on both plain and TiOC/active-deterrence cameras —
which light actually responds (a plain white spotlight, or the
red/blue flashing strobe) depends entirely on your camera's hardware,
not on anything sent differently. So there's one switch, and on TiOC
hardware it *is* your red/blue flashing light. Similarly, `Type=2` is
labeled "Speaker" in the API doc, but the same reference source found
it behaves as a **siren** in practice — it triggers for roughly 10-15
seconds and the device turns itself back off, which is why this
integration calls it Siren and polls it (rather than assuming it stays
on until told otherwise).

Channel numbering for `coaxialControlIO.cgi` is **1-indexed** — the
API doc states this explicitly for `getstatus` ("starts from 1");
`control`'s own description is ambiguous ("default value is 0"), but
both of the doc's own examples use `channel=1` for a first/default
channel on *both* endpoints, so this integration treats both as
1-indexed for consistency.

## Credit

The corrections above — the `DisableLinkage` fallback order, and that
the white light/siren mechanism has no separate red/blue "Type" —
came from checking against the
[rroller/dahua](https://github.com/rroller/dahua) Home Assistant
integration, an actively maintained, widely-used project with much
broader real-hardware testing than this integration has had. If
something here still doesn't match your camera, that project's
`client.py` is a good reference to compare against.

## Troubleshooting switches (debug logging)

All the polling/control calls above log their raw HTTP response body
at `debug` level, and log a `warning` when a poll finds nothing
parseable. **Any non-200 status now includes the response body in the
raised error message too** — Dahua devices commonly return an
explanatory text body even on error statuses (e.g. a specific reason
for a `400 Bad Request`), and previously that was discarded, leaving
error messages like `"Unexpected status 400"` with no way to tell
what actually went wrong. To see the debug-level detail as well, add
to `configuration.yaml`:

```yaml
logger:
  default: info
  logs:
    custom_components.dahua_face: debug
```

...then restart, trigger the switch/poll, and check **Settings →
System → Logs**. If a switch is still unavailable after this fix,
those raw response lines (now including whatever body the device sent
back) are exactly what's needed to correct the parsing precisely
instead of guessing again.

**Every polled switch/sensor now settles down in the logs**: the first
polling failure after becoming available logs a `WARNING` (so it's
visible), and identical failures on every subsequent poll after that
log at `DEBUG` instead of repeating the same `WARNING` forever — this
applies uniformly (Armed, White/Security Light, Siren, Privacy Mask,
alarm inputs/outputs), not just the specific case below.

### Per-channel Armed / light / siren issues on multi-camera NVR setups

If cameras beyond the first showed Armed unavailable while the first
worked, that was **not** caused by adding multiple cameras during
setup — the per-camera feature picker, live entity-add, and connection
restart logic were all already channel-independent by design. Two
concrete bugs actually caused it, both fixed now:

1. **A response-parsing bug**: some NVRs insert a stray space
   (`"table. DisableLinkage.Enable=true"` instead of
   `"table.DisableLinkage.Enable=true"`) that the regex didn't tolerate.
2. **A wrong assumption about per-channel scope**: when a device only
   ever reports the bare, non-indexed `DisableLinkage.Enable` value (no
   `[N]` per channel at all), that single value now applies to *every*
   camera's Armed switch, not just channel 0. Some NVRs genuinely don't
   support distinct per-channel arming — they have one device-wide
   value, and every camera's switch correctly reflects (and controls)
   that same underlying state rather than showing "unsupported" for
   anything past the first channel.

**A `coaxialControlIO.cgi` 501 (Not Implemented) status** — seen for
White/Security Light and Siren, potentially on every channel including
the first — means this specific NVR doesn't support that endpoint at
all when proxied through it, which is a real hardware/firmware
limitation rather than a bug to keep retrying. If you want light/siren
control specifically, this may only work when Home Assistant is
pointed directly at the TiOC camera's own IP rather than through the
NVR — worth trying if it matters to you.

**A `PrivacyMasking.cgi` 400 (Bad Request) status** — unlike 501, a
400 doesn't unambiguously mean "not implemented"; it more often means
the request reached a real handler but something about it (e.g. this
specific channel, or this specific "enable/disable all" convenience
action versus per-shape masking) was rejected. Since the response body
is now included in the error, the next occurrence of this in your logs
should say why, rather than just the bare status code — worth checking
if this is still happening for you.

### Device page showing a "via_device" warning

Fixed: every camera device declared `via_device=(DOMAIN,
entry.entry_id)` (a parent "NVR" device) without that parent device
actually being registered anywhere, which Home Assistant correctly
flagged as referencing a non-existent device. The NVR itself is now
registered as an explicit device at setup, so each camera correctly
nests under it on the integration's device page.

## Audio File Playback (entities) + Play Audio File (service)

Two ways to trigger the same action — pick whichever fits your use
case.

**As entities** (enable the "Audio File Playback" feature on a
camera): `text.<camera>_audio_file_path` (defaults to `/Audio_Path`),
`text.<camera>_audio_file_name`, and a `button.<camera>_play_audio_file`
that plays whatever the two text fields currently hold. Update the
text fields, press the button.

**Important — there's no file browser, on purpose.** I looked
specifically for a "list the audio files already on the device" API,
across both the official doc and the rroller/dahua integration's
source. Neither has one — the doc has no directory-listing endpoint
that covers the alarm-audio folder (the one generic file-search CGI
that exists, `mediaFileFind.cgi`, only searches recorded video/
snapshot files — `dav`/`jpg`/`mp4` — not this folder at all), and
rroller/dahua's own issue tracker shows they hadn't even implemented
file-based playback as of their last check. So: type in the filename
you already know exists there (check the camera's own web UI, under
its Audio Management / alarm-audio page) rather than expecting a
dropdown — the text field is honest about being manual entry, not a
disguised picker.

**As a service** (works regardless of whether the feature/entities are
enabled): `dahua_face.play_audio_file`, via
`POST /cgi-bin/api/Speak/startPlayByTime`. Target a camera device, and
provide:

| Field | Default | Notes |
|---|---|---|
| `path` | `/Audio_Path` | Directory on the device containing the file. |
| `file_name` | *(required)* | e.g. `alarm.wav` — must already exist on the device; this does not upload one. |
| `play_times` | `1` | How many times to play it. |

**Important limitation, direct from the API doc:** this endpoint takes
**no channel parameter at all** — the device you target only
determines which NVR/camera *connection* the request is sent over, not
which channel's speaker plays it. This matches a standalone TiOC
camera addressed directly (one camera = one speaker), where it should
work exactly as expected. Pointed at a multi-channel NVR, there's no
documented way to route it to one specific camera's speaker — it will
play through whatever the device itself resolves it to.

## PTZ (Pan/Tilt/Zoom)

Enable the "PTZ (Pan/Tilt/Zoom)" feature on a camera (auto-detected —
see "Capability auto-detection" above — via a successful `ptz.cgi`
`getPresets` call, which only works on cameras with actual PTZ
hardware) to get:

- **6 movement buttons**: `button.<camera>_ptz_up` / `_down` / `_left`
  / `_right` / `_zoomwide` (Zoom In) / `_zoomtele` (Zoom Out). Each
  press is a brief "nudge" — move, wait ~0.5s, stop — since a button
  press has no natural "release" event the way a held joystick control
  would, and `ptz.cgi` needs an explicit stop or it keeps moving.
- **`select.<camera>_ptz_preset`**: lists the presets actually
  configured on the device (via `getPresets`, refreshed every 5
  minutes — these rarely change) as `"{index}: {name}"`.
- **`button.<camera>_ptz_goto_preset`**: moves to whichever preset is
  currently chosen above.
- **`button.<camera>_ptz_set_preset`**: saves the camera's *current*
  position into whichever preset is chosen above — **this overwrites
  it**; the button press is the confirmation, there's no separate
  "are you sure."

The preset select and its two action buttons are deliberately separate
entities rather than one combined control, so you can drive them from
automations (e.g. "select preset 3, then press Go to Preset") as
easily as from the dashboard.

**Channel numbering for `ptz.cgi` is 1-indexed** — the doc states this
explicitly throughout §8.1 ("channel starts from 1"), same convention
as `coaxialControlIO.cgi`, different from most of the rest of this
integration (0-indexed). Handled internally; nothing else needs to
account for it.

**Not yet implemented**, straightforward to add given the extensible
pattern the rest of this integration follows, if you want them:
continuous (held) movement with a separate explicit stop (vs. the
fixed-duration nudge above), diagonal movement, focus/iris control
(the doc documents these the same way as zoom), and preset
rename/delete.

## Custom Overlay (service, not an entity)

`dahua_face.set_custom_overlay` — sets up to 6 stacked lines of custom
on-screen-display (OSD) text on a specific camera. Target a camera
device:

```yaml
service: dahua_face.set_custom_overlay
target:
  device_id: <camera device id>
data:
  lines:
    - "Front Door"
    - "Temp: 20°C, Humidity: 50%"
```

This is deliberately a **service/action call**, not a text-input-box
entity — arbitrary text (spaces, `%`, `°`, other Unicode) is just a
normal string argument in the call, with no UI-input-box quirks to
work around, and it naturally extends to multiple lines rather than
being limited to one.

The API doc documents a single overlay box as supporting up to 2 lines
via a `|` separator within one `Text` field — this service doesn't use
that. Instead each line gets its **own** overlay box
(`VideoWidget[channel].UserDefinedTitle[0]` through `[5]`),
auto-positioned stacked top-to-bottom, so "N lines in" reliably means
"N lines shown" rather than depending on whether a given firmware
actually honors the `|` convention as documented. Fewer lines than
before clears the extras (so shrinking from 6 lines to 3 removes lines
4-6 rather than leaving them on screen).

**On encoding**: each line's text is percent-encoded via
`urllib.parse.quote` (which correctly UTF-8-encodes arbitrary
characters — verified against spaces, `%`, and `°` directly), and the
underlying HTTP client builds the request URL with `encoded=True` so
nothing re-parses/re-encodes that already-correct encoding a second
time — this removes any risk of double-encoding regardless of the
installed aiohttp/yarl version.

Channel numbering for `VideoWidget` is 0-indexed, same as most of this
integration (the doc explicitly states its array is 0-indexed by
channel) — unlike PTZ/coaxial control elsewhere on this page.

## Privacy Mask

One `switch.<camera>_privacy_mask` entity per camera with the "Privacy
Mask" feature enabled (auto-detected, via `PrivacyMasking.cgi`'s
`getPrivacyMaskingEnable`). This is a single master on/off for whatever
mask shapes (blocked-out regions) are already configured on the camera
— via `PrivacyMasking.cgi`'s `setPrivacyMaskingEnable`. It does **not**
create, resize, or delete the mask shapes themselves — draw those from
the camera's own web UI first; this switch only toggles whether the
ones already there are active. Same 1-indexed channel numbering
caveat as PTZ/coaxial control.

## Alarm Input / Alarm Output (relay)

Different in scope from everything above: `alarm.cgi` (§4.9 of the API
doc) is **device-wide** — it takes no channel parameter at all, so
these entities aren't tied to any one camera. They're attached to the
**NVR hub device** instead (the same device other cameras show as
"via device" on their own device pages), and appear automatically —
no per-camera feature toggle — if the device reports at least one
input or output channel at all:

- **`binary_sensor.alarm_input_n`** (one per channel `alarm.cgi`'s
  `getInSlots` reports) — on/off reflecting whether that input is
  currently triggered.
- **`switch.alarm_output_n`** (one per channel `getOutSlots` reports)
  — a relay you can drive from Home Assistant, via
  `configManager.cgi`'s `AlarmOut[N].Mode`: on = force alarm (`Mode=1`),
  off = force closed (`Mode=2`). There's also a `Mode=0` "automatic/
  rule-driven" state the device can be in on its own, which this
  switch doesn't expose — turning it off always means "force closed",
  not "hand control back to the NVR's own automatic rules."
- Names come from `configManager.cgi`'s `Alarm[N].Name` /
  `AlarmOut[N].Name` if the device has them configured, else "Alarm
  Input N" / "Alarm Output N".

**State is push-based, not just polled** — both use the `AlarmLocal`
/ `AlarmOutput` event codes on the exact same shared event stream
already used for Face/IVS events, so a trigger/clear shows up in Home
Assistant within about as long as the NVR takes to report it, matching
the DMSS app and the rroller/dahua integration's responsiveness rather
than trailing behind by up to a full poll interval. (An earlier
version of this integration relied solely on a 15-second poll, which
measured out to a consistent ~10-11 second lag versus rroller/dahua in
side-by-side testing — the push-based event is what actually closes
that gap; the poll alone was never going to.) Polling is still there
underneath (every 60s) purely as a reconciliation safety net — e.g. if
the event stream was mid-reconnect when a trigger happened — not as
the primary update path anymore.

**Important scope caveat, stated plainly:** you asked for both the
recorder's own local alarm I/O and camera-side ("IPC EXT") alarm I/O.
I searched thoroughly — the full API doc, and the rroller/dahua
integration's source — for a distinct, channel-scoped endpoint that
addresses a specific camera's own alarm terminals separately from the
NVR's local ports, and found nothing: no channel parameter on any
`alarm.cgi` action, no separate config table name, nothing under an
"ExtAlarm"/"RemoteDevice" style name. What I implemented is the one
mechanism that's actually documented: a flat list of alarm input/
output channels, device-wide.

It's plausible this flat list already includes both local and IPC EXT
ports combined — Dahua's own NVR web UI often just groups a "Local"
tab and an "IPC External" tab over what may be one underlying
list — but I can't confirm that without hardware to test against.
Two ways to sanity-check this on yours, if you want to help nail it
down further:
1. Compare `binary_sensor.alarm_input_n`'s count against your NVR's
   physical local alarm-in terminal count. If there are more entities
   than physical terminals, the extras are very likely the IPC EXT
   ports.
2. Physically trigger a camera's own external alarm input (if it has
   one) and watch whether any `binary_sensor.alarm_input_n` reacts.

If it turns out IPC EXT ports need a genuinely separate mechanism
(e.g. addressing each camera directly rather than through the NVR,
the way `coaxialControlIO.cgi` sometimes needed to), let me know what
you find and I'll adjust — same as how the TiOC light/siren
implementation got corrected earlier in this conversation.

## Adding cameras/features without disrupting anything else

Adding a camera (via "Add Integration" pointed at the same NVR) or
enabling a new feature on an existing camera (via the **Configure**
button) does **not** reload the integration. An update listener in
`__init__.py` diffs the old and new camera/feature configuration and:

- Creates entities only for what's actually new, via each platform's
  stored `async_add_entities` callback — every existing entity is
  left completely alone (same entity_id, same registry name, same
  history).
- Restarts only the shared NVR event-stream connection if the channel
  list or subscribed event codes actually changed — and does so in a
  way that skips the "mark unavailable" step entirely for a
  deliberate restart (see `_restart_listener`'s docstring), so
  existing cameras keep showing their last-known values throughout
  rather than flickering unavailable.
- Cleans up properly in the other direction too: deselecting a camera
  removes its device (and cascades to its entities); disabling one
  feature on a camera that's still selected removes just that
  feature's entities, not the whole camera.

## Adding a new feature

Open `features.py` and add one entry to `FEATURE_REGISTRY`:

```python
"SomeNewDetection": Feature(
    code="SomeNewDetection",       # Event Code exactly as the NVR sends it
    name="Some New Detection",     # shown in the setup UI and entity name
    icon="mdi:something",
    attributes_fn=_object_attrs,   # or write a custom one for its data shape
),
```

That's it — it will automatically appear in the per-camera feature
picker, and any camera with it enabled gets a working sensor. No
changes needed to `sensor.py`, `config_flow.py`, or `__init__.py`. A
few small helper functions (`_object_attrs`, `_bbox`, etc.) are
already there to reuse for events that follow the common
`Object`/`Objects[]`/`BoundingBox` shape most IVS events share.

To remove a feature, delete its entry — cameras that had it enabled
simply stop getting that sensor after the next reload; nothing else
needs cleanup.

## Face Detection & Recognition sensors

| Sensor | Source | Notes |
|---|---|---|
| **Face Name** | `FaceRecognition` → `Candidates[].Person.Name` | Highest-similarity match. `"Unknown"` if a face was detected but not matched. |
| **Face Gender** | `Face.Sex` | `"Man"` / `"Woman"` / `"Unknown"`. |
| **Face List** | `FaceRecognition` → `Candidates[].Person.GroupID` | The face database/group the best match belongs to. `"Unknown"` when no match was made. |
| **Face Expression** | `Face.Feature[]` | Detected facial attributes (`WearGlasses`, `Smile`, etc.), comma-joined state, full list as attribute. |
| **Face Confidence** | `Candidates[].Similarity` | 1–100 (%). `0` on a plain detection with no match. |
| **Last Face Snapshot** (image entity) | `image/jpeg` part attached right after the event | Only updates if your NVR actually attaches an image to that event on this stream. |

## How it works

- On startup, **one** persistent HTTP connection per NVR is opened
  (covering every selected camera at once), subscribed only to the
  event codes actually needed — the union of every enabled feature
  across all of that NVR's cameras:
  `GET /cgi-bin/eventManager.cgi?action=attach&codes=[...]&channel=[0,1,2,...]`
  using HTTP **Digest** authentication (implemented manually — aiohttp
  has no built-in digest support).
- The response is a `multipart/x-mixed-replace`-style stream of
  boundary-delimited parts. `text/plain` parts (either the
  `Code=...;action=...;data={json}` format or the
  `Events[0].EventBaseInfo.Code=...` key/value format — both shown in
  the API doc, and both parsed into the same normalized shape) are
  read. Each event carries its own channel/`index` field, which is
  what actually determines which camera's sensors get updated — **not**
  which connection it arrived on, because some Dahua firmware doesn't
  honor the `channel=[...]` filter per connection and broadcasts every
  channel's events down any attach connection.
- When an event's `text/plain` part is immediately followed by an
  `image/jpeg` part on the stream, those bytes become that camera's
  **Last Face Snapshot**. If the next part is not an image, nothing is
  lost — it's processed as the next event on the following loop
  iteration.
- If the connection drops, it reconnects automatically with
  exponential backoff (5s → 60s); all cameras on that NVR briefly show
  unavailable together (they share one stream).

## Assumptions made / things to verify against your hardware

- **`rule_id` on Cross Line / Cross Region Detection** (`*` in the table
  above): the API doc pages provided don't show an explicit rule-ID
  field for either event — only `Object`/`Objects`, `DetectLine` /
  `DetectRegion`, `Direction`, and (for region) `Action`. Many other
  Dahua rule-based events do carry an identifying field, under one of a
  few different key names depending on firmware/event type. This
  integration checks the event's data for `Name`, `RuleId`, `RuleID`,
  `Id`, or `ID`, in that order, and exposes whichever is present as the
  `rule_id` attribute — if your NVR includes one of these fields on
  these two events, it'll show up automatically; if it uses a different
  key, or doesn't send one at all, add it to the list in `_rule_id()`
  near the top of `features.py`.
- **`CrossRegionDetection`'s Event Code**: in the API doc's §9.1.7
  section, the Event Code row and the response example both literally
  read `CrossLineDetection` — but the fields around it (`Action`:
  Appear/Disappear/Cross/Inside, `DetectRegion`) clearly belong to the
  *region* event already given its own section, not the *line* event
  covered in §9.1.6. This looks like a copy-paste artifact in Dahua's
  own documentation. This integration subscribes using the code
  `CrossRegionDetection` (matching the section title). If your NVR
  actually emits `CrossLineDetection` for both, that's one line to
  change in `features.py`'s registry entry (`code="CrossLineDetection"`)
  — though note it would then be indistinguishable from real
  cross-line events by Code alone; you'd need to disambiguate using
  the presence of `DetectRegion` vs `DetectLine` in the event data
  instead.
- The channel name lookup uses
  `configManager.cgi?action=getConfig&name=ChannelTitle` — if your
  NVR/firmware exposes channels differently, `get_channels()` in
  `api.py` is the only place that needs adjusting.
- `FaceDetection` events with more than one face in `Faces[]` at once:
  only the first is reflected in the Face sensors.

## Files

```
dahua_face/
├── __init__.py       # setup/unload, live update listener (add/remove
│                      # entities without reload), background reconnecting
│                      # listener task, computes which event codes to
│                      # subscribe to
├── api.py             # digest auth + multipart event stream client/parser
│                      # + configManager.cgi DisableLinkage arm/disarm calls
├── config_flow.py     # setup flow (connection → cameras → per-camera features)
│                      # + matching options flow
├── const.py
├── features.py         # ← the extension point: registry of IVS event types
├── sensor.py           # Face sensors + the generic per-feature IVS sensor
├── image.py            # Last Face Snapshot image entity
├── switch.py           # Armed / White-Security Light / Siren / Privacy Mask / alarm output switches
├── binary_sensor.py     # Alarm input contacts (NVR hub device)
├── select.py            # PTZ preset selector
├── button.py            # PTZ movement/preset buttons, Play Audio File button
├── text.py               # Audio file path/name (local state for the Play button)
├── manifest.json
├── services.yaml        # play_audio_file service description
├── strings.json
└── translations/en.json
```
