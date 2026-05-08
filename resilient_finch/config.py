from __future__ import annotations

import json as _json
import pathlib as _pathlib

# Audio capture
SAMPLE_RATE: int = 16_000
CHANNELS: int = 1
BLOCK_SIZE_FRAMES: int = 1_024
MIC_DEVICE_NAME: str | None = None
BLACKHOLE_DEVICE_NAME: str = "BlackHole 2ch"
AUDIO_QUEUE_MAXSIZE: int = 500

# Buffering / segmentation
SEGMENT_SECONDS: float = 20.0
SILENCE_FLUSH_SECONDS: float = 2.0
MAX_BUFFER_SECONDS: float = 30.0
SILENCE_RMS_THRESHOLD: float = 0.002
MIN_AUDIO_DURATION_SECONDS: float = 0.5

# Transcription
WHISPER_MODEL: str = "large-v3"
WHISPER_DEVICE: str = "cpu"
WHISPER_COMPUTE_TYPE: str = "int8"
WHISPER_BEAM_SIZE: int = 10
WHISPER_LANGUAGE: str = "en"
WHISPER_VAD_FILTER: bool = True
WHISPER_VAD_MIN_SPEECH_DURATION_MS: int = 250
WHISPER_VAD_MAX_SPEECH_DURATION_S: float = 30.0
WHISPER_TEMPERATURE: float = 0.0
WHISPER_CONDITION_ON_PREVIOUS_TEXT: bool = True
WHISPER_WORD_TIMESTAMPS: bool = False
TRANSCRIPTION_QUEUE_MAXSIZE: int = 2

# Acoustic Echo Cancellation
AEC_ENABLED: bool = True
AEC_FRAME_SAMPLES: int = 160  # 10 ms at 16 kHz
AEC_NUM_TAPS: int = 800  # 50 ms filter — covers typical desktop echo tail
AEC_STEP_SIZE: float = 0.05

# Session output
SESSIONS_DIR: str = "sessions"
SESSION_FILENAME_FORMAT: str = "session_%Y%m%d_%H%M%S.txt"

# Outputs: one or more of "text_file", "google_docs"
OUTPUTS: list[str] = ["text_file"]

# Google Docs output
# Set to an existing doc ID to write all sessions there as new tabs.
# If None, the ID is auto-created on first run and saved to ~/.resilient-finch/state.json.
GOOGLE_DOCS_DOC_ID: str | None = None
GOOGLE_SERVICE_ACCOUNT_PATH: str = "~/.resilient-finch/service_account.json"

# Load user-level overrides from ~/.resilient-finch/config.json.
# Supported keys: outputs, google_docs_doc_id, google_service_account_path,
#                 blackhole_device_name, aec_enabled, aec_num_taps, aec_step_size
_user_config_path = _pathlib.Path("~/.resilient-finch/config.json").expanduser()
if _user_config_path.exists():
    try:
        _user_config: dict[str, object] = _json.loads(_user_config_path.read_text())
        if "outputs" in _user_config:
            _outputs = _user_config["outputs"]
            if isinstance(_outputs, list):
                OUTPUTS = [str(x) for x in _outputs]
        if "google_docs_doc_id" in _user_config:
            _doc_id = _user_config["google_docs_doc_id"]
            if isinstance(_doc_id, str):
                GOOGLE_DOCS_DOC_ID = _doc_id
        if "google_service_account_path" in _user_config:
            GOOGLE_SERVICE_ACCOUNT_PATH = str(_user_config["google_service_account_path"])
        if "blackhole_device_name" in _user_config:
            BLACKHOLE_DEVICE_NAME = str(_user_config["blackhole_device_name"])
        if "aec_enabled" in _user_config:
            AEC_ENABLED = bool(_user_config["aec_enabled"])
        if "aec_num_taps" in _user_config:
            _taps = _user_config["aec_num_taps"]
            if isinstance(_taps, int):
                AEC_NUM_TAPS = _taps
        if "aec_step_size" in _user_config:
            _step = _user_config["aec_step_size"]
            if isinstance(_step, float | int):
                AEC_STEP_SIZE = float(_step)
    except (_json.JSONDecodeError, OSError):
        pass
