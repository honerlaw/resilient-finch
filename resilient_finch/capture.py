from __future__ import annotations

import ctypes
import logging
import queue
import time
import uuid as _uuid_mod
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime

import numpy as np
import sounddevice as sd
from numpy.typing import NDArray

from . import config

logger = logging.getLogger(__name__)

# ── CoreFoundation / CoreAudio ctypes bindings ────────────────────────────────

_CF = ctypes.CDLL("/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")
_CA = ctypes.CDLL("/System/Library/Frameworks/CoreAudio.framework/CoreAudio")

_OSStatus = ctypes.c_int32
_AudioObjID = ctypes.c_uint32
_CFTypeRef = ctypes.c_void_p


def _fcc(s: str) -> int:
    return sum(ord(c) << (24 - 8 * i) for i, c in enumerate(s))


class _PropAddr(ctypes.Structure):
    _fields_ = [
        ("mSelector", ctypes.c_uint32),
        ("mScope", ctypes.c_uint32),
        ("mElement", ctypes.c_uint32),
    ]


_kCFBooleanTrue = ctypes.c_void_p.in_dll(_CF, "kCFBooleanTrue")
_kCFBooleanFalse = ctypes.c_void_p.in_dll(_CF, "kCFBooleanFalse")
_kCFTypeDictKeyCB = (ctypes.c_byte * 1).in_dll(_CF, "kCFTypeDictionaryKeyCallBacks")
_kCFTypeDictValCB = (ctypes.c_byte * 1).in_dll(_CF, "kCFTypeDictionaryValueCallBacks")
_kCFTypeArrCB = (ctypes.c_byte * 1).in_dll(_CF, "kCFTypeArrayCallBacks")

_kCFStringEncodingUTF8: int = 0x08000100
_kAudioObjectPropertyScopeGlobal = _fcc("glob")
_kAudioObjectPropertyElementMain = 0
# Property that returns the UID string for a process tap AudioObject
_kAudioTapPropertyUID = _fcc("tuid")

# CF functions
_CF.CFStringCreateWithCString.restype = _CFTypeRef
_CF.CFStringCreateWithCString.argtypes = [_CFTypeRef, ctypes.c_char_p, ctypes.c_uint32]
_CF.CFStringGetLength.restype = ctypes.c_long
_CF.CFStringGetLength.argtypes = [_CFTypeRef]
_CF.CFStringGetCString.restype = ctypes.c_bool
_CF.CFStringGetCString.argtypes = [_CFTypeRef, ctypes.c_char_p, ctypes.c_long, ctypes.c_uint32]
_CF.CFDictionaryCreateMutable.restype = _CFTypeRef
_CF.CFDictionaryCreateMutable.argtypes = [
    _CFTypeRef,
    ctypes.c_long,
    ctypes.c_void_p,
    ctypes.c_void_p,
]
_CF.CFDictionarySetValue.restype = None
_CF.CFDictionarySetValue.argtypes = [_CFTypeRef, _CFTypeRef, _CFTypeRef]
_CF.CFArrayCreateMutable.restype = _CFTypeRef
_CF.CFArrayCreateMutable.argtypes = [_CFTypeRef, ctypes.c_long, ctypes.c_void_p]
_CF.CFArrayAppendValue.restype = None
_CF.CFArrayAppendValue.argtypes = [_CFTypeRef, _CFTypeRef]
_CF.CFRelease.restype = None
_CF.CFRelease.argtypes = [_CFTypeRef]

# CA functions
_CA.AudioObjectGetPropertyData.restype = _OSStatus
_CA.AudioObjectGetPropertyData.argtypes = [
    _AudioObjID,
    ctypes.POINTER(_PropAddr),
    ctypes.c_uint32,
    ctypes.c_void_p,
    ctypes.POINTER(ctypes.c_uint32),
    ctypes.c_void_p,
]
_CA.AudioHardwareCreateProcessTap.restype = _OSStatus
_CA.AudioHardwareCreateProcessTap.argtypes = [_CFTypeRef, ctypes.POINTER(_AudioObjID)]
_CA.AudioHardwareDestroyProcessTap.restype = _OSStatus
_CA.AudioHardwareDestroyProcessTap.argtypes = [_AudioObjID]
_CA.AudioHardwareCreateAggregateDevice.restype = _OSStatus
_CA.AudioHardwareCreateAggregateDevice.argtypes = [_CFTypeRef, ctypes.POINTER(_AudioObjID)]
_CA.AudioHardwareDestroyAggregateDevice.restype = _OSStatus
_CA.AudioHardwareDestroyAggregateDevice.argtypes = [_AudioObjID]

# ── CF helpers ─────────────────────────────────────────────────────────────────


def _cf_str(s: str) -> int:
    ref = _CF.CFStringCreateWithCString(None, s.encode("utf-8"), _kCFStringEncodingUTF8)
    if not ref:
        raise RuntimeError(f"CFStringCreateWithCString failed for {s!r}")
    return ref


def _cf_str_to_py(ref: int) -> str:
    length = _CF.CFStringGetLength(ref)
    buf = ctypes.create_string_buffer((length + 1) * 4)
    ok = _CF.CFStringGetCString(ref, buf, len(buf), _kCFStringEncodingUTF8)
    return buf.value.decode("utf-8") if ok else ""


def _cf_dict_new() -> int:
    ref = _CF.CFDictionaryCreateMutable(
        None,
        0,
        ctypes.addressof(_kCFTypeDictKeyCB),
        ctypes.addressof(_kCFTypeDictValCB),
    )
    if not ref:
        raise RuntimeError("CFDictionaryCreateMutable failed")
    return ref


def _cf_array_new() -> int:
    ref = _CF.CFArrayCreateMutable(None, 0, ctypes.addressof(_kCFTypeArrCB))
    if not ref:
        raise RuntimeError("CFArrayCreateMutable failed")
    return ref


def _dict_set_str(d: int, key: str, val: str) -> None:
    k = _cf_str(key)
    v = _cf_str(val)
    _CF.CFDictionarySetValue(d, k, v)
    _CF.CFRelease(k)
    _CF.CFRelease(v)


def _dict_set_bool(d: int, key: str, val: bool) -> None:
    k = _cf_str(key)
    _CF.CFDictionarySetValue(d, k, _kCFBooleanTrue if val else _kCFBooleanFalse)
    _CF.CFRelease(k)


def _dict_set_ref(d: int, key: str, val: int) -> None:
    k = _cf_str(key)
    _CF.CFDictionarySetValue(d, k, val)
    _CF.CFRelease(k)


def _read_cfstr_prop(obj_id: int, selector: int) -> str:
    addr = _PropAddr(selector, _kAudioObjectPropertyScopeGlobal, _kAudioObjectPropertyElementMain)
    ref = ctypes.c_void_p(0)
    size = ctypes.c_uint32(ctypes.sizeof(ref))
    status = _CA.AudioObjectGetPropertyData(
        obj_id,
        ctypes.byref(addr),
        0,
        None,
        ctypes.byref(size),
        ctypes.byref(ref),
    )
    if status != 0 or not ref.value:
        return ""
    result = _cf_str_to_py(ref.value)
    _CF.CFRelease(ref.value)
    return result


# ── Process tap ────────────────────────────────────────────────────────────────

_TAP_AGGREGATE_NAME = "Resilient Finch System Audio"
_DEVICE_SEARCH_RETRIES = 10
_DEVICE_RETRY_DELAY_S = 0.2


def _create_process_tap() -> tuple[int, str]:
    tap_uuid = str(_uuid_mod.uuid4()).upper()

    desc = _cf_dict_new()
    # Keys from CATapDescription / AudioHardware.h (macOS 14.2+)
    _dict_set_str(desc, "uuid", tap_uuid)
    _dict_set_str(desc, "name", _TAP_AGGREGATE_NAME)
    _dict_set_bool(desc, "mixd", True)  # stereo mixdown of all output processes
    prcs = _cf_array_new()
    _dict_set_ref(desc, "prcs", prcs)  # empty array = tap all processes
    _CF.CFRelease(prcs)

    tap_id = _AudioObjID(0)
    status = _CA.AudioHardwareCreateProcessTap(desc, ctypes.byref(tap_id))
    _CF.CFRelease(desc)

    if status != 0:
        msg = (
            f"AudioHardwareCreateProcessTap failed: OSStatus {status:#010x}\n"
            "Grant 'Screen & System Audio Recording' in System Settings → Privacy & Security."
        )
        raise RuntimeError(msg)

    tap_uid = _read_cfstr_prop(tap_id.value, _kAudioTapPropertyUID) or tap_uuid
    logger.debug("Process tap created: id=%d uid=%r", tap_id.value, tap_uid)
    return tap_id.value, tap_uid


def _create_tap_aggregate(tap_uid: str) -> int:
    agg_uid = f"com.resilient-finch.tap-agg-{_uuid_mod.uuid4()}"

    tap_entry = _cf_dict_new()
    _dict_set_str(tap_entry, "uid", tap_uid)

    taps_arr = _cf_array_new()
    _CF.CFArrayAppendValue(taps_arr, tap_entry)
    _CF.CFRelease(tap_entry)

    # Aggregate device with the process tap as the sole input source.
    # Subdevices is empty; the tap goes in the "taps" list.
    subdevices_arr = _cf_array_new()

    desc = _cf_dict_new()
    _dict_set_str(desc, "uid", agg_uid)
    _dict_set_str(desc, "name", _TAP_AGGREGATE_NAME)
    _dict_set_ref(desc, "subdevices", subdevices_arr)
    _dict_set_ref(desc, "taps", taps_arr)
    _CF.CFRelease(subdevices_arr)
    _CF.CFRelease(taps_arr)

    agg_id = _AudioObjID(0)
    status = _CA.AudioHardwareCreateAggregateDevice(desc, ctypes.byref(agg_id))
    _CF.CFRelease(desc)

    if status != 0:
        raise RuntimeError(
            f"AudioHardwareCreateAggregateDevice (tap) failed: OSStatus {status:#010x}"
        )

    logger.debug("Tap aggregate device created: id=%d", agg_id.value)
    return agg_id.value


def _destroy_process_tap(tap_id: int) -> None:
    status = _CA.AudioHardwareDestroyProcessTap(tap_id)
    if status != 0:
        logger.warning("AudioHardwareDestroyProcessTap failed: OSStatus %#010x", status)


def _destroy_aggregate(agg_id: int) -> None:
    status = _CA.AudioHardwareDestroyAggregateDevice(agg_id)
    if status != 0:
        logger.warning("AudioHardwareDestroyAggregateDevice failed: OSStatus %#010x", status)


# ── Public API ─────────────────────────────────────────────────────────────────

type _CallbackFn = Callable[[NDArray[np.float32], int, object, object], None]


@dataclass
class AudioChunk:
    data: NDArray[np.float32]
    timestamp: datetime
    source: str


class AudioCapturer:
    def __init__(
        self,
        mic_queue: queue.Queue[AudioChunk],
        speaker_queue: queue.Queue[AudioChunk],
    ) -> None:
        self._mic_queue = mic_queue
        self._speaker_queue = speaker_queue
        self._mic_stream: sd.InputStream | None = None
        self._speaker_stream: sd.InputStream | None = None
        self._tap_id: int | None = None
        self._tap_agg_id: int | None = None

    def start(self) -> None:
        tap_id, tap_uid = _create_process_tap()
        self._tap_id = tap_id
        self._tap_agg_id = _create_tap_aggregate(tap_uid)

        tap_idx = self._wait_for_device(_TAP_AGGREGATE_NAME)
        mic_idx = (
            self._find_device_index(config.MIC_DEVICE_NAME) if config.MIC_DEVICE_NAME else None
        )

        self._mic_stream = sd.InputStream(
            device=mic_idx,
            channels=1,
            samplerate=config.SAMPLE_RATE,
            blocksize=config.BLOCK_SIZE_FRAMES,
            dtype="float32",
            callback=self._make_callback(self._mic_queue, "MIC"),
        )
        self._speaker_stream = sd.InputStream(
            device=tap_idx,
            channels=None,
            samplerate=config.SAMPLE_RATE,
            blocksize=config.BLOCK_SIZE_FRAMES,
            dtype="float32",
            callback=self._make_callback(self._speaker_queue, "SPEAKER"),
        )
        self._mic_stream.start()
        self._speaker_stream.start()
        logger.info("Audio capture started (mic=%s, tap aggregate idx=%d)", mic_idx, tap_idx)

    def stop(self) -> None:
        for stream in (self._mic_stream, self._speaker_stream):
            if stream is not None:
                try:
                    stream.stop()
                    stream.close()
                except sd.PortAudioError:
                    logger.debug("Stream cleanup error", exc_info=True)
        self._mic_stream = None
        self._speaker_stream = None

        if self._tap_agg_id is not None:
            _destroy_aggregate(self._tap_agg_id)
            self._tap_agg_id = None
        if self._tap_id is not None:
            _destroy_process_tap(self._tap_id)
            self._tap_id = None

        logger.info("Audio capture stopped")

    def _wait_for_device(self, name: str) -> int:
        for _ in range(_DEVICE_SEARCH_RETRIES):
            try:
                return self._find_device_index(name)
            except RuntimeError:
                time.sleep(_DEVICE_RETRY_DELAY_S)
        # Last resort: reinitialize PortAudio to force a device rescan
        sd._terminate()
        sd._initialize()
        return self._find_device_index(name)

    def _find_device_index(self, name: str) -> int:
        devices = sd.query_devices()
        name_lower = name.lower()
        for i, dev in enumerate(devices):
            if name_lower in dev["name"].lower() and dev["max_input_channels"] > 0:
                return i
        available = [
            f"  [{i}] {d['name']} (in={d['max_input_channels']})"
            for i, d in enumerate(devices)
            if d["max_input_channels"] > 0
        ]
        msg = f"Input device '{name}' not found.\nAvailable input devices:\n" + "\n".join(available)
        raise RuntimeError(msg)

    def _make_callback(self, q: queue.Queue[AudioChunk], source: str) -> _CallbackFn:
        def callback(
            indata: NDArray[np.float32],
            _frames: int,
            _time: object,
            status: object,
        ) -> None:
            if status:
                logger.warning("[%s] sounddevice status: %s", source, status)
            mono: NDArray[np.float32] = indata.mean(axis=1) if indata.shape[1] > 1 else indata[:, 0]
            chunk = AudioChunk(data=mono.copy(), timestamp=datetime.now(tz=UTC), source=source)
            try:
                q.put_nowait(chunk)
            except queue.Full:
                logger.warning("[%s] audio queue full — dropping chunk", source)

        return callback
