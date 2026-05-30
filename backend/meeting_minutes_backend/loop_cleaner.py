"""F18: 自動清理 ASR 迴圈幻覺片段

讀 transcript.json，偵測 + 移除：
1. 含異常高頻 n-gram 的 segments（F16/F17 邏輯：n-gram > 5x median）
2. 連續重複的 segment（保留首個）

輸出：
- transcript_cleaned.json（移除壞 segments，line_id 保留原值）
- cleanup_report.md（每筆被移除原因）

不修改原檔，只輸出新檔。
CLI:
  python -m meeting_minutes_backend.loop_cleaner <session_dir>
  python -m meeting_minutes_backend.loop_cleaner <session_dir> --out <dir>
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from pathlib import Path
from typing import Any


def find_loop_ngrams(segments: list[dict], min_size: int = 3, max_size: int = 5) -> set[str]:
    """找異常高頻 n-gram (> 5x median, 至少 ≥ 20 次)。"""
    counter: Counter = Counter()
    for s in segments:
        text = s.get("text") or ""
        for block in re.findall(r"[一-鿿]+", text):
            for size in range(min_size, max_size + 1):
                for i in range(len(block) - size + 1):
                    counter[block[i : i + size]] += 1
    if len(counter) < 20:
        return set()
    counts = sorted(counter.values())
    median = counts[len(counts) // 2]
    threshold = max(median * 5, 20)
    return {t for t, c in counter.items() if c > threshold}


def segment_is_looping(text: str, loop_ngrams: set[str], min_hits: int = 3) -> bool:
    """segment text 中包含 loop n-gram ≥ min_hits 次（重複出現）→ 視為壞段。"""
    if not loop_ngrams:
        return False
    for ng in loop_ngrams:
        if text.count(ng) >= min_hits:
            return True
    return False


def clean_transcript(transcript: dict[str, Any]) -> tuple[dict[str, Any], list[dict]]:
    """回傳 (清潔版 transcript, 被移除清單)。"""
    segments = transcript.get("segments", [])
    loop_ngrams = find_loop_ngrams(segments)
    cleaned: list[dict] = []
    removed: list[dict] = []
    prev_text: str | None = None
    consec_count = 0

    for s in segments:
        text = (s.get("text") or "").strip()
        if not text:
            cleaned.append(s)
            prev_text = None
            consec_count = 0
            continue

        # 1) 迴圈幻覺：含異常 n-gram 重複出現
        if segment_is_looping(text, loop_ngrams):
            removed.append({
                "line_id": s.get("line_id"),
                "reason": "hallucination_loop",
                "text": text[:80],
                "matched_ngrams": sorted(ng for ng in loop_ngrams if text.count(ng) >= 3)[:5],
            })
            prev_text = None
            consec_count = 0
            continue

        # 2) 連續重複：保留首個，後續刪
        if text == prev_text:
            consec_count += 1
            if consec_count >= 2:  # 第 3 個以上重複
                removed.append({
                    "line_id": s.get("line_id"),
                    "reason": "consecutive_duplicate",
                    "text": text[:80],
                    "duplicate_of": cleaned[-(consec_count + 1)].get("line_id") if cleaned else None,
                })
                continue
        else:
            consec_count = 0

        prev_text = text
        cleaned.append(s)

    return {**transcript, "segments": cleaned}, removed


def format_report(session_name: str, before_n: int, after_n: int, removed: list[dict]) -> str:
    lines = [f"# 🧹 Loop 清理報告 — {session_name}", ""]
    lines.append(f"- 原 segments：{before_n}")
    lines.append(f"- 清理後：{after_n}")
    lines.append(f"- 移除：**{len(removed)}** ({len(removed)/before_n*100:.1f}%)")
    lines.append("")
    if not removed:
        lines.append("✅ 無壞段需清理")
        return "\n".join(lines)

    # 分類
    by_reason: dict[str, list[dict]] = {}
    for r in removed:
        by_reason.setdefault(r["reason"], []).append(r)

    for reason, items in by_reason.items():
        lines.append(f"## {reason}（{len(items)} 個）")
        lines.append("")
        for r in items[:15]:
            line_ref = f"L{r['line_id']}"
            if reason == "hallucination_loop":
                ngs = ", ".join(f"`{n}`" for n in r.get("matched_ngrams", []))
                lines.append(f"- {line_ref}: `{r['text']}` （含 {ngs}）")
            elif reason == "consecutive_duplicate":
                dup_of = f"dup of L{r['duplicate_of']}" if r.get("duplicate_of") else ""
                lines.append(f"- {line_ref}: `{r['text']}` ({dup_of})")
            else:
                lines.append(f"- {line_ref}: `{r['text']}`")
        if len(items) > 15:
            lines.append(f"- …還有 {len(items) - 15} 個")
        lines.append("")
    return "\n".join(lines)


def clean_session(session_dir: Path, out_dir: Path | None = None) -> dict[str, Any]:
    transcript_path = session_dir / "transcript.json"
    if not transcript_path.exists():
        return {"error": "no transcript.json"}
    transcript = json.loads(transcript_path.read_text(encoding="utf-8"))
    before_n = len(transcript.get("segments", []))
    cleaned_transcript, removed = clean_transcript(transcript)
    after_n = len(cleaned_transcript["segments"])

    if out_dir is None:
        out_dir = session_dir
    out_dir.mkdir(parents=True, exist_ok=True)

    cleaned_path = out_dir / "transcript_cleaned.json"
    report_path = out_dir / "cleanup_report.md"
    cleaned_path.write_text(
        json.dumps(cleaned_transcript, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    report_path.write_text(
        format_report(session_dir.name, before_n, after_n, removed),
        encoding="utf-8",
    )

    return {
        "session": session_dir.name,
        "before": before_n,
        "after": after_n,
        "removed": len(removed),
        "cleaned_path": str(cleaned_path),
        "report_path": str(report_path),
    }


def main():
    p = argparse.ArgumentParser()
    p.add_argument("session_dir")
    p.add_argument("--out", default=None, help="輸出目錄（預設 = session_dir）")
    args = p.parse_args()

    session_dir = Path(args.session_dir).expanduser().resolve()
    if not session_dir.is_dir():
        print(f"not a directory: {session_dir}", file=sys.stderr)
        sys.exit(1)

    out_dir = Path(args.out).expanduser().resolve() if args.out else session_dir
    result = clean_session(session_dir, out_dir)
    print(json.dumps(result, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
