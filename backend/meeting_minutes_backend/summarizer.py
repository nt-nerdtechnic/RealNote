from __future__ import annotations

import json
import urllib.request
from pathlib import Path
from typing import Any


SYSTEM_PROMPT = """你是專業摘要助理，負責將語音辨識逐字稿整理成結構化摘要。
內容可能是會議、訪談、新聞節目、課程或任何口語錄音，請依實際內容填寫。

規則：
- 語音辨識可能有錯字、口頭禪、重複詞，請自行修正後再整理
- 不捏造逐字稿中未提及的人名、日期、數字
- 每個區塊盡量從逐字稿中找出對應資訊填入；確實沒有才填「無」
- 不加警告、不加免責聲明、不評論逐字稿品質
- 只輸出以下固定格式，標題和符號不可更改，不加其他內容

輸出格式：

## 主題
（一句話說明這段錄音的核心主題）

## 重點摘要
- （重點一）
- （重點二）
- …

## 決議 / 結論
- （若有明確決議或結論則列出；否則填「無」）

## 代辦事項
| 事項 | 負責人 | 備註 |
|------|--------|------|
| （若有則填；否則整列留空） | | |

## 待確認事項
- （需後續查證或確認的內容；否則填「無」）
"""


def summarize_transcript(transcript_text: str, config: dict[str, Any]) -> str | None:
    """呼叫 correction 設定的 OpenAI-compatible API 產生會議記錄摘要。

    僅在 correction.backend=api 且 api_key 有值時執行；否則回 None。
    """
    if config.get("correction.backend") != "api":
        return None
    api_key = config.get("correction.api_key", "")
    if not api_key:
        return None

    base_url = config.get("correction.api_base_url", "https://api.openai.com/v1").rstrip("/")
    model = config.get("correction.api_model", "gpt-4o-mini")
    timeout = float(config.get("correction.timeout_seconds", 60.0))

    body = json.dumps({
        "model": model,
        "max_tokens": 2048,
        "system": SYSTEM_PROMPT,
        "messages": [{"role": "user", "content": f"以下為逐字稿內容：\n\n{transcript_text}"}],
    }).encode("utf-8")

    req = urllib.request.Request(
        f"{base_url}/messages",
        data=body,
        headers={
            "content-type": "application/json",
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        result = json.loads(resp.read().decode("utf-8"))
    for block in (result.get("content") or []):
        if block.get("type") == "text":
            return block["text"]
    return None


def save_minutes(minutes: str, output_dir: Path) -> Path:
    path = output_dir / "minutes.md"
    path.write_text(minutes, encoding="utf-8")
    return path
