"""
Audio device setup for resilient-finch.

Creates:
  - "Resilient Finch Output"  — Multi-Output Device (speakers + BlackHole 2ch)
                                 Set as the system default output so audio plays
                                 through speakers AND is captured by BlackHole.
  - "Resilient Finch Capture" — Aggregate Device (BlackHole 2ch only, 16 kHz)
                                 What resilient-finch reads from.

Writes the capture device name to ~/.resilient-finch/config.json so the app
picks it up automatically. Idempotent: safe to run more than once.
"""
from __future__ import annotations

import ctypes
import json
import pathlib
import sys

# ── Load frameworks ────────────────────────────────────────────────────────────

_CF = ctypes.CDLL("/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")
_CA = ctypes.CDLL("/System/Library/Frameworks/CoreAudio.framework/CoreAudio")

# ── Types ──────────────────────────────────────────────────────────────────────

_OSStatus    = ctypes.c_int32
_AudioObjID  = ctypes.c_uint32
_CFTypeRef   = ctypes.c_void_p


class _PropAddr(ctypes.Structure):
    _fields_ = [("mSelector", ctypes.c_uint32),
                ("mScope",    ctypes.c_uint32),
                ("mElement",  ctypes.c_uint32)]


# ── CoreFoundation global callback structs (we only need their addresses) ─────

_kCFTypeDictKeyCB = (ctypes.c_byte * 1).in_dll(_CF, "kCFTypeDictionaryKeyCallBacks")
_kCFTypeDictValCB = (ctypes.c_byte * 1).in_dll(_CF, "kCFTypeDictionaryValueCallBacks")
_kCFTypeArrCB     = (ctypes.c_byte * 1).in_dll(_CF, "kCFTypeArrayCallBacks")

# ── CoreFoundation function signatures ────────────────────────────────────────

_CF.CFStringCreateWithCString.restype  = _CFTypeRef
_CF.CFStringCreateWithCString.argtypes = [_CFTypeRef, ctypes.c_char_p, ctypes.c_uint32]

_CF.CFStringGetLength.restype  = ctypes.c_long
_CF.CFStringGetLength.argtypes = [_CFTypeRef]

_CF.CFStringGetCString.restype  = ctypes.c_bool
_CF.CFStringGetCString.argtypes = [_CFTypeRef, ctypes.c_char_p, ctypes.c_long, ctypes.c_uint32]

_CF.CFNumberCreate.restype  = _CFTypeRef
_CF.CFNumberCreate.argtypes = [_CFTypeRef, ctypes.c_long, ctypes.c_void_p]

_CF.CFDictionaryCreateMutable.restype  = _CFTypeRef
_CF.CFDictionaryCreateMutable.argtypes = [_CFTypeRef, ctypes.c_long,
                                           ctypes.c_void_p, ctypes.c_void_p]

_CF.CFDictionarySetValue.restype  = None
_CF.CFDictionarySetValue.argtypes = [_CFTypeRef, _CFTypeRef, _CFTypeRef]

_CF.CFArrayCreateMutable.restype  = _CFTypeRef
_CF.CFArrayCreateMutable.argtypes = [_CFTypeRef, ctypes.c_long, ctypes.c_void_p]

_CF.CFArrayAppendValue.restype  = None
_CF.CFArrayAppendValue.argtypes = [_CFTypeRef, _CFTypeRef]

_CF.CFRelease.restype  = None
_CF.CFRelease.argtypes = [_CFTypeRef]

# ── CoreAudio function signatures ─────────────────────────────────────────────

_CA.AudioObjectGetPropertyDataSize.restype  = _OSStatus
_CA.AudioObjectGetPropertyDataSize.argtypes = [
    _AudioObjID, ctypes.POINTER(_PropAddr),
    ctypes.c_uint32, ctypes.c_void_p,
    ctypes.POINTER(ctypes.c_uint32),
]

_CA.AudioObjectGetPropertyData.restype  = _OSStatus
_CA.AudioObjectGetPropertyData.argtypes = [
    _AudioObjID, ctypes.POINTER(_PropAddr),
    ctypes.c_uint32, ctypes.c_void_p,
    ctypes.POINTER(ctypes.c_uint32), ctypes.c_void_p,
]

_CA.AudioObjectSetPropertyData.restype  = _OSStatus
_CA.AudioObjectSetPropertyData.argtypes = [
    _AudioObjID, ctypes.POINTER(_PropAddr),
    ctypes.c_uint32, ctypes.c_void_p,
    ctypes.c_uint32, ctypes.c_void_p,
]

_CA.AudioHardwareCreateAggregateDevice.restype  = _OSStatus
_CA.AudioHardwareCreateAggregateDevice.argtypes = [_CFTypeRef, ctypes.POINTER(_AudioObjID)]

# ── Constants ─────────────────────────────────────────────────────────────────

def _fcc(s: str) -> int:
    return sum(ord(c) << (24 - 8 * i) for i, c in enumerate(s))


kCFStringEncodingUTF8 = 0x08000100
kCFNumberSInt32Type   = 3

kAudioObjectSystemObject        = 1
kAudioObjectPropertyScopeGlobal = _fcc("glob")
kAudioObjectPropertyScopeInput  = _fcc("inpt")
kAudioObjectPropertyScopeOutput = _fcc("outp")
kAudioObjectPropertyElementMain = 0

kAudioHardwarePropertyDevices             = _fcc("dev#")
kAudioHardwarePropertyDefaultOutputDevice = _fcc("dOut")

kAudioDevicePropertyDeviceName        = _fcc("name")
kAudioDevicePropertyDeviceUID         = _fcc("uid ")
kAudioDevicePropertyNominalSampleRate = _fcc("nsrt")
kAudioDevicePropertyTransportType     = _fcc("tran")
kAudioDevicePropertyStreams           = _fcc("stm#")

kAudioDeviceTransportTypeBuiltIn  = _fcc("bltn")
kAudioDeviceTransportTypeAggregate = _fcc("agrg")

# ── CF helpers ────────────────────────────────────────────────────────────────

def _cf_str(s: str) -> int:
    ref = _CF.CFStringCreateWithCString(None, s.encode("utf-8"), kCFStringEncodingUTF8)
    if not ref:
        raise RuntimeError(f"CFStringCreateWithCString failed for {s!r}")
    return ref


def _cf_num(n: int) -> int:
    v = ctypes.c_int32(n)
    ref = _CF.CFNumberCreate(None, kCFNumberSInt32Type, ctypes.byref(v))
    if not ref:
        raise RuntimeError("CFNumberCreate failed")
    return ref


def _cf_str_to_py(ref: int) -> str:
    length = _CF.CFStringGetLength(ref)
    buf = ctypes.create_string_buffer((length + 1) * 4)
    if _CF.CFStringGetCString(ref, buf, len(buf), kCFStringEncodingUTF8):
        return buf.value.decode("utf-8")
    return ""


def _cf_dict_new() -> int:
    ref = _CF.CFDictionaryCreateMutable(
        None, 0,
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


def _dict_set_int(d: int, key: str, val: int) -> None:
    k = _cf_str(key)
    v = _cf_num(val)
    _CF.CFDictionarySetValue(d, k, v)
    _CF.CFRelease(k)
    _CF.CFRelease(v)


def _dict_set_ref(d: int, key: str, val: int) -> None:
    k = _cf_str(key)
    _CF.CFDictionarySetValue(d, k, val)
    _CF.CFRelease(k)

# ── CoreAudio helpers ─────────────────────────────────────────────────────────

def _prop(selector: int,
          scope: int = kAudioObjectPropertyScopeGlobal,
          element: int = kAudioObjectPropertyElementMain) -> _PropAddr:
    return _PropAddr(selector, scope, element)


def _get_devices() -> list[int]:
    addr = _prop(kAudioHardwarePropertyDevices)
    size = ctypes.c_uint32(0)
    _CA.AudioObjectGetPropertyDataSize(
        kAudioObjectSystemObject, ctypes.byref(addr), 0, None, ctypes.byref(size))
    count = size.value // ctypes.sizeof(_AudioObjID)
    buf = (_AudioObjID * count)()
    _CA.AudioObjectGetPropertyData(
        kAudioObjectSystemObject, ctypes.byref(addr), 0, None, ctypes.byref(size), buf)
    return list(buf)


def _get_str_prop(device_id: int, selector: int) -> str:
    addr = _prop(selector)
    ref = ctypes.c_void_p(0)
    size = ctypes.c_uint32(ctypes.sizeof(ref))
    status = _CA.AudioObjectGetPropertyData(
        device_id, ctypes.byref(addr), 0, None, ctypes.byref(size), ctypes.byref(ref))
    if status != 0 or not ref.value:
        return ""
    result = _cf_str_to_py(ref.value)
    _CF.CFRelease(ref.value)
    return result


def _get_uint_prop(device_id: int, selector: int, scope: int = kAudioObjectPropertyScopeGlobal) -> int:
    addr = _prop(selector, scope)
    val = ctypes.c_uint32(0)
    size = ctypes.c_uint32(ctypes.sizeof(val))
    _CA.AudioObjectGetPropertyData(
        device_id, ctypes.byref(addr), 0, None, ctypes.byref(size), ctypes.byref(val))
    return val.value


def _has_streams(device_id: int, scope: int) -> bool:
    addr = _prop(kAudioDevicePropertyStreams, scope)
    size = ctypes.c_uint32(0)
    status = _CA.AudioObjectGetPropertyDataSize(
        device_id, ctypes.byref(addr), 0, None, ctypes.byref(size))
    return status == 0 and size.value > 0


def _device_names() -> dict[str, int]:
    return {_get_str_prop(d, kAudioDevicePropertyDeviceName): d for d in _get_devices()}


def _find_by_name(substring: str) -> tuple[int, str] | None:
    for dev_id in _get_devices():
        name = _get_str_prop(dev_id, kAudioDevicePropertyDeviceName)
        if substring.lower() in name.lower():
            uid = _get_str_prop(dev_id, kAudioDevicePropertyDeviceUID)
            return dev_id, uid
    return None


def _find_builtin_output() -> tuple[int, str] | None:
    for dev_id in _get_devices():
        if _get_uint_prop(dev_id, kAudioDevicePropertyTransportType) != kAudioDeviceTransportTypeBuiltIn:
            continue
        if not _has_streams(dev_id, kAudioObjectPropertyScopeOutput):
            continue
        uid = _get_str_prop(dev_id, kAudioDevicePropertyDeviceUID)
        return dev_id, uid
    return None


def _create_aggregate(
    name: str,
    uid: str,
    sub_uids: list[str],
    master_uid: str,
    stacked: bool,
) -> int:
    sub_arr = _cf_array_new()
    for sub_uid in sub_uids:
        sub_dict = _cf_dict_new()
        _dict_set_str(sub_dict, "uid", sub_uid)
        if stacked:
            _dict_set_int(sub_dict, "drift", 1)
        _CF.CFArrayAppendValue(sub_arr, sub_dict)
        _CF.CFRelease(sub_dict)

    desc = _cf_dict_new()
    _dict_set_str(desc, "uid",        uid)
    _dict_set_str(desc, "name",       name)
    _dict_set_ref(desc, "subdevices", sub_arr)
    _dict_set_str(desc, "master",     master_uid)
    _dict_set_int(desc, "private",    0)
    _dict_set_int(desc, "stacked",    1 if stacked else 0)
    _CF.CFRelease(sub_arr)

    out_id = _AudioObjID(0)
    status = _CA.AudioHardwareCreateAggregateDevice(desc, ctypes.byref(out_id))
    _CF.CFRelease(desc)

    if status != 0:
        raise RuntimeError(f"AudioHardwareCreateAggregateDevice failed: OSStatus {status:#010x}")
    return out_id.value


def _set_sample_rate(device_id: int, rate: float) -> bool:
    addr = _prop(kAudioDevicePropertyNominalSampleRate)
    val = ctypes.c_double(rate)
    status = _CA.AudioObjectSetPropertyData(
        device_id, ctypes.byref(addr), 0, None, ctypes.sizeof(val), ctypes.byref(val))
    return status == 0


def _set_default_output(device_id: int) -> None:
    addr = _prop(kAudioHardwarePropertyDefaultOutputDevice)
    val = _AudioObjID(device_id)
    status = _CA.AudioObjectSetPropertyData(
        kAudioObjectSystemObject, ctypes.byref(addr), 0, None,
        ctypes.sizeof(val), ctypes.byref(val))
    if status != 0:
        raise RuntimeError(f"Failed to set default output: OSStatus {status:#010x}")

# ── User config ───────────────────────────────────────────────────────────────

_CONFIG_PATH = pathlib.Path("~/.resilient-finch/config.json").expanduser()

def _load_user_config() -> dict:
    if _CONFIG_PATH.exists():
        try:
            return json.loads(_CONFIG_PATH.read_text())
        except (json.JSONDecodeError, OSError):
            pass
    return {}


def _save_user_config(cfg: dict) -> None:
    _CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    _CONFIG_PATH.write_text(json.dumps(cfg, indent=2) + "\n")

# ── Setup ─────────────────────────────────────────────────────────────────────

_MULTIOUT_NAME  = "Resilient Finch Output"
_CAPTURE_NAME   = "Resilient Finch Capture"
_MULTIOUT_UID   = "com.resilient-finch.multi-output"
_CAPTURE_UID    = "com.resilient-finch.capture"


def setup() -> None:
    # 1. Find BlackHole 2ch
    bh = _find_by_name("BlackHole 2ch")
    if bh is None:
        sys.exit("Error: BlackHole 2ch not found. Install with:\n  brew install blackhole-2ch")
    bh_id, bh_uid = bh
    bh_name = _get_str_prop(bh_id, kAudioDevicePropertyDeviceName)
    print(f"  Found  {bh_name!r} (uid={bh_uid!r})")

    # 2. Find built-in speaker
    spk = _find_builtin_output()
    if spk is None:
        sys.exit("Error: Could not find a built-in output device.")
    spk_id, spk_uid = spk
    spk_name = _get_str_prop(spk_id, kAudioDevicePropertyDeviceName)
    print(f"  Found  {spk_name!r} (uid={spk_uid!r})")

    existing = _device_names()

    # 3. Create Multi-Output Device
    if _MULTIOUT_NAME in existing:
        multiout_id = existing[_MULTIOUT_NAME]
        print(f"  Exists {_MULTIOUT_NAME!r} — skipping")
    else:
        print(f"  Create {_MULTIOUT_NAME!r} (speakers + BlackHole) ...")
        multiout_id = _create_aggregate(
            name=_MULTIOUT_NAME,
            uid=_MULTIOUT_UID,
            sub_uids=[spk_uid, bh_uid],
            master_uid=spk_uid,
            stacked=True,
        )
        print(f"         done (id={multiout_id})")

    # 4. Create Aggregate Capture Device
    if _CAPTURE_NAME in existing:
        capture_id = existing[_CAPTURE_NAME]
        print(f"  Exists {_CAPTURE_NAME!r} — skipping")
    else:
        print(f"  Create {_CAPTURE_NAME!r} (BlackHole only) ...")
        capture_id = _create_aggregate(
            name=_CAPTURE_NAME,
            uid=_CAPTURE_UID,
            sub_uids=[bh_uid],
            master_uid=bh_uid,
            stacked=False,
        )
        print(f"         done (id={capture_id})")

    # 5. Set 16 kHz on capture device
    print(f"  Set    {_CAPTURE_NAME!r} sample rate → 16000 Hz ...")
    if not _set_sample_rate(capture_id, 16000.0):
        print("         Warning: could not set sample rate — set it manually to 16000 Hz in Audio MIDI Setup")
    else:
        print("         done")

    # 6. Set system default output
    print(f"  Set    system output → {_MULTIOUT_NAME!r} ...")
    _set_default_output(multiout_id)
    print("         done")

    # 7. Write capture device name to user config
    cfg = _load_user_config()
    cfg["blackhole_device_name"] = _CAPTURE_NAME
    _save_user_config(cfg)
    print(f"  Wrote  ~/.resilient-finch/config.json")

    print()
    print("Setup complete. Run the app with:  uv run python main.py")
    print()
    print("To revert: System Settings → Sound → Output → select your speakers,")
    print("then delete the created devices in Audio MIDI Setup.")


if __name__ == "__main__":
    print("resilient-finch audio setup")
    print("─" * 40)
    setup()
