from __future__ import annotations

import atexit
import os
import platform
import signal
import weakref
from pathlib import Path
from typing import Any


def _is_apple_silicon() -> bool:
    return platform.system() == "Darwin" and platform.machine() == "arm64"


# ---------------------------------------------------------------------------
# 全域 MLX ProcessPoolExecutor 管理（避免孤兒子程序）
# ---------------------------------------------------------------------------
# 每個 FasterWhisperAsr instance 建立的 PPE 都 weakref 進這個 set。
# atexit + SIGTERM 觸發時統一 shutdown，並 SIGKILL 殘存的 worker subprocess。
_ALL_EXECUTORS: weakref.WeakSet = weakref.WeakSet()
_ATEXIT_REGISTERED = False


def _register_executor(executor) -> None:
    """註冊 executor 到全域追蹤，並（首次）掛上 atexit / signal handler。"""
    global _ATEXIT_REGISTERED
    _ALL_EXECUTORS.add(executor)
    if not _ATEXIT_REGISTERED:
        atexit.register(_shutdown_all_executors)
        # SIGTERM / SIGINT 也要清。signal 只能在 main thread 註冊，try 包起避免 worker thread 拋錯。
        try:
            signal.signal(signal.SIGTERM, _signal_cleanup)
            signal.signal(signal.SIGINT, _signal_cleanup)
        except (ValueError, OSError):
            pass
        _ATEXIT_REGISTERED = True


def _hard_kill_executor(executor) -> None:
    """先 graceful shutdown，再對殘存的 worker subprocess 強殺。"""
    # 必須在 shutdown() 前取 PIDs：shutdown(wait=False) 會清空 _processes dict
    procs = getattr(executor, "_processes", None) or {}
    pids = [getattr(p, "pid", None) for p in list(procs.values())]
    try:
        executor.shutdown(wait=False, cancel_futures=True)
    except Exception:
        pass
    for pid in pids:
        if pid:
            try:
                os.kill(pid, signal.SIGKILL)
            except OSError:
                pass


def _shutdown_all_executors() -> None:
    """atexit 入口：把所有追蹤中的 PPE 都強制清掉。"""
    for exe in list(_ALL_EXECUTORS):
        _hard_kill_executor(exe)


def _signal_cleanup(signum, frame) -> None:
    _shutdown_all_executors()
    # 把原本的 default behaviour（exit）繼續做完
    signal.signal(signum, signal.SIG_DFL)
    os.kill(os.getpid(), signum)


# ---------------------------------------------------------------------------
# Module-level worker — must be top-level (picklable) for ProcessPoolExecutor
# ---------------------------------------------------------------------------
def _mlx_transcribe_worker(
    audio_path_str: str,
    mlx_repo: str,
    lang: str | None,
    initial_prompt: str | None,
    no_speech_threshold: float = 0.9,
) -> dict[str, Any]:
    """Run mlx_whisper in a subprocess to isolate Metal GPU crashes from the backend process."""
    import mlx_whisper  # type: ignore

    result = mlx_whisper.transcribe(
        audio_path_str,
        path_or_hf_repo=mlx_repo,
        language=lang,
        initial_prompt=initial_prompt,
        temperature=0.0,
        condition_on_previous_text=False,
        no_speech_threshold=no_speech_threshold,
    )
    # Return only picklable primitives (strip any non-serialisable objects)
    segments_out = []
    for seg in result.get("segments") or []:
        segments_out.append({
            "start": float(seg["start"]),
            "end": float(seg["end"]),
            "text": str(seg.get("text") or ""),
            # no_speech_prob：Whisper 對「此段無語音」的信心（0=有語音, 1=無語音）
            # 保留供呼叫端做二次過濾
            "no_speech_prob": float(seg.get("no_speech_prob") or 0.0),
        })
    return {
        "segments": segments_out,
        "language": result.get("language"),
    }


class FasterWhisperAsr:
    def __init__(
        self,
        model_size: str | None = None,
        device: str | None = None,
        compute_type: str | None = None,
    ) -> None:
        self.model_size = model_size or os.getenv("LIVE_ASR_MODEL", "medium")
        self.device = device or os.getenv("LIVE_ASR_DEVICE", "cpu")
        self.compute_type = compute_type or os.getenv("LIVE_ASR_COMPUTE_TYPE", "int8")
        self._use_mlx = _is_apple_silicon() and os.getenv("LIVE_ASR_BACKEND", "mlx") == "mlx"

        if self._use_mlx:
            default_repo = f"mlx-community/whisper-{self.model_size}-mlx-q4"
            self._mlx_repo = os.getenv("LIVE_ASR_MLX_REPO", default_repo)
            # ProcessPoolExecutor isolates Metal GPU crashes to the worker process
            import concurrent.futures
            self._mlx_executor: concurrent.futures.ProcessPoolExecutor = (
                concurrent.futures.ProcessPoolExecutor(max_workers=1)
            )
            # 註冊到全域清單，方便 backend shutdown / BrokenProcessPool 時統一清掉
            _register_executor(self._mlx_executor)
            # 不在 __init__ 預熱；由 stream_service._preload_asr_bg 明確呼叫 ensure_warm()
        else:
            from faster_whisper import WhisperModel
            self.model = WhisperModel(
                self.model_size,
                device=self.device,
                compute_type=self.compute_type,
            )

    @staticmethod
    def _to_traditional(text: str) -> str:
        """用 OpenCC s2twp 將簡體轉台灣繁體（含詞彙對應）。"""
        try:
            import opencc
            return opencc.OpenCC("s2twp").convert(text)
        except Exception:
            return text


    def ensure_warm(self) -> None:
        """由外部（背景 thread）呼叫，同步預熱 subprocess 直到模型載入完成。"""
        self._warmup_mlx()

    def _warmup_mlx(self) -> None:
        """送靜音 chunk 預熱 subprocess，讓模型在第一個真實 chunk 到來前完成載入。"""
        import concurrent.futures
        import struct
        import tempfile
        import wave as wave_mod

        # 建立 0.5s 靜音 WAV（16kHz, mono, 16-bit）
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tf:
            tmp_path = tf.name
        try:
            with wave_mod.open(tmp_path, "w") as wf:
                wf.setnchannels(1)
                wf.setsampwidth(2)
                wf.setframerate(16000)
                wf.writeframes(struct.pack("<" + "h" * 8000, *([0] * 8000)))
            lang = os.getenv("LIVE_ASR_LANGUAGE", "zh") or None
            future = self._mlx_executor.submit(
                _mlx_transcribe_worker, tmp_path, self._mlx_repo, lang, None
            )
            future.result(timeout=60)
        except Exception:
            pass
        finally:
            try:
                os.unlink(tmp_path)
            except Exception:
                pass

    def transcribe(
        self,
        audio_path: Path,
        initial_prompt: str | None = None,
        language: str | None = None,
        settings: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """settings 用於覆寫硬編碼閾值（no_speech_threshold、nsp 灰色長度、speed hard limit、
        repeat_in_chunk_threshold）。None → 使用 fallback 預設。"""
        if self._use_mlx:
            return self._transcribe_mlx(audio_path, initial_prompt=initial_prompt, language=language, settings=settings)
        return self._transcribe_faster_whisper(audio_path, initial_prompt=initial_prompt, language=language, settings=settings)

    # ------------------------------------------------------------------
    # MLX 路徑（Apple Silicon，Metal GPU 加速，subprocess 隔離 crash）
    # ------------------------------------------------------------------
    def _transcribe_mlx(
        self,
        audio_path: Path,
        initial_prompt: str | None = None,
        language: str | None = None,
        settings: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        import concurrent.futures

        # language 解析優先序：
        #   - 'auto' / '' → None（Whisper 自動偵測；多語混音必須走這條，否則強制目標語會誘發迴圈幻覺）
        #   - 其他字串 → 直接使用
        #   - None → 退回環境變數（CLI / 暖機等沒有呼叫端的情境）
        if language in ("auto", ""):
            lang = None
        elif language is not None:
            lang = language
        else:
            lang = os.getenv("LIVE_ASR_LANGUAGE", "zh") or None
        # 動態 initial_prompt 優先；無則退回環境變數的靜態值
        prompt = initial_prompt or os.getenv("LIVE_ASR_INITIAL_PROMPT", "") or None

        # 從 settings 取參數（None 時用預設）
        _s = settings or {}
        _nsp_hard = _s.get("asr.no_speech_threshold", 0.9)
        _nsp_soft_min_len = _s.get("asr.nsp_gray_min_len", 4)
        _rate_hard = _s.get("rate.hard_limit", 50.0)
        _repeat_threshold = _s.get("rate.repeat_in_chunk_threshold", 3)

        try:
            future = self._mlx_executor.submit(
                _mlx_transcribe_worker,
                str(audio_path),
                self._mlx_repo,
                lang,
                prompt,
                _nsp_hard,
            )
            result = future.result(timeout=60)
        except concurrent.futures.BrokenProcessPool:
            # Worker crashed → 強殺舊 executor 後重建
            _hard_kill_executor(self._mlx_executor)
            self._mlx_executor = concurrent.futures.ProcessPoolExecutor(max_workers=1)
            _register_executor(self._mlx_executor)
            raise RuntimeError("MLX worker crashed (Metal GPU timeout), executor restarted")
        except concurrent.futures.TimeoutError:
            # Worker 卡死 → 不能讓它繼續吃資源
            _hard_kill_executor(self._mlx_executor)
            self._mlx_executor = concurrent.futures.ProcessPoolExecutor(max_workers=1)
            _register_executor(self._mlx_executor)
            raise RuntimeError("MLX transcription timed out (60s), executor restarted")

        chunks: list[dict[str, Any]] = []
        text_parts: list[str] = []
        duration: float | None = None

        # no_speech_threshold 已在 mlx_whisper 內部過濾一次；
        # 此處再做第二層彈性三區段過濾（所有閾值皆從 settings 來）：
        #   nsp > _nsp_hard               → 直接丟
        #   0.5 < nsp ≤ _nsp_hard 且 文字 < _nsp_soft_min_len 字 → 丟
        #   nsp ≤ 0.5                     → 直接通過

        for seg in result.get("segments") or []:
            nsp = seg.get("no_speech_prob", 0.0)
            if nsp > _nsp_hard:
                continue
            text = self._to_traditional(str(seg.get("text") or "").strip())
            if not text:
                continue
            # 灰色地帶：極短輸出通常是噪音誘發的假語音
            if nsp > 0.5 and len(text) < _nsp_soft_min_len:
                continue
            # 說話速率絕對上限（安全網）；精細動態閾值在 stream_service 層執行
            _seg_dur = seg["end"] - seg["start"]
            if _seg_dur > 0 and len(text) / _seg_dur > _rate_hard:
                continue
            chunks.append({
                "timestamp": (seg["start"], seg["end"]),
                "text": text,
            })
            text_parts.append(text)
            duration = seg["end"]

        # 同一 chunk 結果內有 N+ 個相同 segment → 幻覺迴圈，整批丟棄
        from collections import Counter
        text_counts = Counter(c["text"] for c in chunks)
        if any(count >= _repeat_threshold for count in text_counts.values()):
            return {
                "text": "",
                "chunks": [],
                "language": result.get("language"),
                "duration": None,
            }

        return {
            "text": "".join(text_parts),
            "chunks": chunks,
            "language": result.get("language"),
            "duration": duration,
        }

    # ------------------------------------------------------------------
    # faster-whisper 路徑（非 Apple Silicon 或強制 CPU）
    # ------------------------------------------------------------------
    def _transcribe_faster_whisper(
        self,
        audio_path: Path,
        initial_prompt: str | None = None,
        language: str | None = None,
        settings: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        # 與 MLX 路徑同語義：'auto' / '' → None 給 faster-whisper 自動偵測
        if language in ("auto", ""):
            language = None
        elif language is None:
            lang_env = os.getenv("LIVE_ASR_LANGUAGE", "zh")
            language = lang_env or None

        prompt = initial_prompt or os.getenv(
            "LIVE_ASR_INITIAL_PROMPT",
            "以下是台灣繁體中文的會議記錄。",
        )

        _s = settings or {}
        _nsp_hard = _s.get("asr.no_speech_threshold", 0.9)
        _nsp_soft_min_len = _s.get("asr.nsp_gray_min_len", 4)

        segments, info = self.model.transcribe(
            str(audio_path),
            language=language,
            beam_size=int(os.getenv("LIVE_ASR_BEAM_SIZE", "1")),
            vad_filter=True,
            condition_on_previous_text=False,
            without_timestamps=False,
            initial_prompt=prompt if prompt else None,
        )

        chunks: list[dict[str, Any]] = []
        text_parts: list[str] = []
        for segment in segments:
            nsp = getattr(segment, "no_speech_prob", 0.0)
            if nsp > _nsp_hard:
                continue
            text = self._to_traditional(segment.text.strip())
            if not text:
                continue
            if nsp > 0.5 and len(text) < _nsp_soft_min_len:
                continue
            chunks.append({
                "timestamp": (float(segment.start), float(segment.end)),
                "text": text,
            })
            text_parts.append(text)

        return {
            "text": "".join(text_parts),
            "chunks": chunks,
            "language": getattr(info, "language", None),
            "duration": getattr(info, "duration", None),
        }
