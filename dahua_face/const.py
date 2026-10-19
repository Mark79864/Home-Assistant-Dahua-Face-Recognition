"""Constants for the Dahua Face Recognition integration."""

DOMAIN = "dahua_face"

CONF_CAMERAS = "cameras"
# {channel_str: [feature_key, ...]} — which features (see features.py) are
# enabled for each camera. Feature keys are FEATURE_FACE or a
# FEATURE_REGISTRY key.
CONF_CAMERA_FEATURES = "camera_features"

DEFAULT_PORT = 80
DEFAULT_TIMEOUT = 15  # seconds, for normal (non-streaming) requests

# Dispatcher signal used to push a parsed event from the background
# listener task to the sensor entities for a given config entry + channel.
SIGNAL_FACE_EVENT = "dahua_face_event_{entry_id}_{channel}"

# Dispatcher signal used to notify sensors that a channel's stream
# connection state changed (connected/disconnected), so entities can
# reflect availability.
SIGNAL_FACE_AVAILABILITY = "dahua_face_availability_{entry_id}_{channel}"

# Dispatcher signal used to push a new snapshot (jpeg bytes) of the
# last-seen face to the per-camera image entity.
SIGNAL_FACE_SNAPSHOT = "dahua_face_snapshot_{entry_id}_{channel}"

# Dispatcher signal telling the Face sensors for a camera to revert to
# the "Clear" resting state — fired after FACE_CLEAR_TIMEOUT seconds have
# passed with no new FaceDetection/FaceRecognition event on that channel.
# A new face event before the timeout cancels/reschedules it rather than
# firing, so "Clear" only shows once nothing has been seen for a while.
SIGNAL_FACE_CLEAR = "dahua_face_clear_{entry_id}_{channel}"
FACE_CLEAR_TIMEOUT = 5  # seconds

# Push updates for local alarm input/output, via the "AlarmLocal" /
# "AlarmOutput" event codes on the same shared event stream used for
# Face/IVS events (see __init__.py's _listen_forever). These aren't
# scoped to a camera channel — the index in the signal is the alarm
# input/output slot number (see api.py's get_alarm_in_slots etc.), not
# a video channel. Using the push stream instead of relying solely on
# polling (SCAN_INTERVAL in binary_sensor.py/switch.py, kept as a
# reconciliation safety net) is what gets these entities to near-
# real-time responsiveness, matching the DMSS app / rroller/dahua
# instead of trailing by up to one poll interval.
SIGNAL_ALARM_INPUT = "dahua_alarm_input_{entry_id}_{index}"
SIGNAL_ALARM_OUTPUT = "dahua_alarm_output_{entry_id}_{index}"

ATTR_CHANNEL = "channel"
ATTR_UID = "uid"
ATTR_GROUP_ID = "group_id"
ATTR_SIMILARITY = "similarity"
ATTR_FEATURES = "features"
ATTR_AGE = "age"
ATTR_EYE = "eye"
ATTR_MOUTH = "mouth"
ATTR_MASK = "mask"
ATTR_BEARD = "beard"
ATTR_CANDIDATES = "candidates"
ATTR_LAST_EVENT_CODE = "last_event_code"

UNKNOWN_NAME = "Unknown"
UNKNOWN_GENDER = "Unknown"
CLEAR_STATE = "Clear"

RECONNECT_DELAY_MIN = 5
RECONNECT_DELAY_MAX = 60
