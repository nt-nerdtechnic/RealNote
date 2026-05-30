"""F28: Smart auto-fix（F17 標的 issues 機械式修復）

延伸 F18（迴圈/重複），加上 too_short / too_long 自動處理：

修復策略（保守、可逆）：
1. **too_short merge**：相鄰兩段都 < 4 字、gap < 0.5s → 合併文字、保留首段 start、後段 end
2. **too_long split**：text > 200 字 → 按「，。！？」切，按比例分配時間戳

不修復（風險高，留人工 / LLM）：
- speed_anomaly：可能是真實快講
- large_gap：資料不存在無法補
- hallucination_loop：F18 已處理

CLI:
  python -m meeting_minutes_backend.smart_autofix <session_dir>
  → 輸出 <session_dir>/transcript_autofixed.json + autofix_report.md
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any


def _is_mostly_english(text: str) -> bool:
    if not text:
        return False
    letters = sum(1 for ch in text if ch.isascii() and ch.isalpha())
    return letters / len(text) > 0.5


def merge_too_short(segments: list[dict[str, Any]],
                    min_chars: int = 4,
                    max_gap_secs: float = 0.5) -> tuple[list[dict[str, Any]], list[dict]]:
    """連續 too_short 合併。回傳 (新 segments, 動作 log)。"""
    out: list[dict[str, Any]] = []
    actions: list[dict] = []
    for seg in segments:
        text = (seg.get("text") or "").strip()
        if not text:
            out.append(seg)
            continue
        if out:
            prev = out[-1]
            prev_text = (prev.get("text") or "").strip()
            gap = (seg.get("start") or 0) - (prev.get("end") or 0)
            if (
                len(prev_text) < min_chars
                and len(text) < min_chars
                and 0 <= gap <= max_gap_secs
            ):
                merged_text = prev_text + text
                merged_line_ids = sorted({prev.get("line_id"), seg.get("line_id")} - {None})
                new_seg = {
                    **prev,
                    "text": merged_text,
                    "end": seg.get("end"),
                    "merged_line_ids": merged_line_ids,
                }
                out[-1] = new_seg
                actions.append({
                    "action": "merge_too_short",
                    "from_line_ids": merged_line_ids,
                    "merged_text": merged_text[:60],
                    "gap": round(gap, 2),
                })
                continue
        out.append(seg)
    return out, actions


def split_too_long(segments: list[dict[str, Any]], max_chars: int = 200) -> tuple[list[dict[str, Any]], list[dict]]:
    """text > max_chars 按標點切，時間戳按比例分配。"""
    out: list[dict[str, Any]] = []
    actions: list[dict] = []
    for seg in segments:
        text = (seg.get("text") or "").strip()
        if len(text) <= max_chars:
            out.append(seg)
            continue
        # 按 ，。！？ 切
        parts = re.split(r"([，。！？])", text)
        # 重組成 (chunk_with_trailing_punct, ...)
        chunks: list[str] = []
        buf = ""
        for p in parts:
            buf += p
            if p in "，。！？" and len(buf) >= max_chars // 4:
                chunks.append(buf.strip())
                buf = ""
        if buf.strip():
            chunks.append(buf.strip())
        if len(chunks) <= 1:
            out.append(seg)
            continue
        start = seg.get("start") or 0
        end = seg.get("end") or 0
        total_chars = sum(len(c) for c in chunks) or 1
        cur_t = start
        new_segs = []
        for i, c in enumerate(chunks):
            ratio = len(c) / total_chars
            sub_end = cur_t + (end - start) * ratio
            new_seg = {
                **seg,
                "text": c,
                "start": cur_t,
                "end": sub_end,
                "split_from_line_id": seg.get("line_id"),
                "split_index": i,
            }
            new_segs.append(new_seg)
            cur_t = sub_end
        out.extend(new_segs)
        actions.append({
            "action": "split_too_long",
            "original_line_id": seg.get("line_id"),
            "original_chars": len(text),
            "n_parts": len(chunks),
            "parts": [c[:40] + ("…" if len(c) > 40 else "") for c in chunks],
        })
    return out, actions


def autofix(transcript: dict[str, Any]) -> tuple[dict[str, Any], list[dict]]:
    segments = transcript.get("segments", [])
    if not segments:
        return transcript, []
    segments1, actions1 = merge_too_short(segments)
    segments2, actions2 = split_too_long(segments1)
    return {**transcript, "segments": segments2}, actions1 + actions2


def format_report(session: str, before_n: int, after_n: int, actions: list[dict]) -> str:
    lines = [f"# 🔧 Smart Auto-fix 報告 — {session}", ""]
    lines.append(f"- 原 segments：{before_n}")
    lines.append(f"- 清理後：{after_n}")
    lines.append(f"- 動作數：**{len(actions)}**")
    lines.append("")
    if not actions:
        lines.append("✅ 無自動修復項")
        return "\n".join(lines)

    by_action: dict[str, list] = {}
    for a in actions:
        by_action.setdefault(a["action"], []).append(a)
    for kind, items in by_action.items():
        lines.append(f"## {kind}（{len(items)}）")
        lines.append("")
        for it in items[:10]:
            if kind == "merge_too_short":
                lines.append(f"- L{it['from_line_ids']}: `{it['merged_text']}` (gap={it['gap']}s)")
            elif kind == "split_too_long":
                lines.append(f"- L{it['original_line_id']} ({it['original_chars']} 字 → {it['n_parts']} 份)")
                for p in it["parts"][:3]:
                    lines.append(f"  - `{p}`")
        if len(items) > 10:
            lines.append(f"- _…還有 {len(items) - 10} 項_")
        lines.append("")
    return "\n".join(lines)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("session_dir")
    p.add_argument("--out-dir", default=None, help="輸出目錄（預設 = session_dir）")
    args = p.parse_args()

    session_dir = Path(args.session_dir).expanduser().resolve()
    t_path = session_dir / "transcript.json"
    if not t_path.exists():
        print("no transcript.json", file=sys.stderr)
        sys.exit(1)
    out_dir = Path(args.out_dir).expanduser().resolve() if args.out_dir else session_dir
    out_dir.mkdir(parents=True, exist_ok=True)

    transcript = json.loads(t_path.read_text(encoding="utf-8"))
    before_n = len(transcript.get("segments", []))
    fixed, actions = autofix(transcript)
    after_n = len(fixed["segments"])

    fixed_path = out_dir / "transcript_autofixed.json"
    report_path = out_dir / "autofix_report.md"
    fixed_path.write_text(json.dumps(fixed, indent=2, ensure_ascii=False), encoding="utf-8")
    report_path.write_text(format_report(session_dir.name, before_n, after_n, actions), encoding="utf-8")

    print(json.dumps({
        "session": session_dir.name,
        "before": before_n,
        "after": after_n,
        "actions": len(actions),
        "fixed_path": str(fixed_path),
        "report_path": str(report_path),
    }, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
