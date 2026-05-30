from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def build_transcript(audio_file: Path, asr_result: dict[str, Any]) -> dict[str, Any]:
    segments = []

    for chunk in asr_result.get("chunks", []):
        timestamp = chunk.get("timestamp") or (None, None)
        start, end = timestamp
        text = (chunk.get("text") or "").strip()

        if not text:
            continue

        segments.append(
            {
                "start": start,
                "end": end,
                "speaker": None,
                "text": text,
            }
        )

    if not segments and asr_result.get("text"):
        segments.append(
            {
                "start": None,
                "end": None,
                "speaker": None,
                "text": asr_result["text"].strip(),
            }
        )

    return {
        "audio_file": str(audio_file),
        "language": "zh-TW",
        "segments": segments,
    }


def format_timestamp(seconds: float | None) -> str:
    if seconds is None:
        return "--:--.--"

    total_minutes = int(seconds // 60)
    secs = seconds - total_minutes * 60  # 保留小數，顯示 0.1s 精度
    hours = total_minutes // 60
    minutes = total_minutes % 60

    if hours:
        return f"{hours:02d}:{minutes:02d}:{secs:04.1f}"
    return f"{minutes:02d}:{secs:04.1f}"


def transcript_to_text(transcript: dict[str, Any]) -> str:
    lines = []
    for segment in transcript["segments"]:
        start = format_timestamp(segment["start"])
        end = format_timestamp(segment["end"])
        speaker = segment["speaker"] or "Speaker"
        lines.append(f"[{start} - {end}] {speaker}: {segment['text']}")

    return "\n".join(lines) + "\n"


def save_transcript(transcript: dict[str, Any], output_dir: Path) -> tuple[Path, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)

    json_path = output_dir / "transcript.json"
    txt_path = output_dir / "transcript.txt"

    json_path.write_text(
        json.dumps(transcript, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    txt_path.write_text(transcript_to_text(transcript), encoding="utf-8")

    return json_path, txt_path
