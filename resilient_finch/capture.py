from __future__ import annotations

import ctypes
import logging
import queue
import uuid as _uuid_mod
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime

import numpy as np
import sounddevice as sd
from numpy.typing import NDArray

from . import config

logger = logging.getLogger(__name__)

# ── CoreAudio / ObjC ctypes bindings ─────────────────────────────────────────

_CF = ctypes.CDLL("/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")
_CA = ctypes.CDLL("/System/Library/Frameworks/CoreAudio.framework/CoreAudio")
_objc = ctypes.CDLL("/usr/lib/libobjc.dylib")

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


class _AudioStreamBasicDescription(ctypes.Structure):
    _fields_ = [
        ("mSampleRate", ctypes.c_double),
        ("mFormatID", ctypes.c_uint32),
        ("mFormatFlags", ctypes.c_uint32),
        ("mBytesPerPacket", ctypes.c_uint32),
        ("mFramesPerPacket", ctypes.c_uint32),
        ("mBytesPerFrame", ctypes.c_uint32),
        ("mChannelsPerFrame", ctypes.c_uint32),
        ("mBitsPerChannel", ctypes.c_uint32),
        ("mReserved", ctypes.c_uint32),
    ]


class _AudioBuffer(ctypes.Structure):
    _fields_ = [
        ("mNumberChannels", ctypes.c_uint32),
        ("mDataByteSize", ctypes.c_uint32),
        ("mData", ctypes.c_void_p),
    ]


class _AudioBufferList(ctypes.Structure):
    _fields_ = [
        ("mNumberBuffers", ctypes.c_uint32),
        ("mBuffers", _AudioBuffer * 1),
    ]


# mBuffers is 8 bytes into AudioBufferList due to alignment padding after mNumberBuffers
_AUDIO_BUFFERS_OFFSET: int = _AudioBufferList.mBuffers.offset


# IOProc callback type: (device, now, inputData, inputTime, outputData, outputTime, clientData) -> OSStatus
_IOProcFuncType = ctypes.CFUNCTYPE(
    _OSStatus,
    _AudioObjID,
    ctypes.c_void_p,
    ctypes.c_void_p,
    ctypes.c_void_p,
    ctypes.c_void_p,
    ctypes.c_void_p,
    ctypes.c_void_p,
)

# ObjC runtime
_objc.objc_getClass.restype = ctypes.c_void_p
_objc.objc_getClass.argtypes = [ctypes.c_char_p]
_objc.sel_registerName.restype = ctypes.c_void_p
_objc.sel_registerName.argtypes = [ctypes.c_char_p]

# CF
_kCFStringEncodingUTF8: int = 0x08000100
_kCFTypeDictKeyCB = (ctypes.c_byte * 1).in_dll(_CF, "kCFTypeDictionaryKeyCallBacks")
_kCFTypeDictValCB = (ctypes.c_byte * 1).in_dll(_CF, "kCFTypeDictionaryValueCallBacks")
_kCFTypeArrCB = (ctypes.c_byte * 1).in_dll(_CF, "kCFTypeArrayCallBacks")

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
_CF.CFNumberCreate.restype = _CFTypeRef
_CF.CFNumberCreate.argtypes = [_CFTypeRef, ctypes.c_long, ctypes.c_void_p]
_CF.CFRelease.restype = None
_CF.CFRelease.argtypes = [_CFTypeRef]

_kCFNumberSInt32Type = 3

# CoreAudio
_kAudioObjectPropertyScopeGlobal = _fcc("glob")
_kAudioObjectPropertyElementMain = 0
_kAudioTapPropertyUID = _fcc("tuid")
_kAudioTapPropertyFormat = _fcc("tfmt")

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
_CA.AudioDeviceCreateIOProcID.restype = _OSStatus
_CA.AudioDeviceCreateIOProcID.argtypes = [
    _AudioObjID,
    _IOProcFuncType,
    ctypes.c_void_p,
    ctypes.POINTER(ctypes.c_void_p),
]
_CA.AudioDeviceDestroyIOProcID.restype = _OSStatus
_CA.AudioDeviceDestroyIOProcID.argtypes = [_AudioObjID, ctypes.c_void_p]
_CA.AudioDeviceStart.restype = _OSStatus
_CA.AudioDeviceStart.argtypes = [_AudioObjID, ctypes.c_void_p]
_CA.AudioDeviceStop.restype = _OSStatus
_CA.AudioDeviceStop.argtypes = [_AudioObjID, ctypes.c_void_p]


# ── Helpers ───────────────────────────────────────────────────────────────────


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
    length = _CF.CFStringGetLength(ref.value)
    buf = ctypes.create_string_buffer((length + 1) * 4)
    _CF.CFStringGetCString(ref.value, buf, len(buf), _kCFStringEncodingUTF8)
    _CF.CFRelease(ref.value)
    return buf.value.decode("utf-8")


def _cf_str(s: str) -> int:
    ref = _CF.CFStringCreateWithCString(None, s.encode("utf-8"), _kCFStringEncodingUTF8)
    if not ref:
        raise RuntimeError(f"CFStringCreateWithCString failed for {s!r}")
    return ref


def _cf_dict_new() -> int:
    return _CF.CFDictionaryCreateMutable(
        None,
        0,
        ctypes.addressof(_kCFTypeDictKeyCB),
        ctypes.addressof(_kCFTypeDictValCB),
    )


def _cf_array_new() -> int:
    return _CF.CFArrayCreateMutable(None, 0, ctypes.addressof(_kCFTypeArrCB))


def _dict_set_str(d: int, key: str, val: str) -> None:
    k = _cf_str(key)
    v = _cf_str(val)
    _CF.CFDictionarySetValue(d, k, v)
    _CF.CFRelease(k)
    _CF.CFRelease(v)


def _dict_set_int(d: int, key: str, val: int) -> None:
    k = _cf_str(key)
    v_raw = ctypes.c_int32(val)
    v = _CF.CFNumberCreate(None, _kCFNumberSInt32Type, ctypes.byref(v_raw))
    _CF.CFDictionarySetValue(d, k, v)
    _CF.CFRelease(k)
    _CF.CFRelease(v)


def _dict_set_ref(d: int, key: str, val: int) -> None:
    k = _cf_str(key)
    _CF.CFDictionarySetValue(d, k, val)
    _CF.CFRelease(k)


def _create_tap_aggregate(tap_uid: str) -> int:
    tap_entry = _cf_dict_new()
    _dict_set_str(tap_entry, "uid", tap_uid)

    taps_arr = _cf_array_new()
    _CF.CFArrayAppendValue(taps_arr, tap_entry)
    _CF.CFRelease(tap_entry)

    subdevices_arr = _cf_array_new()

    agg_uid = f"com.resilient-finch.tap-agg-{_uuid_mod.uuid4()}"
    desc = _cf_dict_new()
    _dict_set_str(desc, "uid", agg_uid)
    _dict_set_str(desc, "name", "Resilient Finch System Audio")
    _dict_set_ref(desc, "subdevices", subdevices_arr)
    _dict_set_ref(desc, "taps", taps_arr)
    _dict_set_int(desc, "private", 1)  # required when using tapautostart
    _dict_set_int(desc, "tapautostart", 1)  # wait for first tap audio before clocking
    _CF.CFRelease(subdevices_arr)
    _CF.CFRelease(taps_arr)

    agg_id = _AudioObjID(0)
    status = _CA.AudioHardwareCreateAggregateDevice(desc, ctypes.byref(agg_id))
    _CF.CFRelease(desc)

    if status != 0:
        raise RuntimeError(f"AudioHardwareCreateAggregateDevice failed: OSStatus {status:#010x}")

    logger.debug("Tap aggregate device created: id=%d", agg_id.value)
    return agg_id.value


def _destroy_tap_aggregate(agg_id: int) -> None:
    status = _CA.AudioHardwareDestroyAggregateDevice(agg_id)
    if status != 0:
        logger.warning("AudioHardwareDestroyAggregateDevice failed: OSStatus %#010x", status)


def _get_tap_format(tap_id: int) -> _AudioStreamBasicDescription:
    addr = _PropAddr(
        _kAudioTapPropertyFormat, _kAudioObjectPropertyScopeGlobal, _kAudioObjectPropertyElementMain
    )
    asbd = _AudioStreamBasicDescription()
    size = ctypes.c_uint32(ctypes.sizeof(asbd))
    status = _CA.AudioObjectGetPropertyData(
        tap_id,
        ctypes.byref(addr),
        0,
        None,
        ctypes.byref(size),
        ctypes.byref(asbd),
    )
    if status != 0:
        raise RuntimeError(f"Failed to get tap format: OSStatus {status:#010x}")
    return asbd


def _msg(obj: int, sel: str, *args: int) -> int:
    _objc.objc_msgSend.restype = ctypes.c_void_p
    _objc.objc_msgSend.argtypes = [ctypes.c_void_p, ctypes.c_void_p] + [ctypes.c_void_p] * len(args)
    return _objc.objc_msgSend(obj, _objc.sel_registerName(sel.encode()), *args)


# ── Process tap ────────────────────────────────────────────────────────────────


def _create_process_tap() -> tuple[int, str, _AudioStreamBasicDescription]:
    ns_array_cls = _objc.objc_getClass(b"NSArray")
    empty_arr = _msg(ns_array_cls, "array")
    tap_desc_cls = _objc.objc_getClass(b"CATapDescription")
    tap_desc = _msg(
        _msg(tap_desc_cls, "alloc"),
        "initStereoGlobalTapButExcludeProcesses:",
        empty_arr,
    )
    if not tap_desc:
        raise RuntimeError("CATapDescription init returned nil")

    tap_id = _AudioObjID(0)
    status = _CA.AudioHardwareCreateProcessTap(tap_desc, ctypes.byref(tap_id))
    if status != 0:
        msg = (
            f"AudioHardwareCreateProcessTap failed: OSStatus {status:#010x}\n"
            "Grant 'Screen & System Audio Recording' in System Settings → Privacy & Security."
        )
        raise RuntimeError(msg)

    tap_uid = _read_cfstr_prop(tap_id.value, _kAudioTapPropertyUID)
    if not tap_uid:
        raise RuntimeError("Failed to read tap UID after creation")
    fmt = _get_tap_format(tap_id.value)
    logger.debug(
        "Process tap created: id=%d uid=%r %.0f Hz %d ch",
        tap_id.value,
        tap_uid,
        fmt.mSampleRate,
        fmt.mChannelsPerFrame,
    )
    return tap_id.value, tap_uid, fmt


def _destroy_process_tap(tap_id: int) -> None:
    status = _CA.AudioHardwareDestroyProcessTap(tap_id)
    if status != 0:
        logger.warning("AudioHardwareDestroyProcessTap failed: OSStatus %#010x", status)


# ── IOProc speaker capture ─────────────────────────────────────────────────────


def _make_tap_ioproc(
    speaker_queue: queue.Queue[AudioChunk],
    source_rate: float,
) -> object:
    target_rate = config.SAMPLE_RATE
    need_resample = int(source_rate) != target_rate

    def _ioproc(
        _device: int,
        _now: int,
        in_data_ptr: int,
        _in_time: int,
        _out_data_ptr: int,
        _out_time: int,
        _client: int,
    ) -> int:
        if not in_data_ptr:
            return 0
        try:
            n_buffers = ctypes.c_uint32.from_address(in_data_ptr).value
            if n_buffers == 0:
                return 0
            buffers = (_AudioBuffer * n_buffers).from_address(in_data_ptr + _AUDIO_BUFFERS_OFFSET)

            channels: list[NDArray[np.float32]] = []
            for i in range(n_buffers):
                buf = buffers[i]
                if not buf.mData or buf.mDataByteSize == 0:
                    continue
                n_samples = buf.mDataByteSize // 4
                arr = np.frombuffer(
                    (ctypes.c_float * n_samples).from_address(buf.mData), dtype=np.float32
                ).copy()
                if buf.mNumberChannels > 1:
                    arr = arr.reshape(-1, buf.mNumberChannels).mean(axis=1).astype(np.float32)
                channels.append(arr)

            if not channels:
                return 0

            mono = (
                np.mean(channels, axis=0).astype(np.float32) if len(channels) > 1 else channels[0]
            )

            if need_resample:
                target_len = int(len(mono) * target_rate / source_rate)
                if target_len > 0:
                    mono = np.interp(
                        np.linspace(0, len(mono) - 1, target_len),
                        np.arange(len(mono)),
                        mono,
                    ).astype(np.float32)

            chunk = AudioChunk(data=mono, timestamp=datetime.now(tz=UTC), source="SPEAKER")
            try:
                speaker_queue.put_nowait(chunk)
            except queue.Full:
                logger.warning("[SPEAKER] audio queue full — dropping chunk")
        except Exception:  # noqa: BLE001
            pass  # never raise from a CoreAudio real-time callback
        return 0

    return _IOProcFuncType(_ioproc)


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
        mode: str = "mic_only",
        mic_gain: float = 1.0,
    ) -> None:
        self._mic_queue = mic_queue
        self._speaker_queue = speaker_queue
        self._mode = mode
        self._mic_gain = mic_gain
        self._mic_stream: sd.InputStream | None = None
        self._tap_id: int | None = None
        self._tap_agg_id: int | None = None
        self._tap_proc_id: int | None = None
        self._tap_ioproc_fn: object = None  # must stay alive (ctypes GC guard)

    def start(self) -> None:
        if self._mode == "mic_and_speaker":
            tap_id, tap_uid, tap_fmt = _create_process_tap()
            self._tap_id = tap_id

            # Wrap the tap in an aggregate device — the aggregate is a proper AudioDevice
            # that supports IOProc, whereas the tap AudioObject does not.
            agg_id = _create_tap_aggregate(tap_uid)
            self._tap_agg_id = agg_id

            self._tap_ioproc_fn = _make_tap_ioproc(self._speaker_queue, tap_fmt.mSampleRate)
            proc_id = ctypes.c_void_p(0)
            status = _CA.AudioDeviceCreateIOProcID(
                agg_id,
                self._tap_ioproc_fn,
                None,
                ctypes.byref(proc_id),
            )
            if status != 0:
                raise RuntimeError(f"AudioDeviceCreateIOProcID failed: OSStatus {status:#010x}")
            self._tap_proc_id = proc_id.value

            status = _CA.AudioDeviceStart(agg_id, self._tap_proc_id)
            if status != 0:
                raise RuntimeError(f"AudioDeviceStart (tap aggregate) failed: OSStatus {status:#010x}")

        mic_idx = (
            self._find_device_index(config.MIC_DEVICE_NAME) if config.MIC_DEVICE_NAME else None
        )
        self._mic_stream = sd.InputStream(
            device=mic_idx,
            channels=1,
            samplerate=config.SAMPLE_RATE,
            blocksize=config.BLOCK_SIZE_FRAMES,
            dtype="float32",
            callback=self._make_callback(self._mic_queue, "MIC", gain=self._mic_gain),
        )
        self._mic_stream.start()
        logger.info("Audio capture started (mode=%s, mic=%s)", self._mode, mic_idx)

    def stop(self) -> None:
        if self._mic_stream is not None:
            try:
                self._mic_stream.stop()
                self._mic_stream.close()
            except sd.PortAudioError:
                logger.debug("Mic stream cleanup error", exc_info=True)
            self._mic_stream = None

        if self._tap_agg_id is not None and self._tap_proc_id is not None:
            _CA.AudioDeviceStop(self._tap_agg_id, self._tap_proc_id)
            _CA.AudioDeviceDestroyIOProcID(self._tap_agg_id, self._tap_proc_id)
            self._tap_proc_id = None

        if self._tap_agg_id is not None:
            _destroy_tap_aggregate(self._tap_agg_id)
            self._tap_agg_id = None

        if self._tap_id is not None:
            _destroy_process_tap(self._tap_id)
            self._tap_id = None

        self._tap_ioproc_fn = None
        logger.info("Audio capture stopped")

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

    def _make_callback(
        self, q: queue.Queue[AudioChunk], source: str, gain: float = 1.0
    ) -> _CallbackFn:
        def callback(
            indata: NDArray[np.float32],
            _frames: int,
            _time: object,
            status: object,
        ) -> None:
            if status:
                logger.warning("[%s] sounddevice status: %s", source, status)
            mono: NDArray[np.float32] = indata.mean(axis=1) if indata.shape[1] > 1 else indata[:, 0]
            if gain != 1.0:
                mono = np.clip(mono * gain, -1.0, 1.0).astype(np.float32)
            chunk = AudioChunk(data=mono.copy(), timestamp=datetime.now(tz=UTC), source=source)
            try:
                q.put_nowait(chunk)
            except queue.Full:
                logger.warning("[%s] audio queue full — dropping chunk", source)

        return callback
