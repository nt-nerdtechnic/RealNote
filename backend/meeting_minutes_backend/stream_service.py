from __future__ import annotations

import asyncio
import json
import os
import queue
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable

import numpy as np
import soundfile as sf
import torch

from .stream import AudioChunk, _offset_asr_result
from .transcript import build_transcript, transcript_to_text


MAX_BACKLOG = 3  # queue 積壓超過此數量時，丟棄舊 chunk 以防延遲堆積


def _merge_short_segments(
    segments: list[dict[str, Any]],
    max_gap: float = 0.3,
    min_duration: float = 1.0,
) -> list[dict[str, Any]]:
    """將碎片 segment 合併：若相鄰兩段的 gap ≤ max_gap，且任一段 < min_duration，則合併。"""
    if not segments:
        return segments
    merged: list[dict[str, Any]] = [dict(segments[0])]
    for seg in segments[1:]:
        prev = merged[-1]
        gap = seg.get("start", 0.0) - prev.get("end", 0.0)
        prev_dur = prev.get("end", 0.0) - prev.get("start", 0.0)
        curr_dur = seg.get("end", 0.0) - seg.get("start", 0.0)
        if gap <= max_gap and (prev_dur < min_duration or curr_dur < min_duration):
            merged[-1] = {
                **prev,
                "end": seg.get("end"),
                "text": prev.get("text", "") + seg.get("text", ""),
            }
        else:
            merged.append(dict(seg))
    return merged



def _enforce_chronological(
    segments: list[dict[str, Any]],
    prev_end: float = 0.0,
) -> list[dict[str, Any]]:
    """確保 segments 無時間重疊，同時保留所有 segment 的文字內容。

    若某段的 start < 前一段的 end（重疊），將整段平移到 prev_end 之後，
    保留原始 duration（不丟棄文字）。例如：
        前段 [70.5-71.7]，本段 [71.2-71.7] duration=0.5
        → 平移為 [71.7-72.2]，文字完整保留。

    不重新排序 — segments 應已按時間順序輸入。
    """
    result: list[dict[str, Any]] = []
    cur_end = prev_end
    for seg in segments:
        start = seg.get("start")
        end = seg.get("end")
        if start is None or end is None:
            result.append(seg)
            continue
        if start < cur_end:
            # 平移：保留原始 duration，start 推到 cur_end 之後
            duration = max(end - start, 0.1)  # 至少 0.1s，避免零時長
            start = cur_end
            end = start + duration
        result.append(seg if (start == seg.get("start") and end == seg.get("end")) else {**seg, "start": start, "end": end})
        cur_end = end
    return result


SAMPLE_RATE = 16_000                               # 固定，mlx_whisper 要求 16kHz
MIN_TAIL_FRAMES = int(0.3 * SAMPLE_RATE)           # 固定，chunk 尾段保留長度
# 純靜音軌安全閥：從未偵測到語音、VAD 也未觸發切句時，buffer 會無限成長。
# 分軌時「對方」軌可能整段數位靜音，故設上限，超過且確認為靜音就丟棄（無內容損失）。
MAX_SILENT_SAMPLES = 30 * SAMPLE_RATE              # 30 秒

# 以下所有「常數」皆已移到 settings.json，每次錄音由 start() 動態載入：
#   self._overlap_samples           ← chunk.overlap_seconds
#   self._preview_window_frames     ← preview.window_seconds
#   self._preview_interval_frames   ← preview.interval_seconds
#   self._min_speech_frames         ← vad.min_speech_seconds
#   self._vad_speech_threshold      ← vad.speech_threshold
#   self._vad_silence_threshold     ← vad.silence_threshold
#   self._vad_peak_ratio            ← vad.peak_ratio
#   self._force_flush_seconds       ← vad.force_flush_seconds
#   self._leading_silence_trim_seconds   ← vad.leading_silence_trim_seconds
#   self._min_speech_in_chunk_seconds    ← vad.min_speech_in_chunk_seconds
#   self._preview_min_audio_samples ← preview.min_audio_seconds
#   self._preview_silence_clear_samples  ← preview.silence_clear_seconds

# Silero VAD 串流分段（固定）
VAD_FRAME_SIZE = 512                               # 固定，Silero VAD 每次處理 512 samples（32ms@16kHz）
VAD_SILENCE_FRAMES_DEFAULT = 9                     # __init__ 用的預設值；start() 會覆蓋為 settings 計算結果

EmitFn = Callable[[dict[str, Any]], Awaitable[None]]


@dataclass
class StreamState:
    status: str = "idle"
    output_dir: str | None = None
    started_at: float | None = None
    segment_count: int = 0
    backlog: int = 0
    error: str | None = None


@dataclass
class _TrackBuf:
    """每軌的切句狀態（push_pcm 側，只在 asyncio event loop 存取，無需 lock）。

    混音模式只用一個 track（index=0, speaker=None），行為與原本單軌完全相同；
    分軌模式有兩個 track（0=你, 1=對方），各自獨立 VAD / buffer / overlap / 時間軸。
    """
    index: int = 0
    speaker: str | None = None
    pcm_buffer: np.ndarray = field(default_factory=lambda: np.empty((0, 1), dtype=np.float32))
    overlap_buffer: np.ndarray = field(default_factory=lambda: np.empty((0, 1), dtype=np.float32))
    vad_iterator: Any = None
    vad_frame_buf: np.ndarray = field(default_factory=lambda: np.empty(0, dtype=np.float32))
    buffer_start_time: float | None = None
    chunk_start: float = 0.0


@dataclass
class _TrackCtx:
    """每軌的跨-chunk ASR 上下文（_asr_worker 側）。

    overlap 去重、時間戳順序、rolling context、語言 sticky、語速閾值都假設「單一時間軸」，
    所以分軌時必須每軌一份，避免兩個講者互相污染。混音模式只用 track 0。
    """
    segments: list[dict[str, Any]] = field(default_factory=list)
    rolling_ctx_by_lang: dict[str, str] = field(default_factory=dict)
    rolling_context: str = ""
    current_lang: str | None = None
    pending_lang: str | None = None
    pending_count: int = 0
    accepted_rates: list[float] = field(default_factory=list)


import threading as _threading

_PRELOADED_ASR: Any = None
_preload_ready = _threading.Event()  # set 後表示 warmup 完成，可以安全使用


def _preload_asr_bg() -> None:
    """在背景 thread 建立 FasterWhisperAsr 並明確呼叫 ensure_warm()（同步暖機），
    完成後設定 _preload_ready event。
    _asr_worker 不等待此 event——只是「如果恰好已好就用」的優化路徑。"""
    global _PRELOADED_ASR
    try:
        from .faster_asr import FasterWhisperAsr
        asr = FasterWhisperAsr()   # __init__ 快速，不暖機
        asr.ensure_warm()          # 同步暖機，約 8-12s
        _PRELOADED_ASR = asr
    except Exception:
        pass
    finally:
        _preload_ready.set()


# backend 啟動時立刻在背景開始 warmup（不阻塞 HTTP server）
_threading.Thread(target=_preload_asr_bg, daemon=True).start()


# ---------------------------------------------------------------------------
# Silero VAD 預載（與 ASR warmup 並行）
# ---------------------------------------------------------------------------
_SILERO_VAD_MODEL: Any = None


def _preload_silero_bg() -> None:
    global _SILERO_VAD_MODEL
    try:
        from silero_vad import load_silero_vad
        _SILERO_VAD_MODEL = load_silero_vad()
    except Exception:
        pass  # 不影響啟動；push_pcm 會 fallback 到 RMS VAD


_threading.Thread(target=_preload_silero_bg, daemon=True).start()


class StreamService:
    def __init__(self, emit: EmitFn) -> None:
        self.emit = emit
        self.state = StreamState()
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._loop: asyncio.AbstractEventLoop | None = None
        # 音軌：start() 重建。混音=1 軌（speaker=None）；分軌=2 軌（你/對方）。
        # 每軌的 PCM buffer / overlap / VAD 狀態皆在 _TrackBuf 內，只在 asyncio loop 存取，無需 lock
        self._tracks: list[_TrackBuf] = []
        self._dual_track: bool = False
        self._chunk_queue: queue.Queue[AudioChunk] = queue.Queue()
        self._chunk_index: int = 1
        self._vad_silence_frames: int = VAD_SILENCE_FRAMES_DEFAULT  # overridden on start()
        # 1-second rolling preview
        self._preview_buffer: np.ndarray = np.empty((0, 1), dtype=np.float32)
        self._frames_since_preview: int = 0
        self._preview_queue: queue.Queue[np.ndarray] = queue.Queue(maxsize=1)
        self._preview_thread: threading.Thread | None = None
        # display log 路徑（開始收音後設定）
        self._display_log_path: Path | None = None
        # 預覽靜音偵測：累積靜音樣本數，超過 1s 後送清除信號
        self._preview_silence_samples: int = 0
        # 區分「使用者手動停止」(True) 與「WS 自然斷線結束」(False)
        # 手動停止時跳過耗時的 Breeze 校正 + LLM 摘要，避免 thread 長時間卡住
        self._explicit_stop: bool = False
        # Segment 穩定 id：每個 emit 出去的 segment 分配 monotonic line_id
        # 用途：前端以 line_id 為 key，Breeze 校正以 line_id 回綁（不再用陣列 index）
        self._next_line_id: int = 1
        self._correction_worker: Any = None
        # LLM 校正落地追蹤（供寫 display_log / llm_corrected_transcript 用）
        self._llm_corrections: dict[int, str] = {}        # first_line_id → corrected text
        self._llm_merge_groups: dict[int, list[int]] = {} # first_line_id → all line_ids
        self._llm_merged_secondary: set[int] = set()      # 被合併掉的非主 line_id
        self._line_id_to_segment: dict[int, dict[str, Any]] = {}
        # 執行期設定（每次 start() 重新載入；__init__ 用 defaults 預先填入避免 None 錯誤）
        from . import settings as _settings_mod
        self._settings: dict[str, Any] = _settings_mod.defaults()

    def snapshot(self) -> dict[str, Any]:
        # 校正 worker 狀態（為 None 則回傳 disabled）
        cw = self._correction_worker
        if cw is not None:
            correction_status = {
                "enabled": True,
                "queue_size": cw._queue.qsize() if hasattr(cw, "_queue") else 0,
                "buffered": len(cw._buffer) if hasattr(cw, "_buffer") else 0,
                "corrected_count": len(cw._corrected_lines) if hasattr(cw, "_corrected_lines") else 0,
            }
        else:
            correction_status = {"enabled": False, "queue_size": 0, "buffered": 0, "corrected_count": 0}
        return {
            "status": self.state.status,
            "output_dir": self.state.output_dir,
            "started_at": self.state.started_at,
            "segment_count": self.state.segment_count,
            "correction": correction_status,
            "backlog": self.state.backlog,
            "error": self.state.error,
        }

    async def start(
        self,
        output_dir: str | None,
        segment_seconds: float,
        language: str | None = None,
        dual_track: bool = False,
    ) -> dict[str, Any]:
        if self.state.status in {"recording", "stopping", "transcribing", "correcting", "summarizing"}:
            raise RuntimeError("stream is already running")
        if segment_seconds <= 0:
            raise RuntimeError("segment_seconds must be positive")

        path = self._make_output_dir(output_dir)
        # 載入最新設定，本次錄音生效；錄音中變更不影響當下，要等下次 start
        from . import settings as _settings_mod
        self._settings = _settings_mod.load()
        _S = self._settings
        # 把與 push_pcm / VAD / 預覽 相關的「次數/樣本數」算好存到 instance attr
        self._overlap_samples = int(_S["chunk.overlap_seconds"] * SAMPLE_RATE)
        self._preview_window_frames = int(_S["preview.window_seconds"] * SAMPLE_RATE)
        self._preview_interval_frames = int(_S["preview.interval_seconds"] * SAMPLE_RATE)
        self._min_speech_frames = int(_S["vad.min_speech_seconds"] * SAMPLE_RATE)
        self._vad_silence_threshold = _S["vad.silence_threshold"]
        self._vad_speech_threshold = _S["vad.speech_threshold"]
        self._vad_peak_ratio = _S["vad.peak_ratio"]
        self._force_flush_seconds = _S["vad.force_flush_seconds"]
        self._leading_silence_trim_seconds = _S["vad.leading_silence_trim_seconds"]
        self._min_speech_in_chunk_seconds = _S["vad.min_speech_in_chunk_seconds"]
        self._preview_min_audio_samples = int(_S["preview.min_audio_seconds"] * SAMPLE_RATE)
        self._preview_silence_clear_samples = int(_S["preview.silence_clear_seconds"] * SAMPLE_RATE)
        self._stop_event = threading.Event()
        self._loop = asyncio.get_running_loop()
        self._dual_track = dual_track
        self._chunk_queue = queue.Queue()
        self._chunk_index = 1
        # 呼吸停頓閾值：由前端 segment_seconds 控制（e.g. 0.5s → ~15 幀）
        self._vad_silence_frames = max(3, int(segment_seconds * SAMPLE_RATE / VAD_FRAME_SIZE))
        self._preview_buffer = np.empty((0, 1), dtype=np.float32)
        self._frames_since_preview = 0
        self._preview_queue = queue.Queue(maxsize=1)
        self._preview_silence_samples = 0
        self._explicit_stop = False
        self._next_line_id = 1
        self._llm_corrections = {}
        self._llm_merge_groups = {}
        self._llm_merged_secondary = set()
        self._line_id_to_segment = {}
        # 建立音軌：混音模式 1 軌（speaker=None），分軌模式 2 軌（0=你/mic, 1=對方/system）。
        # 每軌各自一份 Silero VADIterator（stateful，每次錄音重建以重置內部狀態）。
        _silence_ms = max(100, int(self._vad_silence_frames * VAD_FRAME_SIZE * 1000 / SAMPLE_RATE))
        track_specs = [(0, "你"), (1, "對方")] if dual_track else [(0, None)]
        self._tracks = []
        for _idx, _speaker in track_specs:
            tb = _TrackBuf(index=_idx, speaker=_speaker)
            if _SILERO_VAD_MODEL is not None:
                from silero_vad import VADIterator
                tb.vad_iterator = VADIterator(
                    _SILERO_VAD_MODEL,
                    threshold=_S["vad.speech_threshold"],
                    sampling_rate=SAMPLE_RATE,
                    min_silence_duration_ms=_silence_ms,
                )
            self._tracks.append(tb)
        self._display_log_path = path / "display_log.txt"
        # 寫入 header
        self._display_log_path.write_text(
            f"# display log — {time.strftime('%Y-%m-%d %H:%M:%S')}\n"
            "# 格式：[牆鐘時間] [音訊時間] 文字 (LIVE)\n"
            "#        [牆鐘時間] [CORRECTION idx=N] 原始 → 校正後\n\n",
            encoding="utf-8",
        )

        self.state = StreamState(
            status="recording",
            output_dir=str(path),
            started_at=time.time(),
            segment_count=0,
            backlog=0,
            error=None,
        )

        self._thread = threading.Thread(
            target=self._asr_worker,
            args=(path, self._loop, language),
            daemon=True,
        )
        self._preview_thread = threading.Thread(
            target=self._preview_worker,
            args=(self._loop,),
            daemon=True,
        )
        # LLM 即時校正 worker — 永遠啟動（不再需要 correction.enabled 切換）
        # 失敗 fallback：worker=None，主流程繼續，只是沒有 LLM 校正
        self._correction_worker = None
        try:
            from .correction_worker import CorrectionWorker

            def _ts_emit(payload: dict[str, Any]) -> None:
                self._emit_threadsafe(self._loop, payload)

            def _correction_emit(payload: dict[str, Any]) -> None:
                if payload.get("type") == "transcript.correction":
                    corr = payload["correction"]
                    line_ids: list[int] = corr.get("line_ids") or [corr["line_id"]]
                    self._record_llm_correction(line_ids, corr["text"])
                self._emit_threadsafe(self._loop, payload)

            self._correction_worker = CorrectionWorker(
                config=_S,
                emit=_correction_emit,
                log_emit=_ts_emit,
                log_path=path / "correction_log.jsonl",
            )
            self._correction_worker.start()
        except Exception as err:  # noqa: BLE001
            await self.emit({
                "type": "stream.log",
                "message": f"[correction] failed to start worker: {type(err).__name__}: {err}",
            })
            self._correction_worker = None
        await self.emit({"type": "stream.state", "state": self.snapshot()})
        self._thread.start()
        self._preview_thread.start()
        return self.snapshot()

    async def push_pcm(self, data: bytes) -> None:
        """Feed raw Float32-LE PCM frames from the Electron renderer audio capture.

        混音模式：mono（每樣本 1 channel），全部路由到 track 0。
        分軌模式：stereo 交錯（L=你/mic, R=對方/system），de-interleave 後各送一軌。

        Called exclusively from the asyncio event loop (inside the /ws/audio handler),
        so no locking is needed for per-track buffers.
        """
        if self.state.status not in {"recording", "transcribing", "stopping"}:
            return
        if not data:
            return
        _rms_silence = self._settings["audio.rms_silence_threshold"]

        if self._dual_track:
            stereo = np.frombuffer(data, dtype="<f4").reshape(-1, 2)
            track_samples = [stereo[:, 0:1].copy(), stereo[:, 1:2].copy()]
            # 預覽用兩軌平均混音（與單軌時的 mono 行為一致）
            preview_mono = ((stereo[:, 0] + stereo[:, 1]) * 0.5).reshape(-1, 1)
        else:
            mono = np.frombuffer(data, dtype="<f4").reshape(-1, 1)
            track_samples = [mono]
            preview_mono = mono

        for tb, samples in zip(self._tracks, track_samples):
            await self._process_track_pcm(tb, samples, _rms_silence)

        self._update_preview(preview_mono, _rms_silence)

    async def _process_track_pcm(
        self, tb: _TrackBuf, samples: np.ndarray, rms_silence: float
    ) -> None:
        """單一音軌的 VAD 切句 + 兜底 flush。混音模式只有一軌，行為同原本單軌。"""
        tb.pcm_buffer = np.concatenate((tb.pcm_buffer, samples), axis=0)

        incoming_rms = float(np.sqrt(np.mean(samples.flatten() ** 2)))

        # 記錄 buffer 開始累積的時間：只有偵測到語音能量才啟動計時
        # 避免背景噪音觸發兜底 flush → 幻覺輸出
        if tb.buffer_start_time is None and incoming_rms >= rms_silence:
            tb.buffer_start_time = time.time()

        # VAD 句尾偵測：Silero（有模型）或 RMS（fallback）→ 觸發切句
        flushed = False
        if tb.vad_iterator is not None:
            # 將本次新音訊與上次剩餘不足一幀的片段合併，逐幀送 Silero VAD
            combined = np.concatenate([tb.vad_frame_buf, samples.flatten()])
            n_frames = len(combined) // VAD_FRAME_SIZE
            end_frame_idx: int | None = None
            for fi in range(n_frames):
                frame = combined[fi * VAD_FRAME_SIZE : (fi + 1) * VAD_FRAME_SIZE]
                speech_dict = tb.vad_iterator(
                    torch.from_numpy(frame).float(), return_seconds=False
                )
                if (
                    speech_dict is not None
                    and "end" in speech_dict
                    and len(tb.pcm_buffer) >= self._min_speech_frames
                ):
                    end_frame_idx = fi
                    break

            if end_frame_idx is not None:
                chunk_audio = tb.pcm_buffer
                tb.pcm_buffer = np.empty((0, 1), dtype=np.float32)
                tb.buffer_start_time = None
                await self._flush_chunk(tb, chunk_audio)
                flushed = True
                tb.vad_iterator.reset_states()
                # 剩餘未處理的幀留給下一次呼叫
                tb.vad_frame_buf = combined[(end_frame_idx + 1) * VAD_FRAME_SIZE :]
            else:
                # 沒有觸發：把不足一幀的尾端存回，等下次補齊
                tb.vad_frame_buf = combined[n_frames * VAD_FRAME_SIZE :]
        else:
            # Fallback：原始 RMS VAD
            vad_min = self._min_speech_frames + self._vad_silence_frames * VAD_FRAME_SIZE
            if len(tb.pcm_buffer) >= vad_min:
                if self._silero_end_of_speech(tb.pcm_buffer.flatten()):
                    chunk_audio = tb.pcm_buffer
                    tb.pcm_buffer = np.empty((0, 1), dtype=np.float32)
                    tb.buffer_start_time = None
                    await self._flush_chunk(tb, chunk_audio)
                    flushed = True

        # 時間兜底：獨立判斷，不受 VAD 是否觸發影響
        if not flushed and (
            tb.buffer_start_time is not None
            and len(tb.pcm_buffer) >= self._min_speech_frames
            and time.time() - tb.buffer_start_time >= self._force_flush_seconds
        ):
            chunk_audio = tb.pcm_buffer
            tb.pcm_buffer = np.empty((0, 1), dtype=np.float32)
            tb.buffer_start_time = None
            await self._flush_chunk(tb, chunk_audio)

        # 安全閥：buffer 過大但從未偵測到語音能量（buffer_start_time 仍 None），
        # 且整段確認為靜音 → 丟棄，避免純靜音軌（如無聲的「對方」軌）無限成長。
        # 加上 RMS 複查確保不會誤刪低音量但 Silero 偵測中的長語音。
        elif (
            tb.buffer_start_time is None
            and len(tb.pcm_buffer) > MAX_SILENT_SAMPLES
            and float(np.sqrt(np.mean(tb.pcm_buffer.flatten() ** 2))) < rms_silence
        ):
            tb.pcm_buffer = np.empty((0, 1), dtype=np.float32)
            tb.vad_frame_buf = np.empty(0, dtype=np.float32)
            if tb.vad_iterator is not None:
                tb.vad_iterator.reset_states()

    def _update_preview(self, samples: np.ndarray, rms_silence: float) -> None:
        """更新滾動預覽 buffer，每 interval 觸發一次預覽 ASR（mono，分軌時為混音）。"""
        incoming_rms = float(np.sqrt(np.mean(samples.flatten() ** 2)))

        # 更新滾動預覽 buffer（保留最近 5 秒）
        self._preview_buffer = np.concatenate((self._preview_buffer, samples), axis=0)
        if len(self._preview_buffer) > self._preview_window_frames:
            self._preview_buffer = self._preview_buffer[-self._preview_window_frames:]

        # 每 300ms 觸發一次預覽
        self._frames_since_preview += len(samples)
        if self._frames_since_preview >= self._preview_interval_frames:
            self._frames_since_preview = 0
            if incoming_rms >= rms_silence:
                # 有語音能量：送 5s 視窗給預覽 ASR
                self._preview_silence_samples = 0
                audio_snapshot = self._preview_buffer.copy()
                try:
                    self._preview_queue.put_nowait(audio_snapshot)
                except queue.Full:
                    pass
            else:
                # 靜音：累積，超過 1s 後送一次清除信號，讓預覽消失
                self._preview_silence_samples += self._preview_interval_frames
                if self._preview_silence_samples >= self._preview_silence_clear_samples:
                    self._preview_silence_samples = 0
                    try:
                        # 空陣列作為「清除預覽」的 sentinel
                        self._preview_queue.put_nowait(np.empty((0, 1), dtype=np.float32))
                    except queue.Full:
                        pass

    async def _flush_chunk(self, tb: _TrackBuf, audio: np.ndarray) -> None:
        """Write a complete segment to disk and enqueue it for the ASR worker."""
        # 取出本軌前一個 chunk 的尾端音訊作為 overlap
        overlap = tb.overlap_buffer
        actual_overlap_secs = len(overlap) / SAMPLE_RATE

        # 更新 overlap buffer：保留本次音訊的最後 self._overlap_samples
        tail_samples = min(self._overlap_samples, len(audio))
        tb.overlap_buffer = audio[-tail_samples:].copy() if tail_samples > 0 else np.empty((0, 1), dtype=np.float32)

        # WAV = [overlap] + [本次音訊]
        full_audio = np.concatenate([overlap, audio], axis=0) if len(overlap) > 0 else audio

        path = Path(self.state.output_dir) / "chunks" / f"chunk_{self._chunk_index:06d}.wav"
        sf.write(path, full_audio, SAMPLE_RATE)

        duration = len(audio) / SAMPLE_RATE  # 本次音訊的長度（不含 overlap）
        audio_rms = float(np.sqrt(np.mean(audio.flatten() ** 2)))
        chunk = AudioChunk(
            index=self._chunk_index,
            started_at=tb.chunk_start,
            duration=duration,
            path=path,
            overlap_seconds=actual_overlap_secs,
            rms=audio_rms,
            track=tb.index,
            speaker=tb.speaker,
        )
        tb.chunk_start += duration
        self._chunk_index += 1
        self._chunk_queue.put(chunk)
        await self.emit({"type": "stream.log", "message": f"Streamed {path.name}"})

    def notify_recording_ended(self) -> None:
        """Called (synchronously) when the browser audio WebSocket disconnects.

        Flushes any remaining PCM tail, then signals the ASR worker to finish.
        Safe to call from asyncio context — sync I/O is brief (<1 ms for a tail chunk).
        """
        if self.state.status not in {"recording", "transcribing", "stopping"}:
            return
        for tb in self._tracks:
            if len(tb.pcm_buffer) < MIN_TAIL_FRAMES:
                continue
            tail = tb.pcm_buffer
            tb.pcm_buffer = np.empty((0, 1), dtype=np.float32)
            duration = len(tail) / SAMPLE_RATE
            tail_rms = float(np.sqrt(np.mean(tail.flatten() ** 2)))
            path = Path(self.state.output_dir) / "chunks" / f"chunk_{self._chunk_index:06d}.wav"
            sf.write(path, tail, SAMPLE_RATE)
            self._chunk_queue.put(
                AudioChunk(
                    index=self._chunk_index,
                    started_at=tb.chunk_start,
                    duration=duration,
                    path=path,
                    rms=tail_rms,
                    track=tb.index,
                    speaker=tb.speaker,
                )
            )
            self._chunk_index += 1
        self._stop_event.set()

    async def stop(self) -> dict[str, Any]:
        if self.state.status not in {"recording", "transcribing"}:
            return self.snapshot()
        self._explicit_stop = True   # 標記為手動中止，_asr_worker 收到後略過後處理
        self.state.status = "stopping"
        self._stop_event.set()
        # 清空佇列，讓 ASR worker 在處理完目前這塊後立即結束
        while not self._chunk_queue.empty():
            try:
                self._chunk_queue.get_nowait()
            except queue.Empty:
                break
        # 停 LLM 校正 worker（drain 剩餘批次）；失敗不影響 stop
        if self._correction_worker is not None:
            try:
                self._correction_worker.stop(timeout=10.0)
            except Exception:
                pass
            self._correction_worker = None
        await self.emit({"type": "stream.state", "state": self.snapshot()})
        return self.snapshot()

    def request_stop(self) -> None:
        if self.state.status in {"recording", "transcribing", "stopping"}:
            self.state.status = "stopping"
            self._stop_event.set()

    def _make_output_dir(self, output_dir: str | None) -> Path:
        if output_dir:
            path = Path(output_dir).expanduser().resolve()
        else:
            path = (Path("data/output") / f"desktop-{time.strftime('%Y%m%d-%H%M%S')}").resolve()
        (path / "chunks").mkdir(parents=True, exist_ok=True)
        return path

    def _emit_threadsafe(self, loop: asyncio.AbstractEventLoop, payload: dict[str, Any]) -> None:
        asyncio.run_coroutine_threadsafe(self.emit(payload), loop)

    def _preview_worker(self, loop: asyncio.AbstractEventLoop) -> None:
        """以 tiny 模型每秒跑一次滾動視窗預覽，結果即時顯示在 UI。

        與 _asr_worker 平行執行，使用獨立的模型實例，互不干擾。
        """
        import os
        import tempfile

        from .faster_asr import FasterWhisperAsr

        try:
            preview_asr = FasterWhisperAsr(
                model_size=os.getenv("PREVIEW_ASR_MODEL", "tiny"),
                device=os.getenv("LIVE_ASR_DEVICE", "cpu"),
                compute_type=os.getenv("LIVE_ASR_COMPUTE_TYPE", "int8"),
            )
        except Exception as err:  # noqa: BLE001
            self._emit_threadsafe(loop, {"type": "stream.log", "message": f"Preview ASR 載入失敗: {err}"})
            return

        try:
            while not self._stop_event.is_set():
                try:
                    audio = self._preview_queue.get(timeout=0.5)
                except queue.Empty:
                    continue

                # 空陣列 sentinel：1s 靜音後由 push_pcm 送入，清除預覽文字
                if len(audio) == 0:
                    self._emit_threadsafe(loop, {"type": "transcript.preview", "text": ""})
                    continue

                if len(audio) < self._preview_min_audio_samples:
                    continue

                # RMS 能量檢查：靜音時不送 ASR，避免 tiny 模型從雜訊輸出隨機文字
                # 預覽用的閾值為主 RMS 閾值的一半（更寬鬆，因為預覽容忍噪音）
                rms = float(np.sqrt(np.mean(audio.flatten() ** 2)))
                if rms < self._settings["audio.rms_silence_threshold"] * 0.5:
                    continue  # 靜音，直接略過，不發送 preview 事件（避免 UI 閃爍）

                # 進行耗時的 ASR 前再檢查一次 stop（避免停止後多跑一輪）
                if self._stop_event.is_set():
                    break

                tmp_path: Path | None = None
                try:
                    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
                        sf.write(f.name, audio, SAMPLE_RATE)
                        tmp_path = Path(f.name)
                    result = preview_asr.transcribe(tmp_path)
                    text = result.get("text", "").strip()
                    if text:
                        from .itn import normalize as _itn_normalize
                        text = _itn_normalize(text)
                        self._emit_threadsafe(loop, {"type": "transcript.preview", "text": text})
                except Exception:  # noqa: BLE001
                    pass
                finally:
                    if tmp_path and tmp_path.exists():
                        tmp_path.unlink(missing_ok=True)
        finally:
            # 停止後強制清理 preview ASR 的 MLX subprocess（不清會以孤兒殘留）
            from .faster_asr import _hard_kill_executor
            executor = getattr(preview_asr, "_mlx_executor", None)
            if executor is not None:
                _hard_kill_executor(executor)

    def _asr_worker(
        self,
        output_dir: Path,
        loop: asyncio.AbstractEventLoop,
        language: str | None = None,
    ) -> None:
        transcript: dict[str, Any] = {
            "audio_file": "microphone-stream",
            "language": "zh-TW",
            "mode": "desktop-faster-whisper-live-stream",
            "segments": [],
        }

        try:
            from .faster_asr import FasterWhisperAsr


            # 非阻塞檢查：preload 已完成就用（fast path），否則建立新 instance
            # 新 instance 的第一個 chunk 會慢（subprocess 冷啟動 ~9s），但不會有 content loss
            if _preload_ready.is_set() and _PRELOADED_ASR is not None:
                live_asr = _PRELOADED_ASR
                self._emit_threadsafe(loop, {"type": "stream.log", "message": "ASR ready (pre-loaded model)"})
            else:
                live_asr = FasterWhisperAsr()
                self._emit_threadsafe(loop, {"type": "stream.log", "message": "ASR ready (first chunk may be slow ~9s)"})

            # === 從 settings 載入本次錄音用的可調參數（snapshot；錄音中變更不影響）===
            _S = self._settings
            _CONTEXT_MAX_CHARS = _S["context.max_chars"]
            _LANG_WHITELIST = set(_S["lang.whitelist"])
            _SWITCH_CONFIRM = _S["lang.switch_confirm"]
            _RATE_FALLBACK = _S["rate.fallback"]
            _RATE_MIN_SAMPLES = _S["rate.min_samples"]
            _RATE_SIGMA = _S["rate.sigma"]
            _RATE_HARD_LIMIT = _S["rate.hard_limit"]
            _RATE_MIN_SEG_DUR = _S["rate.min_seg_duration"]
            _RMS_SILENCE = _S["audio.rms_silence_threshold"]
            _MAX_BACKLOG = _S["queue.max_backlog"]
            _SEG_MIN_LEN = _S["segment.min_text_len"]

            # 「主要語言」：從 UI 傳來的 hint，不強制 Whisper decode，但影響：
            #   - 自動加入白名單（保證 hint 語言能被接受）
            #   - sticky bias：detected == primary 時 1-confirm 即可（其他仍需 _SWITCH_CONFIRM）
            # 'auto' / None / 非白名單代碼 → 視為「無偏好」
            _PRIMARY_LANG = language if language in {"zh", "en", "yue"} else None
            if _PRIMARY_LANG:
                _LANG_WHITELIST.add(_PRIMARY_LANG)
                self._emit_threadsafe(loop, {
                    "type": "stream.log",
                    "message": f"[lang] primary={_PRIMARY_LANG} (cold-start 1-confirm bias)",
                })
            # =====================================================================

            # 每軌獨立的跨-chunk 上下文（rolling ctx / 語言 sticky / 語速 / segments）。
            # 混音模式只用 track 0；分軌模式 track 0=你, 1=對方，彼此不互相污染。
            _track_ctxs: dict[int, _TrackCtx] = {}

            # 不使用冷啟動種子（_COLD_START_SEED）：
            # 中文種子 "以下是台灣繁體中文的會議記錄。" 會在無說話的背景噪音 chunk 中
            # 引導 Whisper 往訓練記憶（如 YouTube 推廣語）方向生成幻覺。
            # language 參數已傳給 mlx_whisper，語言偵測不依賴 initial_prompt。
            # → 冷啟動時 initial_prompt=None，讓 Whisper 不帶偏見地判斷是否有語音。

            # Process chunks until stop is signalled AND the queue is drained
            while not (self._stop_event.is_set() and self._chunk_queue.empty()):
                try:
                    chunk = self._chunk_queue.get(timeout=0.5)
                except queue.Empty:
                    continue

                # 若積壓過深，持續以新 chunk 替換舊 chunk，直到 backlog ≤ _MAX_BACKLOG
                while self._chunk_queue.qsize() > _MAX_BACKLOG:
                    try:
                        newer = self._chunk_queue.get_nowait()
                        self._emit_threadsafe(
                            loop,
                            {"type": "stream.log", "message": f"[skip] backlog={self._chunk_queue.qsize()+1}, dropped {chunk.path.name}"},
                        )
                        self._chunk_queue.task_done()
                        chunk = newer
                    except queue.Empty:
                        break

                # 靜音 chunk：RMS < 閾值 → 跳過，不進 ASR 也不汙染 rolling context
                if chunk.rms < _RMS_SILENCE:
                    self._emit_threadsafe(loop, {"type": "stream.log", "message": f"[skip] {chunk.path.name} silence (rms={chunk.rms:.4f})"})
                    self._chunk_queue.task_done()
                    continue

                # stop() 可能在 queue.get() 等待期間被呼叫：再確認一次，避免對已停止的 session 開新一輪 ASR
                if self._stop_event.is_set() and self._chunk_queue.empty():
                    self._chunk_queue.task_done()
                    break

                # 取本 chunk 所屬音軌的上下文（混音=track 0；分軌=0 你 / 1 對方）
                ctx = _track_ctxs.setdefault(chunk.track, _TrackCtx())

                self.state.status = "transcribing"
                self.state.backlog = self._chunk_queue.qsize()
                self._emit_threadsafe(loop, {"type": "stream.state", "state": self.snapshot()})

                # 🔍 lag 監控：從 session 起點推算 chunk 音訊結束的牆鐘時間
                # → 現在的牆鐘時間扣掉就是「畫面與音訊的延遲」
                _now = time.time()
                _audio_end = chunk.started_at + chunk.duration
                _session_start = self.state.started_at or _now
                _wall_lag = _now - (_session_start + _audio_end)
                _chunk_t0 = _now  # 給下方算 ASR 耗時用

                self._emit_threadsafe(
                    loop, {"type": "stream.log",
                           "message": f"Transcribing {chunk.path.name} "
                                      f"[queue={self._chunk_queue.qsize()} "
                                      f"lag={_wall_lag:.1f}s "
                                      f"audio={chunk.duration:.1f}s]"}
                )

                # ── 音訊靜音偵測：逐幀 RMS，確認有足夠語音才送 ASR ──────────
                # 用聲音能量本身判斷，不依賴文字模式過濾。
                # 解決：整體 RMS 勉強超過閾值，但大部分是靜音的 chunk 仍被送進 ASR 的問題。
                _chunk_audio, _ = sf.read(str(chunk.path), dtype="float32", always_2d=True)
                _flat = _chunk_audio.flatten()
                _fr = VAD_FRAME_SIZE  # 512 samples = 32ms @ 16kHz
                _n_fr = len(_flat) // _fr
                _asr_chunk = chunk
                _trim_path: Path | None = None

                if _n_fr > 0:
                    _frms = np.sqrt(np.mean(_flat[:_n_fr * _fr].reshape(_n_fr, _fr) ** 2, axis=1))
                    _speech_mask = _frms > _RMS_SILENCE
                    _speech_secs = float(np.sum(_speech_mask) * _fr / SAMPLE_RATE)

                    if _speech_secs < self._min_speech_in_chunk_seconds:
                        # 語音不足 → 跳過，完全靜音或背景音
                        self._emit_threadsafe(loop, {
                            "type": "stream.log",
                            "message": f"[skip] {chunk.path.name} no speech ({_speech_secs:.2f}s)",
                        })
                        self._chunk_queue.task_done()
                        continue

                    # 前導靜音：找到第一個語音幀，若超過閾值就修剪掉
                    _first = int(np.argmax(_speech_mask)) if np.any(_speech_mask) else _n_fr
                    _lead_secs = _first * _fr / SAMPLE_RATE
                    if _lead_secs >= self._leading_silence_trim_seconds:
                        _trim_samples = _first * _fr
                        _trim_path = chunk.path.parent / f"_trim_{chunk.path.name}"
                        sf.write(_trim_path, _chunk_audio[_trim_samples:], SAMPLE_RATE)
                        # 調整時間基準：前導靜音修剪後，Whisper 的時間戳以修剪後起點為基準
                        if _lead_secs <= chunk.overlap_seconds:
                            _new_ov = chunk.overlap_seconds - _lead_secs
                            _new_st = chunk.started_at
                        else:
                            _new_ov = 0.0
                            _new_st = chunk.started_at + (_lead_secs - chunk.overlap_seconds)
                        _asr_chunk = AudioChunk(
                            index=chunk.index,
                            started_at=_new_st,
                            duration=chunk.duration,
                            path=_trim_path,
                            overlap_seconds=_new_ov,
                            rms=chunk.rms,
                        )

                # 取當前語言對應的 context（不同語言獨立 buffer，避免跨語言污染）
                _rolling_context = ctx.rolling_ctx_by_lang.get(ctx.current_lang or "", "")

                try:
                    # ASR 推理前通知 LLM worker 讓出 GPU
                    if self._correction_worker is not None:
                        self._correction_worker.asr_busy()
                    # Whisper 永遠 auto-detect：user 的「主要語言」只作 sticky bias / whitelist，
                    # 不該強制 decode 語言（強制會在多語場景誘發迴圈幻覺）
                    asr_result = live_asr.transcribe(
                        _asr_chunk.path,
                        initial_prompt=_rolling_context or None,
                        language="auto",
                        settings=_S,
                    )
                finally:
                    if _trim_path and _trim_path.exists():
                        _trim_path.unlink(missing_ok=True)
                    # ASR 完畢，放行 LLM dispatch
                    if self._correction_worker is not None:
                        self._correction_worker.asr_idle()

                # transcribe 已附贈本 chunk 的語言偵測結果（內部完整 decode 後決定）
                _detected_lang = (asr_result.get("language") or "").lower() or None

                new_segments = _offset_asr_result(asr_result, _asr_chunk)
                new_segments = _merge_short_segments(
                    new_segments,
                    max_gap=_S["segment.merge_max_gap_seconds"],
                    min_duration=_S["segment.merge_min_duration_seconds"],
                )

                # ── overlap 文字去重 ──────────────────────────────────────────
                # 新 chunk 開頭若與「同軌」上一 segment 結尾重疊，截掉重複部分（最多比對 8 字）
                if ctx.segments and new_segments:
                    last_text = ctx.segments[-1].get("text", "")
                    first_text = new_segments[0].get("text", "")
                    for n in range(min(8, len(last_text), len(first_text)), 0, -1):
                        if last_text.endswith(first_text[:n]):
                            deduped = first_text[n:].lstrip()
                            if deduped.strip():
                                new_segments[0] = {**new_segments[0], "text": deduped}
                            else:
                                new_segments.pop(0)
                            break

                # 過濾極短雜訊 segment（_SEG_MIN_LEN 來自 settings）
                new_segments = [s for s in new_segments if len((s.get("text") or "").strip()) >= _SEG_MIN_LEN]

                # ── 動態說話速率檢查（偵測 Whisper 迴圈幻覺）────────────────────
                # 迴圈幻覺特徵：同一段音訊把重複句子全塞進一個 segment，字/秒遠超正常
                # 閾值由本 session 實際語速動態計算，不依賴固定數字
                if new_segments:
                    # 計算當前動態閾值
                    if len(ctx.accepted_rates) >= _RATE_MIN_SAMPLES:
                        _rmean = sum(ctx.accepted_rates) / len(ctx.accepted_rates)
                        _rvar = sum((r - _rmean) ** 2 for r in ctx.accepted_rates) / len(ctx.accepted_rates)
                        _rstd = _rvar ** 0.5
                        _rate_limit = max(_rmean + _RATE_SIGMA * _rstd, _RATE_FALLBACK)
                    else:
                        _rate_limit = _RATE_FALLBACK  # 冷啟動期用固定保守值

                    _rate_out = []
                    for _seg in new_segments:
                        _stxt = (_seg.get("text") or "").strip()
                        _sdur = (_seg.get("end") or 0.0) - (_seg.get("start") or 0.0)
                        if _sdur >= _RATE_MIN_SEG_DUR and len(_stxt) > 0:
                            _rate = len(_stxt) / _sdur
                            if _rate > _rate_limit:
                                self._emit_threadsafe(loop, {
                                    "type": "stream.log",
                                    "message": (
                                        f"[rate] dropped '{_stxt[:30]}' "
                                        f"({_rate:.1f} chars/s > limit {_rate_limit:.1f})"
                                    ),
                                })
                                continue
                            # 接受：記錄此 segment 語速供動態調整（保留最近 50 個）
                            ctx.accepted_rates.append(_rate)
                            if len(ctx.accepted_rates) > 50:
                                ctx.accepted_rates = ctx.accepted_rates[-50:]
                        _rate_out.append(_seg)
                    new_segments = _rate_out

                # 幻覺傳播保護：若某 seg 的文字已出現在「任何語言桶」的 rolling context 中
                # → 合併所有桶比對（substring 跨語言不會誤殺）
                # 避免：detected_lang 抖動 → 內容散落多桶 → halluc check 抓不到迴圈
                _all_ctx = "".join(ctx.rolling_ctx_by_lang.values())
                if _all_ctx and new_segments:
                    _halluc_kept = []
                    for seg in new_segments:
                        seg_text = (seg.get("text") or "").strip()
                        if seg_text and seg_text in _all_ctx:
                            self._emit_threadsafe(loop, {
                                "type": "stream.log",
                                "message": f"[hallucination] dropped '{seg_text[:30]}' (in rolling ctx)",
                            })
                        else:
                            _halluc_kept.append(seg)
                    new_segments = _halluc_kept

                # ── coherence check 已移除 ────────────────────────────────────
                # 原本用 bigram 交集擋「記憶化幻覺」（如「請多多關注」），但對中文
                # topic shift 會大量誤殺合法新主題（中文每句 bigram 集合稀疏，
                # 不同主題天然零重疊）。
                #
                # 同類幻覺改交由其他防護：
                #   - 白名單 {zh, en, yue}：擋掉 ja/ko/ru/de 等偵測誤判
                #   - 第一 chunk 2-confirm：擋掉冷啟動單句幻覺
                #   - _all_ctx 字面重複比對：擋掉迴圈
                #   - nsp > 0.9：擋掉無語音段
                # ─────────────────────────────────────────────────────────

                # ── 時間戳重疊修正 ────────────────────────────────────────────
                # overlap 邊界 segment 的 start 有時比前一 chunk 最後一個 segment 的 end 還早
                # → 將 start 往前移至 prev_end，時間已耗盡者整段丟棄
                _prev_end = (ctx.segments[-1].get("end") or 0.0) if ctx.segments else 0.0
                new_segments = _enforce_chronological(new_segments, _prev_end)

                # ── 語言切換 sticky 邏輯（含白名單 + 主要語言 bias）────
                # 全噪音/全幻覺 chunk（new_segments=[]）的 detected lang 不可靠，不更新狀態。
                # 白名單外的偵測語言（ja/ko/ru…）：本錄音不支援，視同雜訊，不更新狀態。
                # 預設第一 chunk + 切換都要 _SWITCH_CONFIRM 個同語言才算數。
                # 主要語言 bias：detected == _PRIMARY_LANG → 1-confirm 即可
                #   - init: 第一個就偵測到主要語言 → 立刻 init（高度信任）
                #   - switch: 切回主要語言 → 1 chunk 確認（回到預期狀態，省一個 chunk）
                #   - 切離主要語言 → 仍需 _SWITCH_CONFIRM（避免噪音誤離開）
                _lang_in_whitelist = _detected_lang in _LANG_WHITELIST if _detected_lang else False
                # 動態調整本輪確認門檻：detected 是主要語言 → 1，否則 _SWITCH_CONFIRM
                _needed_confirm = 1 if (_PRIMARY_LANG and _detected_lang == _PRIMARY_LANG) else _SWITCH_CONFIRM
                if new_segments and _lang_in_whitelist:
                    if ctx.current_lang is None:
                        # 第一 chunk 也走 pending 流程
                        if _detected_lang == ctx.pending_lang:
                            ctx.pending_count += 1
                        else:
                            ctx.pending_lang = _detected_lang
                            ctx.pending_count = 1
                        if ctx.pending_count >= _needed_confirm:
                            ctx.current_lang = _detected_lang
                            ctx.pending_lang = None
                            ctx.pending_count = 0
                            _bias_tag = " (primary-bias 1-confirm)" if _needed_confirm == 1 else f" (confirmed after {_needed_confirm} chunks)"
                            self._emit_threadsafe(loop, {
                                "type": "stream.log",
                                "message": f"[lang] init={ctx.current_lang}{_bias_tag}",
                            })
                    elif _detected_lang == ctx.current_lang:
                        # 與當前一致，重置 pending（之前的切換意圖被打斷）
                        ctx.pending_lang = None
                        ctx.pending_count = 0
                    else:
                        if _detected_lang == ctx.pending_lang:
                            ctx.pending_count += 1
                        else:
                            ctx.pending_lang = _detected_lang
                            ctx.pending_count = 1
                        if ctx.pending_count >= _needed_confirm:
                            _bias_tag = " (primary-bias 1-confirm)" if _needed_confirm == 1 else ""
                            self._emit_threadsafe(loop, {
                                "type": "stream.log",
                                "message": f"[lang] switch {ctx.current_lang}→{_detected_lang}{_bias_tag}",
                            })
                            ctx.current_lang = _detected_lang
                            ctx.pending_lang = None
                            ctx.pending_count = 0

                # 更新對應語言的 rolling context
                # 讀＆寫都用 _current_lang（已 sticky 確認），避免 _detected_lang 抖動。
                # 第一 chunk 確認前：_current_lang 為 None → 退回到「白名單內的 detected」桶
                # 暫存；若是孤立幻覺，下個正常 chunk 不會被它污染。
                _ctx_key = ctx.current_lang or (_detected_lang if _lang_in_whitelist else "") or ""
                if _ctx_key and new_segments:
                    _ctx = ctx.rolling_ctx_by_lang.get(_ctx_key, "")
                    for seg in new_segments:
                        _ctx += (seg.get("text") or "").strip()
                    if len(_ctx) > _CONTEXT_MAX_CHARS:
                        _ctx = _ctx[-_CONTEXT_MAX_CHARS:]
                    ctx.rolling_ctx_by_lang[_ctx_key] = _ctx
                    _rolling_context = _ctx

                # ── ITN 後處理：數字 / 日期 / 時間 / 電話正規化 ──────────────
                from .itn import apply_itn_to_segments
                new_segments = apply_itn_to_segments(new_segments)

                # ── 分配穩定 line_id + 標記 is_complete ───────────────────
                # 每個 emit 出去的 segment 取得 monotonic id；VAD 已確認句末，
                # 故發出當下即為終態（is_complete=True），前端不應再改它的文字
                # （Breeze 後處理是例外，會以 line_id 重新發 correction 事件）
                for seg in new_segments:
                    seg["line_id"] = self._next_line_id
                    seg["is_complete"] = True
                    seg["speaker"] = chunk.speaker  # 分軌：你/對方；混音：None
                    self._next_line_id += 1

                transcript["segments"].extend(new_segments)
                ctx.segments.extend(new_segments)  # 同軌 segments（供下個 chunk 去重/時序）
                self.state.segment_count += len(new_segments)
                self._save_transcript(transcript, output_dir, "transcript")

                if not new_segments:
                    self._emit_threadsafe(
                        loop,
                        {"type": "stream.log", "message": f"No speech text detected in {chunk.path.name}"},
                    )
                for segment in new_segments:
                    self._emit_threadsafe(loop, {"type": "transcript.segment", "segment": segment})
                    self._append_display_log(output_dir, segment)
                    self._line_id_to_segment[segment["line_id"]] = segment
                    # 餵給 LLM 校正 worker（若 enabled；未啟動則 push 為 no-op）
                    if self._correction_worker is not None:
                        try:
                            from .correction_worker import Segment as _CorrSeg
                            self._correction_worker.push(_CorrSeg(
                                line_id=segment["line_id"],
                                text=segment.get("text", ""),
                                start=segment.get("start") or 0.0,
                                end=segment.get("end") or 0.0,
                                speaker=segment.get("speaker"),
                            ))
                        except Exception:
                            pass  # 校正失敗不該影響主流程

                # 🔍 lag 監控：ASR 處理耗時
                _proc_time = time.time() - _chunk_t0
                _audio_ratio = _proc_time / max(0.1, chunk.duration)
                _ratio_warn = " ⚠️SLOW" if _audio_ratio > 1.5 else ""
                self._emit_threadsafe(loop, {
                    "type": "stream.log",
                    "message": f"[lag] {chunk.path.name} done in {_proc_time:.2f}s "
                               f"(audio={chunk.duration:.1f}s, ratio={_audio_ratio:.2f}x){_ratio_warn}"
                })

                self.state.status = "recording" if not self._stop_event.is_set() else "stopping"
                self.state.backlog = self._chunk_queue.qsize()
                self._emit_threadsafe(loop, {"type": "stream.state", "state": self.snapshot()})
                self._chunk_queue.task_done()

            # 先存一份逐字稿（不論是否手動中止）
            self._save_transcript(transcript, output_dir, "transcript")
            self._save_llm_corrected_transcript(transcript, output_dir)

            if self._explicit_stop:
                self._emit_threadsafe(
                    loop, {"type": "stream.log", "message": "手動中止，略過 Breeze 校正與 LLM 摘要。"}
                )
                self.state.status = "idle"
                self._emit_threadsafe(loop, {"type": "stream.state", "state": self.snapshot()})
                return

            self.state.status = "idle"
            self._emit_threadsafe(loop, {"type": "stream.state", "state": self.snapshot()})
        except Exception as err:  # noqa: BLE001
            self.state.status = "error"
            self.state.error = str(err)
            self._emit_threadsafe(loop, {"type": "stream.state", "state": self.snapshot()})
            self._emit_threadsafe(loop, {"type": "stream.log", "message": f"Error: {err}"})
        finally:
            self._stop_event.set()

    def _silero_end_of_speech(self, audio: np.ndarray) -> bool:
        """RMS 能量自適應偵測句末：buffer 有語音，且最後 N 幀靜音。

        改用 RMS 而非 Silero VAD，因為 faster-whisper 的 VAD model 是 stateful，
        對整段 buffer 重複呼叫結果不可靠。
        """
        n_frames = len(audio) // VAD_FRAME_SIZE
        if n_frames < self._vad_silence_frames + 3:
            return False

        frames = audio[: n_frames * VAD_FRAME_SIZE].reshape(n_frames, VAD_FRAME_SIZE)
        rms = np.sqrt(np.mean(frames ** 2, axis=1))

        peak = float(rms.max())
        if peak < self._settings["audio.rms_silence_threshold"]:
            return False  # 絕對能量太低（背景噪音），不觸發 VAD

        # 自適應閾值：語音幀 = 超過峰值的 X 倍（X 來自 settings.vad.peak_ratio）
        threshold = peak * self._vad_peak_ratio
        is_speech = rms > threshold

        vad_frames = self._vad_silence_frames
        had_speech = bool(np.any(is_speech[:-vad_frames]))
        recent_silent = bool(np.all(~is_speech[-vad_frames:]))
        return had_speech and recent_silent

    def _emit_breeze_corrections(
        self,
        original: list[dict[str, Any]],
        breeze: list[dict[str, Any]],
        loop: asyncio.AbstractEventLoop,
    ) -> None:
        """依時間重疊將 Breeze 校正結果對應回原始 segment，以 line_id 重綁，
        只對文字有差異的發送更新事件（前端用 line_id 找對應行，不受排序影響）。"""
        for orig in original:
            line_id = orig.get("line_id")
            if line_id is None:
                continue  # 舊資料沒有 line_id 就跳過，避免錯誤對應
            orig_start: float = orig.get("start") or 0.0
            orig_end: float = orig.get("end") or 0.0
            orig_text: str = (orig.get("text") or "").strip()

            overlapping = [
                s for s in breeze
                if (s.get("start") or 0.0) < orig_end and (s.get("end") or 0.0) > orig_start
            ]
            if not overlapping:
                continue

            corrected_text = "".join(s.get("text", "").strip() for s in overlapping).strip()
            if corrected_text and corrected_text != orig_text:
                self._emit_threadsafe(loop, {
                    "type": "transcript.correction",
                    "correction": {"line_id": line_id, "text": corrected_text},
                })
                # log：記錄校正前後
                wall = time.strftime("%H:%M:%S")
                line = f"[{wall}] [CORRECTION line_id={line_id}] {orig_text!r} → {corrected_text!r}\n"
                try:
                    with open(self._display_log_path, "a", encoding="utf-8") as f:
                        f.write(line)
                except Exception:
                    pass

    def _record_llm_correction(self, line_ids: list[int], corrected_text: str) -> None:
        first_lid = line_ids[0]
        self._llm_corrections[first_lid] = corrected_text
        self._llm_merge_groups[first_lid] = line_ids
        for lid in line_ids[1:]:
            self._llm_merged_secondary.add(lid)
        self._append_llm_correction_log(line_ids, corrected_text)

    def _append_llm_correction_log(self, line_ids: list[int], corrected_text: str) -> None:
        if self._display_log_path is None:
            return
        try:
            wall = time.strftime("%H:%M:%S")
            originals = [
                (self._line_id_to_segment.get(lid) or {}).get("text", "?")
                for lid in line_ids
            ]
            ids_str = (
                f"line_ids={','.join(str(x) for x in line_ids)}"
                if len(line_ids) > 1
                else f"line_id={line_ids[0]}"
            )
            orig_str = " | ".join(originals)
            with open(self._display_log_path, "a", encoding="utf-8") as f:
                f.write(f"[{wall}] [LLM_CORRECTION {ids_str}] {orig_str!r} → {corrected_text!r}\n")
        except Exception:
            pass

    def _save_llm_corrected_transcript(self, transcript: dict[str, Any], output_dir: Path) -> None:
        if not self._llm_corrections:
            return
        new_segments: list[dict[str, Any]] = []
        for seg in transcript["segments"]:
            lid = seg.get("line_id")
            if lid in self._llm_merged_secondary:
                continue  # 已被合併進前一行，跳過
            if lid in self._llm_corrections:
                group = self._llm_merge_groups.get(lid, [lid])
                end_time = seg.get("end")
                for other_lid in group[1:]:
                    other_end = (self._line_id_to_segment.get(other_lid) or {}).get("end")
                    if other_end is not None:
                        end_time = other_end
                new_segments.append({
                    **seg,
                    "text": self._llm_corrections[lid],
                    "end": end_time,
                    "llm_corrected": True,
                })
            else:
                new_segments.append(seg)
        llm_corrected = {**transcript, "segments": new_segments, "mode": "llm-corrected-live"}
        self._save_transcript(llm_corrected, output_dir, "llm_corrected_transcript")

    def _append_display_log(self, output_dir: Path, segment: dict[str, Any]) -> None:
        """即時 append 一個剛顯示到畫面上的 segment。"""
        if self._display_log_path is None:
            return
        try:
            wall = time.strftime("%H:%M:%S")
            start = segment.get("start")
            end = segment.get("end")
            def fmt(v: float | None) -> str:
                if v is None:
                    return "--:--"
                t = int(max(0, v))
                return f"{t // 60:02d}:{t % 60:02d}"
            audio_ts = f"{fmt(start)}-{fmt(end)}"
            text = (segment.get("text") or "").strip()
            with open(self._display_log_path, "a", encoding="utf-8") as f:
                f.write(f"[{wall}] [{audio_ts}] {text}\n")
        except Exception:
            pass

    def _save_transcript(self, transcript: dict[str, Any], output_dir: Path, stem: str) -> None:
        text = transcript_to_text(transcript)
        (output_dir / f"{stem}.json").write_text(
            json.dumps(transcript, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        (output_dir / f"{stem}.txt").write_text(text, encoding="utf-8")

    def _merge_chunks(self, output_dir: Path) -> Path | None:
        chunk_paths = sorted((output_dir / "chunks").glob("chunk_*.wav"))
        if not chunk_paths:
            return None

        merged_path = output_dir / "meeting_audio.wav"
        with sf.SoundFile(merged_path, mode="w", samplerate=SAMPLE_RATE, channels=1, subtype="PCM_16") as out:
            for path in chunk_paths:
                audio, sample_rate = sf.read(path, dtype="float32", always_2d=True)
                if sample_rate != SAMPLE_RATE:
                    continue
                out.write(audio)
        return merged_path

    def _run_breeze_final_correction(
        self,
        output_dir: Path,
        loop: asyncio.AbstractEventLoop,
    ) -> dict[str, Any] | None:
        try:
            from .asr import BreezeAsr

            merged_audio = self._merge_chunks(output_dir)
            if merged_audio is None:
                self._emit_threadsafe(loop, {"type": "stream.log", "message": "No chunks for Breeze correction"})
                return None

            asr_result = BreezeAsr().transcribe(merged_audio)
            corrected = build_transcript(merged_audio, asr_result)
            corrected["mode"] = "breeze-asr-25-final-correction"
            self._save_transcript(corrected, output_dir, "final_transcript")
            self._emit_threadsafe(
                loop, {"type": "stream.log", "message": "Saved Breeze corrected final_transcript.txt"}
            )
            return corrected
        except Exception as err:  # noqa: BLE001
            self._emit_threadsafe(
                loop, {"type": "stream.log", "message": f"Breeze correction failed: {err}"}
            )
            return None

    async def generate_summary(self) -> dict[str, Any]:
        """錄音結束後由前端明確觸發：讀取最後一份逐字稿，呼叫 LLM 產生會議記錄摘要。

        不影響錄音主流程——這是完全獨立的後置步驟。
        成功 → {"ok": True, "path": "..."}
        失敗 → {"ok": False, "error": "..."}
        """
        output_dir = self.state.output_dir
        if not output_dir:
            return {"ok": False, "error": "尚無錄音輸出目錄"}

        path = Path(output_dir)
        # 優先讀 Breeze 校正後的版本，否則用即時稿
        for stem in ("final_transcript", "llm_corrected_transcript", "transcript"):
            txt_path = path / f"{stem}.txt"
            if txt_path.exists():
                transcript_text = txt_path.read_text(encoding="utf-8")
                break
        else:
            return {"ok": False, "error": "找不到逐字稿檔案"}

        try:
            import re as _re
            # 去掉時間戳與講者標記（格式：[MM:SS.s - MM:SS.s] 你: 或 Speaker:）
            # 讓模型看到的是純口語文字，避免格式干擾判斷
            clean_text = _re.sub(r'^\[[\d:.\s\-]+\]\s*[^:]+:\s*', '', transcript_text, flags=_re.MULTILINE)
            clean_text = '\n'.join(line for line in clean_text.splitlines() if line.strip())

            from .summarizer import save_minutes, summarize_transcript
            minutes = summarize_transcript(clean_text, self._settings)
            if minutes is None:
                return {"ok": False, "error": "請在設定中確認 correction.backend=api 且填入 API Key"}
            minutes_path = save_minutes(minutes, path)
            return {"ok": True, "path": str(minutes_path)}
        except Exception as err:
            return {"ok": False, "error": str(err)}
