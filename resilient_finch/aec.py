from __future__ import annotations

import logging
import queue
import threading
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import numpy as np

from . import config
from .capture import AudioChunk

if TYPE_CHECKING:
    from numpy.typing import NDArray

logger = logging.getLogger(__name__)

_FRAME_DUR = timedelta(seconds=config.AEC_FRAME_SAMPLES / config.SAMPLE_RATE)


class _BlockNLMS:
    """Block-mode Normalized LMS adaptive filter for echo cancellation.

    Processes audio in fixed-size frames. The speaker stream is used as the
    adaptive filter reference; the output is the mic stream with the estimated
    echo subtracted.
    """

    def __init__(self, num_taps: int, step_size: float) -> None:
        self._w = np.zeros(num_taps, dtype=np.float64)
        self._n = num_taps
        self._mu = step_size
        self._history = np.zeros(num_taps - 1, dtype=np.float64)

    def process(
        self,
        mic: NDArray[np.float32],
        spk: NDArray[np.float32],
    ) -> NDArray[np.float32]:
        x = np.concatenate([self._history, spk.astype(np.float64)])
        # inp[i] = x[i : i + num_taps] — filter input vector at each sample
        inp = np.lib.stride_tricks.sliding_window_view(x, self._n)

        echo = inp @ self._w
        error = mic.astype(np.float64) - echo

        # Block NLMS weight update
        power = float(np.mean(np.einsum("ij,ij->i", inp, inp))) + 1e-8
        self._w += (self._mu / power) * (inp.T @ error) / len(mic)

        self._history = x[len(mic) :]
        return np.clip(error, -1.0, 1.0).astype(np.float32)


class AcousticEchoCanceller:
    """Removes speaker bleed-through from the mic stream.

    Sits between AudioCapturer and the two Transcribers. The speaker stream is
    forwarded unchanged while also serving as the AEC reference. The mic stream
    is echo-cancelled before being forwarded.
    """

    def __init__(
        self,
        raw_mic_queue: queue.Queue[AudioChunk],
        raw_spk_queue: queue.Queue[AudioChunk],
        proc_mic_queue: queue.Queue[AudioChunk],
        proc_spk_queue: queue.Queue[AudioChunk],
    ) -> None:
        self._raw_mic = raw_mic_queue
        self._raw_spk = raw_spk_queue
        self._proc_mic = proc_mic_queue
        self._proc_spk = proc_spk_queue

        self._filter = _BlockNLMS(config.AEC_NUM_TAPS, config.AEC_STEP_SIZE)
        self._mic_buf: NDArray[np.float32] = np.empty(0, dtype=np.float32)
        self._spk_buf: NDArray[np.float32] = np.empty(0, dtype=np.float32)
        self._mic_ts = None

        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._loop, name="aec", daemon=True)
        self._thread.start()
        logger.info("[AEC] started (taps=%d, mu=%.3f)", config.AEC_NUM_TAPS, config.AEC_STEP_SIZE)

    def flush_and_stop(self, timeout: float = 10.0) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=timeout)
        logger.info("[AEC] stopped")

    def _drain_spk(self) -> None:
        """Pull all immediately available speaker chunks, forward and buffer them."""
        while True:
            try:
                chunk = self._raw_spk.get_nowait()
            except queue.Empty:
                break
            self._spk_buf = np.concatenate([self._spk_buf, chunk.data])
            try:
                self._proc_spk.put_nowait(chunk)
            except queue.Full:
                logger.warning("[AEC] speaker queue full — dropping chunk")

    def _process_frames(self) -> None:
        silence = np.zeros(config.AEC_FRAME_SAMPLES, dtype=np.float32)
        while len(self._mic_buf) >= config.AEC_FRAME_SAMPLES:
            mic_frame = self._mic_buf[: config.AEC_FRAME_SAMPLES]
            self._mic_buf = self._mic_buf[config.AEC_FRAME_SAMPLES :]

            if len(self._spk_buf) >= config.AEC_FRAME_SAMPLES:
                spk_frame = self._spk_buf[: config.AEC_FRAME_SAMPLES]
                self._spk_buf = self._spk_buf[config.AEC_FRAME_SAMPLES :]
            else:
                spk_frame = silence

            processed = self._filter.process(mic_frame, spk_frame)
            ts: datetime = self._mic_ts if self._mic_ts is not None else datetime.now(tz=UTC)
            self._mic_ts = ts + _FRAME_DUR

            out = AudioChunk(data=processed, timestamp=ts, source="MIC")
            try:
                self._proc_mic.put_nowait(out)
            except queue.Full:
                logger.warning("[AEC] mic queue full — dropping frame")

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                chunk = self._raw_mic.get(timeout=0.05)
            except queue.Empty:
                continue

            if self._mic_ts is None:
                self._mic_ts = chunk.timestamp
            self._mic_buf = np.concatenate([self._mic_buf, chunk.data])

            while True:
                try:
                    extra = self._raw_mic.get_nowait()
                    self._mic_buf = np.concatenate([self._mic_buf, extra.data])
                except queue.Empty:
                    break

            self._drain_spk()
            self._process_frames()

        # Flush remaining data on shutdown
        while True:
            try:
                chunk = self._raw_mic.get_nowait()
                if self._mic_ts is None:
                    self._mic_ts = chunk.timestamp
                self._mic_buf = np.concatenate([self._mic_buf, chunk.data])
            except queue.Empty:
                break
        self._drain_spk()
        self._process_frames()
