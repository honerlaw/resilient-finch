from __future__ import annotations

import logging
import queue
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime

import numpy as np
import sounddevice as sd
from numpy.typing import NDArray

from . import config

logger = logging.getLogger(__name__)

type _CallbackFn = Callable[[NDArray[np.float32], int, object, object], None]


@dataclass
class AudioChunk:
    data: NDArray[np.float32]
    timestamp: datetime
    source: str  # "MIC" or "SPEAKER"


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

    def start(self) -> None:
        mic_idx = (
            self._find_device_index(config.MIC_DEVICE_NAME)
            if config.MIC_DEVICE_NAME
            else None
        )
        speaker_idx = self._find_device_index(config.BLACKHOLE_DEVICE_NAME)

        self._mic_stream = sd.InputStream(
            device=mic_idx,
            channels=1,
            samplerate=config.SAMPLE_RATE,
            blocksize=config.BLOCK_SIZE_FRAMES,
            dtype="float32",
            callback=self._make_callback(self._mic_queue, "MIC"),
        )
        self._speaker_stream = sd.InputStream(
            device=speaker_idx,
            channels=None,  # accept whatever channel count BlackHole reports
            samplerate=config.SAMPLE_RATE,
            blocksize=config.BLOCK_SIZE_FRAMES,
            dtype="float32",
            callback=self._make_callback(self._speaker_queue, "SPEAKER"),
        )
        self._mic_stream.start()
        self._speaker_stream.start()
        logger.info(
            "Audio capture started (mic device=%s, blackhole idx=%d)", mic_idx, speaker_idx
        )

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
        device_list = "\n".join(available)
        msg = f"Input device '{name}' not found.\nAvailable input devices:\n{device_list}"
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
            mono: NDArray[np.float32] = (
                indata.mean(axis=1) if indata.shape[1] > 1 else indata[:, 0]
            )
            chunk = AudioChunk(data=mono.copy(), timestamp=datetime.now(tz=UTC), source=source)
            try:
                q.put_nowait(chunk)
            except queue.Full:
                logger.warning("[%s] audio queue full — dropping chunk", source)

        return callback
