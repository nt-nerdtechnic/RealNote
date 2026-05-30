from __future__ import annotations

import argparse
import json
import queue
import signal
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any


SAMPLE_RATE = 16_000


@dataclass
class AudioChunk:
    index: int
    started_at: float
    duration: float
    path: Path
    overlap_seconds: float = 0.0  # 此 chunk 開頭重疊自前一個 chunk 的秒數
    rms: float = 0.0              # 新音訊（不含 overlap）的 RMS 能量
    track: int = 0               # 來源音軌（0=人聲/你, 1=對方）；混音模式恆為 0
    speaker: str | None = None   # 講者標記（分軌模式才有：你/對方）


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Record microphone audio and transcribe it continuously.")
    parser.add_argument("--output-dir", type=Path, default=None, help="Directory for generated files.")
    parser.add_argument(
        "--segment-seconds",
        type=float,
        default=20.0,
        help="Seconds per ASR segment. Shorter segments feel faster but lose more context.",
    )
    parser.add_argument("--input-device", default=None, help="sounddevice input device id or name.")
    parser.add_argument("--list-devices", action="store_true", help="List audio devices and exit.")
    parser.add_argument("--no-summary", action="store_true", help="Skip final LLM meeting minutes generation.")
    return parser.parse_args()


def _console_print(message: str) -> None:
    try:
        from rich.console import Console

        Console().print(message)
    except ModuleNotFoundError:
        print(message)


def _list_devices() -> None:
    import sounddevice as sd

    print(sd.query_devices())


def _new_output_dir(output_dir: Path | None) -> Path:
    if output_dir is not None:
        path = output_dir.expanduser().resolve()
    else:
        path = Path("data/output") / f"stream-{time.strftime('%Y%m%d-%H%M%S')}"
        path = path.resolve()

    (path / "chunks").mkdir(parents=True, exist_ok=True)
    return path


def _empty_transcript() -> dict[str, Any]:
    return {
        "audio_file": "microphone-stream",
        "language": "zh-TW",
        "mode": "near-realtime-stream",
        "segments": [],
    }


def _offset_asr_result(asr_result: dict[str, Any], chunk: AudioChunk) -> list[dict[str, Any]]:
    # 實際音訊起始點（含 overlap 往前移）
    audio_start = chunk.started_at - chunk.overlap_seconds
    overlap_text_parts: list[str] = []
    segments: list[dict[str, Any]] = []

    for item in asr_result.get("chunks", []):
        timestamp = item.get("timestamp") or (None, None)
        start, end = timestamp
        text = (item.get("text") or "").strip()
        if not text:
            continue

        session_start = None if start is None else start + audio_start
        session_end   = None if end   is None else end   + audio_start
        # ASR 偶爾回報超出 WAV 範圍的時間 → 截斷或整段跳過
        max_end = chunk.started_at + chunk.duration
        if session_start is not None and session_start > max_end:
            continue  # phantom segment（幻覺時間戳），整段丟棄
        if session_end is not None and session_end > max_end:
            session_end = max_end

        # overlap 去重：start 落在 overlap 區（< chunk.started_at - 0.02）的 segment
        # 分兩種情況：
        #   1. end 也在 overlap 區（< chunk.started_at + 0.05）→ 前一個 chunk 已處理，丟棄
        #   2. end 延伸進主音訊（橫跨邊界）→ 保留，但將 start 截斷到 chunk.started_at，
        #      避免帶著 overlap 時間戳造成 _enforce_chronological 累積推移
        if (
            chunk.overlap_seconds > 0
            and session_start is not None
            and session_start < chunk.started_at - 0.02
        ):
            if session_end is not None and session_end <= chunk.started_at + 0.05:
                # 完全在 overlap 區 → 前一個 chunk 已見過，視為舊內容
                overlap_text_parts.append(text)
                continue
            else:
                # 橫跨邊界 → 截斷 start，讓時間戳從主音訊起點開始
                session_start = chunk.started_at

        # 若有 overlap 暫存文字，拼接到這段的開頭
        if overlap_text_parts:
            text = "".join(overlap_text_parts) + text
            overlap_text_parts.clear()

        segments.append(
            {
                "start": session_start,
                "end": session_end,
                "speaker": None,
                "text": text,
                "source_chunk": chunk.path.name,
            }
        )

    # overlap 文字沒有後繼段落（邊緣情況）
    # 使用 overlap 區真實的時間範圍，避免 start==end 的零時長 segment
    if overlap_text_parts:
        segments.append(
            {
                "start": audio_start,
                "end": chunk.started_at,
                "speaker": None,
                "text": "".join(overlap_text_parts),
                "source_chunk": chunk.path.name,
            }
        )

    if not segments and asr_result.get("text"):
        segments.append(
            {
                "start": chunk.started_at,
                "end": chunk.started_at + chunk.duration,
                "speaker": None,
                "text": asr_result["text"].strip(),
                "source_chunk": chunk.path.name,
            }
        )

    return segments


def _save_live_transcript(transcript: dict[str, Any], output_dir: Path) -> tuple[Path, Path]:
    from .transcript import transcript_to_text

    json_path = output_dir / "transcript.json"
    txt_path = output_dir / "transcript.txt"

    json_path.write_text(json.dumps(transcript, ensure_ascii=False, indent=2), encoding="utf-8")
    txt_path.write_text(transcript_to_text(transcript), encoding="utf-8")

    return json_path, txt_path


def _record_chunk(
    index: int,
    started_at: float,
    duration: float,
    output_dir: Path,
    input_device: str | None,
) -> AudioChunk:
    import sounddevice as sd
    import soundfile as sf

    frames = int(duration * SAMPLE_RATE)
    path = output_dir / "chunks" / f"chunk_{index:06d}.wav"
    device: int | str | None = None
    if input_device:
        device = int(input_device) if input_device.isdigit() else input_device

    audio = sd.rec(
        frames,
        samplerate=SAMPLE_RATE,
        channels=1,
        dtype="float32",
        device=device,
    )
    sd.wait()
    sf.write(path, audio, SAMPLE_RATE)

    return AudioChunk(index=index, started_at=started_at, duration=duration, path=path)


def _install_stop_handler(stop_event: threading.Event) -> None:
    def handle_stop(signum: int, frame: object) -> None:
        stop_event.set()

    signal.signal(signal.SIGINT, handle_stop)
    signal.signal(signal.SIGTERM, handle_stop)


def _recording_worker(
    chunk_queue: queue.Queue[AudioChunk],
    stop_event: threading.Event,
    output_dir: Path,
    segment_seconds: float,
    input_device: str | None,
    recording_started: float,
) -> None:
    index = 1
    while not stop_event.is_set():
        chunk_started = time.monotonic() - recording_started
        _console_print(f"Recording chunk {index}...")
        chunk = _record_chunk(index, chunk_started, segment_seconds, output_dir, input_device)
        chunk_queue.put(chunk)
        index += 1


def run(
    output_dir: Path | None,
    segment_seconds: float,
    input_device: str | None,
    no_summary: bool,
) -> None:
    if segment_seconds < 5:
        raise ValueError("--segment-seconds should be at least 5 seconds for usable ASR context.")

    from .asr import BreezeAsr
    from .summarizer import save_minutes, summarize_transcript
    from .transcript import transcript_to_text

    path = _new_output_dir(output_dir)
    transcript = _empty_transcript()
    chunk_queue: queue.Queue[AudioChunk] = queue.Queue()
    stop_event = threading.Event()
    _install_stop_handler(stop_event)

    _console_print(f"Output: {path}")
    _console_print("Loading Breeze-ASR model...")
    asr = BreezeAsr()

    _console_print("Recording started. Press Ctrl+C to stop.")
    recording_started = time.monotonic()
    recorder = threading.Thread(
        target=_recording_worker,
        args=(chunk_queue, stop_event, path, segment_seconds, input_device, recording_started),
        daemon=True,
    )
    recorder.start()

    while not stop_event.is_set() or not chunk_queue.empty() or recorder.is_alive():
        try:
            chunk = chunk_queue.get(timeout=0.5)
        except queue.Empty:
            continue

        backlog = chunk_queue.qsize()
        if backlog:
            _console_print(f"ASR backlog: {backlog} chunk(s).")
        _console_print(f"Transcribing {chunk.path.name}...")
        asr_result = asr.transcribe(chunk.path)
        new_segments = _offset_asr_result(asr_result, chunk)
        transcript["segments"].extend(new_segments)
        _save_live_transcript(transcript, path)

        for segment in new_segments:
            _console_print(f"{segment['text']}")

        chunk_queue.task_done()

    _console_print("Recording stopped.")
    _save_live_transcript(transcript, path)

    if no_summary:
        _console_print("Skipped summary generation.")
        return

    _console_print("Generating final meeting minutes with LLM...")
    minutes = summarize_transcript(transcript_to_text(transcript))
    if minutes is None:
        prompt_path = path / "llm_input.txt"
        prompt_path.write_text(transcript_to_text(transcript), encoding="utf-8")
        _console_print("OPENAI_API_KEY is not set. Saved transcript for manual LLM summary instead.")
        _console_print(f"Saved LLM input: {prompt_path}")
        return

    minutes_path = save_minutes(minutes, path)
    _console_print(f"Saved meeting minutes: {minutes_path}")


def main() -> None:
    args = parse_args()

    if args.list_devices:
        _list_devices()
        return

    try:
        run(args.output_dir, args.segment_seconds, args.input_device, args.no_summary)
    except (FileNotFoundError, ModuleNotFoundError, ValueError) as exc:
        _console_print(f"Error: {exc}")
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
