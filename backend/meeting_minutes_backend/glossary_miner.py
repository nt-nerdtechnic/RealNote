"""術語自學器：從 correction_log.jsonl 萃取 ASR 常見誤聽詞，自動更新 glossary.txt。

演算法：
1. 對每筆校正，比對原文 vs 校正後文字的差異
2. 找出「校正後才出現、原文沒有」的 2-8 字詞彙
3. 跨 session 統計頻率，過濾常見虛詞與標點
4. 高頻候選詞 → 附加到 glossary.txt（去重）

CLI 用法：
    python -m meeting_minutes_backend.glossary_miner [--output-dir DIR] [--min-count N] [--dry-run]
    python -m meeting_minutes_backend.glossary_miner data/output/session1 data/output/session2

    不給 session 路徑時，自動掃描 data/output/ 下所有有 correction_log.jsonl 的目錄。
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from pathlib import Path

_BOUNDARY = set("的了在有是和與或也都就還不但而且之其")
_MIN_LEN = 2
_MAX_LEN = 10


def _extract_tokens(text: str) -> list[str]:
    """從文字中提取候選詞彙（2-8 字的中文片段）。"""
    parts = re.split(r"[\s，。！？、；：「」『』（）【】\-—…]+", text)
    tokens: list[str] = []
    for p in parts:
        p = p.strip()
        if not p:
            continue
        for length in range(_MIN_LEN, min(_MAX_LEN + 1, len(p) + 1)):
            for start in range(len(p) - length + 1):
                tok = p[start:start + length]
                if re.match(r'^[\d]+$', tok):
                    continue
                if all(c in _BOUNDARY or c in '，。！？、；：' for c in tok):
                    continue
                tokens.append(tok)
    return tokens


def _diff_tokens(orig: str, corrected: str) -> list[str]:
    orig_tokens = set(_extract_tokens(orig))
    corr_tokens = _extract_tokens(corrected)
    return [t for t in corr_tokens if t not in orig_tokens and len(t) >= _MIN_LEN]


def mine_session(log_path: Path) -> Counter:
    counter: Counter = Counter()
    try:
        lines = [json.loads(l) for l in log_path.read_text(encoding="utf-8").splitlines() if l.strip()]
    except (OSError, json.JSONDecodeError):
        return counter
    for entry in lines:
        corrections = entry.get("corrections") or []
        batch_by_lid = {b["line_id"]: b["text"] for b in entry.get("batch", [])}
        for corr in corrections:
            line_ids = corr.get("line_ids", [])
            orig = "".join(batch_by_lid.get(lid, "") for lid in line_ids)
            corrected = corr.get("text", "")
            if not orig or not corrected or orig == corrected:
                continue
            counter.update(_diff_tokens(orig, corrected))
    return counter


def load_glossary(path: Path) -> set[str]:
    if not path.exists():
        return set()
    result = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            result.add(line)
    return result


def append_to_glossary(path: Path, terms: list[str]) -> None:
    block = "\n# --- 自動萃取（glossary_miner）---\n" + "\n".join(terms) + "\n"
    with open(path, "a", encoding="utf-8") as f:
        f.write(block)


def find_sessions(base_dir: Path) -> list[Path]:
    return sorted(base_dir.glob("*/correction_log.jsonl"))


def main() -> None:
    ap = argparse.ArgumentParser(description="從 correction_log 萃取術語並更新 glossary.txt")
    ap.add_argument("sessions", nargs="*", help="指定 session 目錄（不填 = 自動掃描 data/output/）")
    ap.add_argument("--output-dir", default="data", help="glossary.txt 所在目錄（預設 data/）")
    ap.add_argument("--min-count", type=int, default=2, help="最低出現次數才納入（預設 2）")
    ap.add_argument("--top", type=int, default=30, help="最多新增幾個術語（預設 30）")
    ap.add_argument("--dry-run", action="store_true", help="只印候選，不寫入 glossary.txt")
    args = ap.parse_args()

    log_paths: list[Path] = []
    if args.sessions:
        for s in args.sessions:
            p = Path(s)
            candidate = p / "correction_log.jsonl"
            if candidate.exists():
                log_paths.append(candidate)
            elif p.name == "correction_log.jsonl" and p.exists():
                log_paths.append(p)
            else:
                print(f"[warn] 找不到 {candidate}", file=sys.stderr)
    else:
        base = Path("data/output")
        if not base.exists():
            print(f"[error] {base} 不存在，請指定 session 路徑", file=sys.stderr)
            sys.exit(1)
        log_paths = find_sessions(base)

    if not log_paths:
        print("[error] 沒有找到任何 correction_log.jsonl", file=sys.stderr)
        sys.exit(1)

    print(f"掃描 {len(log_paths)} 個 session...")
    total: Counter = Counter()
    for lp in log_paths:
        c = mine_session(lp)
        total.update(c)
        print(f"  {lp.parent.name}: {len(c)} 個候選詞")

    glossary_path = Path(args.output_dir) / "glossary.txt"
    existing = load_glossary(glossary_path)
    print(f"\n現有術語表：{len(existing)} 個詞")

    filtered = [
        (term, count) for term, count in total.most_common()
        if count >= args.min_count
        and term not in existing
        and len(term) >= _MIN_LEN
        and len(term) <= _MAX_LEN
        and not (len(term) <= 2 and re.match(r'^[一-鿿]{1,2}$', term))
        and term[-1] not in _BOUNDARY
        and term[0] not in _BOUNDARY
        and not re.search(r'[，。！？、；：\.\。]', term)
        and re.search(r'[一-鿿A-Za-z]', term)
        and not re.search(r'(.)\1{2,}', term)
    ]

    term_set = {t for t, _ in filtered}
    candidates = [
        (term, count) for term, count in filtered
        if not any(
            term != longer and term in longer
            for longer in term_set
            if total[longer] >= count * 0.6
        )
    ][:args.top]

    if not candidates:
        print("\n沒有新術語候選（所有詞已在 glossary 或出現次數不足）")
        return

    print(f"\n新術語候選（出現次數 ≥ {args.min_count}）：")
    print(f"{'術語':<20} {'次數':>5}")
    print("-" * 28)
    for term, count in candidates:
        print(f"{term:<20} {count:>5}")

    new_terms = [term for term, _ in candidates]

    if args.dry_run:
        print("\n[dry-run] 未寫入 glossary.txt")
        return

    append_to_glossary(glossary_path, new_terms)
    print(f"\n✅ 已將 {len(new_terms)} 個新術語附加到 {glossary_path}")


if __name__ == "__main__":
    main()
