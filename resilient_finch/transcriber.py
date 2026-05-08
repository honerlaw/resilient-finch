from __future__ import annotations

import logging
import queue
import threading
from collections import deque
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

import numpy as np
from numpy.typing import NDArray

from . import config

if TYPE_CHECKING:
    from faster_whisper import WhisperModel

    from .capture import AudioChunk
    from .session import Session

logger = logging.getLogger(__name__)

type _TranscribeJob = tuple[NDArray[np.float32], datetime]


class Transcriber:
    def __init__(
        self,
        source_label: str,
        audio_queue: queue.Queue[AudioChunk],
        session: Session,
        model: WhisperModel,
    ) -> None:
        self._source = source_label
        self._audio_queue = audio_queue
        self._session = session
        self._model = model

        self._stop_event = threading.Event()
        self._transcription_queue: queue.Queue[_TranscribeJob | None] = queue.Queue(
            maxsize=config.TRANSCRIPTION_QUEUE_MAXSIZE
        )
        self._buffer: deque[AudioChunk] = deque()
        self._buffer_samples: int = 0
        self._buffer_start_time: datetime | None = None

        self._buffer_thread: threading.Thread | None = None
        self._transcription_thread: threading.Thread | None = None

    def start(self) -> None:
        self._buffer_thread = threading.Thread(
            target=self._buffer_loop, name=f"{self._source}-buffer", daemon=True
        )
        self._transcription_thread = threading.Thread(
            target=self._transcription_loop, name=f"{self._source}-transcribe", daemon=True
        )
        self._buffer_thread.start()
        self._transcription_thread.start()
        logger.info("[%s] Transcriber started", self._source)

    def flush_and_stop(self, timeout: float = 60.0) -> None:
        self._stop_event.set()
        if self._buffer_thread:
            self._buffer_thread.join(timeout=timeout)
        if self._transcription_thread:
            self._transcription_thread.join(timeout=timeout)
        logger.info("[%s] Transcriber stopped", self._source)

    def _buffer_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                chunk = self._audio_queue.get(timeout=0.1)
            except queue.Empty:
                continue

            if self._buffer_start_time is None:
                self._buffer_start_time = chunk.timestamp

            self._buffer.append(chunk)
            self._buffer_samples += len(chunk.data)

            if self._should_flush():
                self._flush_buffer()

        # drain remaining audio on shutdown
        while True:
            try:
                chunk = self._audio_queue.get_nowait()
            except queue.Empty:
                break
            if self._buffer_start_time is None:
                self._buffer_start_time = chunk.timestamp
            self._buffer.append(chunk)
            self._buffer_samples += len(chunk.data)

        if self._buffer_samples > 0:
            self._flush_buffer()

        self._transcription_queue.put(None)  # sentinel

    def _should_flush(self) -> bool:
        seconds = self._buffer_samples / config.SAMPLE_RATE
        if seconds >= config.MAX_BUFFER_SECONDS or seconds >= config.SEGMENT_SECONDS:
            return True
        if seconds < config.SILENCE_FLUSH_SECONDS:
            return False
        tail_samples = int(config.SILENCE_FLUSH_SECONDS * config.SAMPLE_RATE)
        pieces: list[NDArray[np.float32]] = []
        collected = 0
        for chunk in reversed(self._buffer):
            pieces.append(chunk.data)
            collected += len(chunk.data)
            if collected >= tail_samples:
                break
        if not pieces:
            return False
        tail_audio: NDArray[np.float32] = np.concatenate(pieces)[-tail_samples:]
        rms = float(np.sqrt(np.mean(np.square(tail_audio))))
        return rms < config.SILENCE_RMS_THRESHOLD

    def _flush_buffer(self) -> None:
        if not self._buffer or self._buffer_start_time is None:
            return
        audio: NDArray[np.float32] = np.concatenate([c.data for c in self._buffer])
        seg_time = self._buffer_start_time
        self._buffer.clear()
        self._buffer_samples = 0
        self._buffer_start_time = None
        try:
            self._transcription_queue.put((audio, seg_time), timeout=5.0)
        except queue.Full:
            logger.warning("[%s] transcription queue full — dropping segment", self._source)

    def _transcription_loop(self) -> None:
        while True:
            try:
                item = self._transcription_queue.get(timeout=0.5)
            except queue.Empty:
                continue
            if item is None:
                break
            audio, seg_time = item
            self._run_transcription(audio, seg_time)

    def _run_transcription(self, audio: NDArray[np.float32], seg_time: datetime) -> None:
        if len(audio) / config.SAMPLE_RATE < config.MIN_AUDIO_DURATION_SECONDS:
            return

        logger.debug(
            "[%s] Transcribing %.1fs of audio",
            self._source,
            len(audio) / config.SAMPLE_RATE,
        )

        try:
            segments, _ = self._model.transcribe(
                audio,
                language=config.WHISPER_LANGUAGE,
                beam_size=config.WHISPER_BEAM_SIZE,
                temperature=config.WHISPER_TEMPERATURE,
                vad_filter=config.WHISPER_VAD_FILTER,
                vad_parameters={
                    "min_speech_duration_ms": config.WHISPER_VAD_MIN_SPEECH_DURATION_MS,
                    "max_speech_duration_s": config.WHISPER_VAD_MAX_SPEECH_DURATION_S,
                },
                condition_on_previous_text=config.WHISPER_CONDITION_ON_PREVIOUS_TEXT,
                word_timestamps=config.WHISPER_WORD_TIMESTAMPS,
            )
            for segment in segments:
                text = segment.text.strip()
                if not text:
                    continue
                abs_time = seg_time + timedelta(seconds=segment.start)
                self._session.add_entry(abs_time, self._source, text)
                logger.info("[%s] %s", self._source, text)
        except (RuntimeError, ValueError, OSError):
            logger.exception("[%s] Transcription error", self._source)
