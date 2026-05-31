"""LLM 即時校正 worker（llama.cpp 內嵌版）。

職責：
- 接收即時的 ASR segments（含 line_id + text）
- 累積到滿 batch 或閒置觸發
- 用 llama-cpp-python 跑本地 GGUF 模型校正錯字、術語、人名
- 模型用 huggingface_hub 從 HF 下載到本機快取（與 Whisper 共用 ~/.cache/huggingface/）
- emit `transcript.correction { line_id, text }` 事件給前端
- 失敗時不阻塞錄音；保留原文

設計原則：
- enabled=0 → worker 完全不啟動，零開銷
- 模型懶下載：首次啟用才開始下載（~1.6GB for gemma2-2b Q4_K_M）
- LLM 失敗 / timeout / 解析失敗 → log warning，不影響後續批次
- 與 Breeze 終版校正並存：Breeze 在錄音停止後跑，以 line_id 覆蓋 LLM 校正版
"""
from __future__ import annotations

import hashlib
import json
import queue
import re
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable


EmitFn = Callable[[dict[str, Any]], None]


# ---------------------------------------------------------------------------
# 公開 API
# ---------------------------------------------------------------------------
@dataclass
class Segment:
    line_id: int
    text: str
    start: float
    end: float
    speaker: str | None = None   # 講者標記（分軌模式：你/對方）；混音模式為 None


def load_glossary(path: str | Path) -> list[str]:
    """從純文字檔載入術語表，每行一個。空檔/不存在 → 回空 list。"""
    p = Path(path)
    if not p.exists():
        return []
    try:
        out = []
        for raw in p.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            out.append(line)
        return out
    except OSError:
        return []


# ---------------------------------------------------------------------------
# Prompt 組裝
# ---------------------------------------------------------------------------
def build_prompt(
    context_lines: list[str],
    batch: list[Segment],
    glossary: list[str],
    summary_block: str = "",
) -> str:
    """組成 LLM 重組+校正 prompt（精簡版）。

    比起含長範例的版本，prompt 從 ~800 tokens 降到 ~300，prefill 加速 ~60%。
    Qwen 系列 instruct 模型不需大量 few-shot，靠規則描述即可。
    """
    glossary_block = ""
    if glossary:
        glossary_block = "\n術語：" + "、".join(glossary[:80]) + "\n"

    context_block = ""
    if context_lines:
        context_block = "\n前文（參考用，勿重複）：" + " / ".join(context_lines[-2:]) + "\n"

    batch_block = "\n".join(
        f'{{"line_id":{s.line_id},"text":"{_escape(s.text)}"}}'
        for s in batch
    )

    return f"""把中文口語逐字稿整理成書面句子：合併被切斷的句子（line_ids 可含多個）、清贅詞（嗯/那個/重複字）、補標點、修近音字、統一數字。不改變語意，不刪減資訊。
合併多行時：在原本斷句的接合處補逗號或句號（依語氣判斷），讓書面化句子讀起來有自然停頓，不是把破碎詞硬黏成一長串。
{summary_block}{glossary_block}{context_block}
輸入：
{batch_block}

輸出 JSON 陣列 [{{"line_ids":[...],"text":"..."}}]；多行合併 → line_ids 含全部；無需修改的行不回。完全不需整理 → []。直接出 JSON、不加說明、不用 markdown。
"""


def _escape(s: str) -> str:
    return s.replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ")


# ---------------------------------------------------------------------------
# F4: 校正信心評分（啟發式，無需 LLM 輸出機率）
# ---------------------------------------------------------------------------
_PUNCT_DIGITS = set("，。、；：！？「」（）()[]{}『』 0123456789.,%-")


def compute_confidence(orig: str, corrected: str) -> float:
    """估計校正信心 0-1。越接近 1 越可信。"""
    if not orig or not corrected:
        return 0.0
    len_ratio = min(len(corrected), len(orig)) / max(len(corrected), len(orig))
    set_orig = set(orig.replace(" ", ""))
    set_corr = set(corrected.replace(" ", ""))
    union = set_orig | set_corr
    overlap = len(set_orig & set_corr) / len(union) if union else 0.0
    new_chars = sum(1 for ch in corrected if ch not in set_orig and ch not in _PUNCT_DIGITS)
    fabrication = new_chars / len(corrected)
    confidence = len_ratio * 0.3 + overlap * 0.5 + (1 - fabrication) * 0.2
    return max(0.0, min(1.0, confidence))


# ---------------------------------------------------------------------------
# F7: LRU 校正結果快取
# ---------------------------------------------------------------------------
class CorrectionCache:
    """Thread-safe LRU cache。key 用 batch 內 segment text，可跨 session 共用。"""

    def __init__(self, max_size: int = 256):
        self._cache: OrderedDict[str, list[dict[str, Any]]] = OrderedDict()
        self._max = max_size
        self._lock = threading.Lock()
        self.hits = 0
        self.misses = 0

    @staticmethod
    def _key(batch: list[Segment]) -> str:
        h = hashlib.sha1()
        for s in batch:
            h.update(s.text.encode("utf-8"))
            h.update(b"\x1f")
        return h.hexdigest()[:20]

    def get(self, batch: list[Segment]) -> list[dict[str, Any]] | None:
        if not batch:
            return None
        k = self._key(batch)
        with self._lock:
            cached = self._cache.get(k)
            if cached is None:
                self.misses += 1
                return None
            self._cache.move_to_end(k)
            self.hits += 1
        result = []
        for c in cached:
            new_lids = [batch[i].line_id for i in c["_idx"] if i < len(batch)]
            if new_lids:
                entry = {k2: v for k2, v in c.items() if k2 != "_idx"}
                entry["line_ids"] = new_lids
                result.append(entry)
        return result

    def put(self, batch: list[Segment], corrections: list[dict[str, Any]]) -> None:
        if not batch:
            return
        k = self._key(batch)
        lid_to_idx = {s.line_id: i for i, s in enumerate(batch)}
        stored = []
        for c in corrections:
            idx = [lid_to_idx[lid] for lid in c["line_ids"] if lid in lid_to_idx]
            if idx:
                entry = {k2: v for k2, v in c.items() if k2 != "line_ids"}
                entry["_idx"] = idx
                stored.append(entry)
        with self._lock:
            self._cache[k] = stored
            self._cache.move_to_end(k)
            while len(self._cache) > self._max:
                self._cache.popitem(last=False)

    def stats(self) -> dict[str, int]:
        with self._lock:
            return {"hits": self.hits, "misses": self.misses, "size": len(self._cache)}


# ---------------------------------------------------------------------------
# Response 解析（與 Ollama 版相同）
# ---------------------------------------------------------------------------
def parse_response(raw: str) -> list[dict[str, Any]]:
    """解析 LLM 回應，找第一個含有效條目的 JSON 陣列。

    修正 `[] [{...}]` 的 bug：舊版用 regex 找第一個 `[...]`，會抓到前面的空陣列。
    現在改用深度追蹤掃描所有 `[...]` 候選，取第一個能 parse 且非空的。
    """
    if not raw:
        return []
    txt = re.sub(r"```(?:json)?\s*", "", raw, flags=re.IGNORECASE)
    txt = txt.replace("```", "").strip()

    def _extract_items(arr: list) -> list[dict[str, Any]]:
        out = []
        for item in arr:
            if not isinstance(item, dict):
                continue
            text_value = item.get("text")
            if not isinstance(text_value, str) or not text_value.strip():
                continue
            line_ids: list[int] = []
            raw_ids = item.get("line_ids")
            if isinstance(raw_ids, list):
                line_ids = [x for x in raw_ids if isinstance(x, int)]
            elif isinstance(item.get("line_id"), int):
                line_ids = [item["line_id"]]
            if not line_ids:
                continue
            out.append({"line_ids": line_ids, "text": text_value.strip()})
        return out

    # 掃描所有 '[' 位置，合併 raw 內所有有效陣列的結果（避免 [{L1}][{L2}] 只取第一個）
    all_items: list[dict[str, Any]] = []
    seen_lids: set[int] = set()

    for match in re.finditer(r"\[", txt):
        start = match.start()
        depth = 0
        end = -1
        for i, ch in enumerate(txt[start:], start):
            if ch == "[":
                depth += 1
            elif ch == "]":
                depth -= 1
                if depth == 0:
                    end = i
                    break

        if end == -1:
            # stop token `] ` 把結尾的 `]` 吃掉了 → 補上再試
            candidate = txt[start:] + "]"
        else:
            candidate = txt[start : end + 1]

        try:
            arr = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if not isinstance(arr, list) or not arr:
            continue
        for item in _extract_items(arr):
            first_lid = item["line_ids"][0]
            if first_lid not in seen_lids:
                all_items.append(item)
                for lid in item["line_ids"]:
                    seen_lids.add(lid)

    # Fallback：若主流程一無所獲，嘗試容錯解析
    # 處理畸形格式：["line_ids":[5],"text":"..."] (缺 dict 包裝)
    # 用 regex 抓 "line_ids":[N] + "text":"..."，不管包在什麼結構裡
    if not all_items:
        pat = re.compile(
            r'"line_ids"\s*:\s*\[\s*([\d,\s]+?)\s*\]\s*,\s*"text"\s*:\s*"((?:[^"\\]|\\.)*)"'
        )
        for m in pat.finditer(txt):
            try:
                ids = [int(x.strip()) for x in m.group(1).split(",") if x.strip()]
                # 用 json.loads 還原內部跳脫字元，避免 unicode_escape 破壞中文
                text_val = json.loads(f'"{m.group(2)}"')
                if ids and text_val.strip() and ids[0] not in seen_lids:
                    all_items.append({"line_ids": ids, "text": text_val.strip()})
                    for lid in ids:
                        seen_lids.add(lid)
            except (ValueError, json.JSONDecodeError):
                continue

    return all_items


# ---------------------------------------------------------------------------
# LLM 載入 + 呼叫（llama-cpp-python）
# ---------------------------------------------------------------------------
# 滾動摘要 Worker
# ---------------------------------------------------------------------------
class SummaryWorker:
    """獨立 thread，累積已校正文字，閒置 N 秒後呼叫 LLM 更新摘要。

    摘要格式：{"topic": "一句話主題", "keywords": ["關鍵詞", ...]}
    透過 get_summary_block() 回傳可注入 prompt 的文字段落。
    """

    def __init__(
        self,
        ensure_llm_fn: Any,
        log_emit: EmitFn,
        data_emit: EmitFn | None = None,
        idle_seconds: float = 15.0,
        log_path: Path | None = None,
    ) -> None:
        self._ensure_llm = ensure_llm_fn
        self._log = log_emit
        self._data_emit = data_emit or (lambda payload: None)
        self._idle_seconds = idle_seconds
        self._log_path = log_path
        self._texts: list[str] = []
        self._summary: dict[str, Any] = {}
        self._lock = threading.Lock()
        self._has_new = threading.Event()
        self._stop = threading.Event()
        self._last_push: float = time.time()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True, name="summary-worker")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._has_new.set()
        if self._thread:
            self._thread.join(timeout=5.0)
        self._thread = None

    def push(self, text: str) -> None:
        """新增一行文字到摘要 buffer。"""
        if not text.strip():
            return
        with self._lock:
            self._texts.append(text.strip())
            self._last_push = time.time()
        self._has_new.set()

    def get_summary_block(self) -> str:
        """回傳可注入 prompt 的摘要段落，無摘要時回空字串。"""
        with self._lock:
            s = dict(self._summary)
        if not s:
            return ""
        topic = s.get("topic", "")
        keywords = s.get("keywords", [])
        parts: list[str] = []
        if topic:
            parts.append(topic)
        if keywords:
            parts.append("關鍵詞：" + "、".join(str(k) for k in keywords[:15]))
        return "\n背景摘要（參考，勿重複輸出）：" + "，".join(parts) + "\n" if parts else ""

    def _run(self) -> None:
        try:
            llm = self._ensure_llm()
        except Exception as err:  # noqa: BLE001
            self._log({"type": "stream.log", "message": f"[summary] LLM 初始化失敗: {err}"})
            return
        if llm is None:
            return
        while not self._stop.is_set():
            # 固定每 N 秒觸發一次，不管是否閒置
            self._stop.wait(timeout=self._idle_seconds)
            if self._stop.is_set():
                break
            with self._lock:
                texts = list(self._texts[-120:])
            if not texts:
                continue
            try:
                full = "\n".join(texts)
                prompt = (
                    "以下是口語錄音逐字稿，請輸出 JSON：\n"
                    '{"topic":"一句話說明主題(15字內)","keywords":["人名/地名/機構/術語"...]}\n'
                    "只回 JSON，不加說明，keywords 最多 15 個。\n\n"
                    f"逐字稿：\n{full[:3000]}"
                )
                raw = llm.generate(prompt, max_tokens=200)
                # 從回應中找 JSON
                m = re.search(r'\{[^{}]+\}', raw, re.DOTALL)
                if m:
                    data = json.loads(m.group())
                    if isinstance(data, dict):
                        with self._lock:
                            self._summary = data
                        kw_count = len(data.get("keywords", []))
                        self._log({
                            "type": "stream.log",
                            "message": (
                                f"[summary] 摘要更新：{data.get('topic','?')} "
                                f"| 關鍵詞 {kw_count} 個"
                            ),
                        })
                        self._data_emit({
                            "type": "transcript.summary",
                            "topic": data.get("topic", ""),
                            "keywords": data.get("keywords", []),
                        })
                        # 寫進 summary_log.jsonl，方便事後分析
                        if self._log_path is not None:
                            try:
                                entry = {
                                    "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
                                    "topic": data.get("topic", ""),
                                    "keywords": data.get("keywords", []),
                                    "n_lines": len(texts),
                                }
                                with open(self._log_path, "a", encoding="utf-8") as f:
                                    f.write(json.dumps(entry, ensure_ascii=False) + "\n")
                            except Exception:
                                pass
            except Exception as err:  # noqa: BLE001
                self._log({"type": "stream.log", "message": f"[summary] 失敗: {err}"})
            self._has_new.clear()


# ---------------------------------------------------------------------------
class LlamaCppLLM:
    """封裝 llama-cpp-python 載入 + generate。

    模型用 huggingface_hub 下載到 HF 快取（與 Whisper 共用）。
    首次載入會花時間（下載 + 編譯 Metal kernel）；之後 reload 是秒級。
    """

    def __init__(self, model_repo: str, model_file: str, n_ctx: int, n_gpu_layers: int):
        from llama_cpp import Llama
        from huggingface_hub import hf_hub_download

        # 下載（已快取則秒回）
        self._model_path = hf_hub_download(
            repo_id=model_repo,
            filename=model_file,
        )
        # 載入 — verbose=False 抑制大量 stdout 訊息
        self._llm = Llama(
            model_path=self._model_path,
            n_ctx=n_ctx,
            n_gpu_layers=n_gpu_layers,
            verbose=False,
        )

    def generate(self, prompt: str, max_tokens: int = 300) -> str:
        # max_tokens 從 1024 → 300：本任務輸出永遠是 JSON 陣列，
        # 即使 batch=3 全合併也不需 300 以上。防 LLM 跑長解釋（前測曾 26s）。
        # stop：JSON 陣列結束符或第一個多餘換行就停。
        out = self._llm(
            prompt,
            max_tokens=max_tokens,
            temperature=0.1,
            top_p=0.9,
            echo=False,
            stop=[
                "]\n\n",     # 正常結束後空行
                "] ",        # ] 後接空白 → 同行加解釋（] 會被吃掉，parser 補回）
                "]\n直接出", # 模型 echo prompt 指令「直接出 JSON...」
                "]\n輸",     # 模型迴圈 echo「輸入：...輸出：」
                "]\n合併",   # F1 副作用：模型 echo「合併多行時：...」prompt 指令
                "]\n把",     # 模型 echo prompt 開頭「把中文口語...」
                "]\n{",      # 模型在陣列外又開新 JSON object（應該包在陣列內）
                "]\n完全",   # 模型 echo「完全不需整理 → []」
                "\n}\n}", "\n```", "\n\n\n",
            ],
        )
        return out["choices"][0]["text"]


# ---------------------------------------------------------------------------
# API LLM（OpenAI-compatible，支援 MiniMax / OpenAI / 任何相容端點）
# ---------------------------------------------------------------------------
class ApiLLM:
    """呼叫雲端 LLM API，支援 OpenAI Chat Completions 與 Anthropic Messages 兩種格式。

    api_format='openai'     → POST /chat/completions，Authorization: Bearer
    api_format='anthropic'  → POST /messages，x-api-key + anthropic-version
    """

    def __init__(
        self,
        base_url: str,
        api_key: str,
        model: str,
        timeout: float = 30.0,
        api_format: str = "openai",
    ):
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._model = model
        self._timeout = timeout
        self._format = api_format  # "openai" or "anthropic"

    def generate(self, prompt: str, max_tokens: int = 300) -> str:
        if self._format == "anthropic":
            return self._call_anthropic(prompt, max_tokens)
        return self._call_openai(prompt, max_tokens)

    def _call_openai(self, prompt: str, max_tokens: int) -> str:
        import urllib.request

        body = json.dumps({
            "model": self._model,
            "max_tokens": 1024,
            "messages": [
                {"role": "system", "content": "你是逐字稿校正助手，嚴格依照指令輸出 JSON，不加說明。"},
                {"role": "user", "content": prompt},
            ],
        }).encode("utf-8")

        req = urllib.request.Request(
            f"{self._base_url}/chat/completions",
            data=body,
            headers={
                "content-type": "application/json",
                "authorization": f"Bearer {self._api_key}",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=self._timeout) as resp:
            result = json.loads(resp.read().decode("utf-8"))
        return (result.get("choices") or [{}])[0].get("message", {}).get("content", "[]")

    def _call_anthropic(self, prompt: str, max_tokens: int) -> str:
        import urllib.request

        body = json.dumps({
            "model": self._model,
            "max_tokens": 1024,
            "system": "你是逐字稿校正助手，嚴格依照指令輸出 JSON，不加說明。",
            "messages": [{"role": "user", "content": prompt}],
        }).encode("utf-8")

        req = urllib.request.Request(
            f"{self._base_url}/messages",
            data=body,
            headers={
                "content-type": "application/json",
                "x-api-key": self._api_key,
                "anthropic-version": "2023-06-01",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=self._timeout) as resp:
            result = json.loads(resp.read().decode("utf-8"))
        for block in (result.get("content") or []):
            if block.get("type") == "text":
                return block["text"]
        return "[]"


# ---------------------------------------------------------------------------
# Worker
# ---------------------------------------------------------------------------
class CorrectionWorker:
    """累積 + 並行處理 LLM 校正。

    架構：
      ┌─────────────┐    ┌─────────────────┐    ┌──────────────────┐
      │ ASR push    │ →  │ Accumulator     │ →  │ Batch queue      │
      │ (segments)  │    │ thread (1 個)   │    │ (待處理批次)      │
      └─────────────┘    └─────────────────┘    └──────────────────┘
                                                          │
                                                          ▼
                                            ┌────────────────────────┐
                                            │ LLM worker 1..N        │
                                            │ 各持一份 Llama 實例     │
                                            └────────────────────────┘
                                                          │
                                                          ▼
                                              emit transcript.correction

    Llama 實例非 thread-safe → 每個 worker thread 一份模型實例。
    parallel_workers=1 = 序列；2+ = 並行（多載 N×1.8GB RAM 換速度）。
    """

    def __init__(
        self,
        config: dict[str, Any],
        emit: EmitFn,
        log_emit: EmitFn | None = None,
        llm: LlamaCppLLM | None = None,
        log_path: Path | None = None,
    ) -> None:
        self._config = config
        self._emit = emit
        self._log = log_emit or (lambda payload: None)
        self._log_path = log_path
        self._segment_queue: queue.Queue[Segment | None] = queue.Queue()
        self._batch_queue: queue.Queue[list[Segment] | None] = queue.Queue()
        self._buffer: list[Segment] = []
        self._corrected_lines: dict[int, str] = {}
        self._corrected_lock = threading.Lock()
        self._stop_event = threading.Event()
        self._acc_thread: threading.Thread | None = None
        self._llm_threads: list[threading.Thread] = []
        self._glossary: list[str] = load_glossary(config["correction.glossary_path"][0])
        self._inject_llm = llm
        # F7: 校正結果快取（相同 batch text → 直接回 cached corrections）
        cache_size = int(config.get("correction.cache_size", 256))
        self._cache = CorrectionCache(max_size=cache_size) if cache_size > 0 else None
        # ASR 閒置旗標：ASR 推理期間 clear，推理完畢後 set；LLM dispatch 前等 set
        self._asr_idle = threading.Event()
        self._asr_idle.set()  # 初始為閒置
        # API 模式時不佔 GPU，不需等 asr_idle
        self._use_api = config.get("correction.backend", "local") == "api"
        # 滾動摘要 worker
        summary_enabled = str(config.get("correction.summary_enabled", True)).lower() not in ("false", "0", "")
        idle_s = float(config.get("correction.summary_idle_seconds", 15.0))
        summary_log_path = (
            self._log_path.parent / "summary_log.jsonl"
            if self._log_path is not None else None
        )
        self._summary_worker: SummaryWorker | None = (
            SummaryWorker(
                ensure_llm_fn=lambda: self._ensure_llm(0),
                log_emit=self._log,
                data_emit=self._emit,
                idle_seconds=idle_s,
                log_path=summary_log_path,
            )
            if summary_enabled else None
        )

    def asr_busy(self) -> None:
        """ASR 開始推理：暫停 LLM batch dispatch，讓 GPU 讓給 ASR。"""
        self._asr_idle.clear()

    def asr_idle(self) -> None:
        """ASR 推理完畢：放行 LLM batch dispatch。"""
        self._asr_idle.set()

    # ── 兼容舊 API：保留 _thread / _buffer / _queue / _corrected_lines / _llm ──
    @property
    def _thread(self) -> threading.Thread | None:
        return self._acc_thread

    @property
    def _queue(self) -> queue.Queue:
        return self._segment_queue

    @property
    def _llm(self) -> LlamaCppLLM | None:
        return self._inject_llm

    def start(self) -> None:
        if self._acc_thread is not None:
            return
        self._stop_event.clear()
        n_workers = max(1, int(self._config.get("correction.parallel_workers", 1)))

        # 累積 thread
        self._acc_thread = threading.Thread(target=self._accumulate_loop, daemon=True, name="correction-acc")
        self._acc_thread.start()

        # LLM worker pool
        for i in range(n_workers):
            t = threading.Thread(target=self._llm_loop, args=(i + 1,), daemon=True, name=f"correction-llm-{i+1}")
            t.start()
            self._llm_threads.append(t)

        # 摘要 worker
        if self._summary_worker is not None:
            self._summary_worker.start()

        backend = self._config.get("correction.backend", "local")
        model_label = (
            self._config.get("correction.api_model", "api")
            if backend == "api"
            else self._config["correction.model_file"][0]
        )
        self._log({
            "type": "stream.log",
            "message": (
                f"[correction] worker started "
                f"(backend={backend}, model={model_label}, "
                f"batch={self._config['correction.batch_lines']}, "
                f"idle={self._config['correction.idle_seconds']}s, "
                f"parallel={n_workers}, "
                f"glossary={len(self._glossary)} terms, "
                f"summary={'on' if self._summary_worker else 'off'})"
            ),
        })

    def push(self, segment: Segment) -> None:
        if self._acc_thread is None:
            return
        self._segment_queue.put(segment)

    def stop(self, timeout: float = 10.0) -> None:
        if self._acc_thread is None:
            return
        self._stop_event.set()
        self._segment_queue.put(None)
        # accumulator 拿到 None 後會把 buffer flush 進 batch_queue 並送 stop sentinel
        self._acc_thread.join(timeout=timeout)
        # 給每個 LLM worker 一個 sentinel
        for _ in self._llm_threads:
            self._batch_queue.put(None)
        for t in self._llm_threads:
            t.join(timeout=timeout)
        self._acc_thread = None
        self._llm_threads = []
        if self._summary_worker is not None:
            self._summary_worker.stop()

    # ── 累積 thread ─────────────────────────────────────────────────
    def _accumulate_loop(self) -> None:
        """單一 thread：從 segment_queue 收集、滿批或閒置就推到 batch_queue。

        F2 動態 batch size：依 buffer 內 segments 的平均字長動態決定 target：
          - 長行（avg ≥ 20 字，已是完整句）→ target=max(2, base-1)，早點送
          - 短行（avg < 8 字，破碎）→ target=base+2，多累幾行才送
          - 中等 → 用 settings 的 batch_lines
        idle 仍會強制 flush，所以最壞延遲不變；只是把「滿批」門檻動態化。
        """
        idle_sec = float(self._config["correction.idle_seconds"])
        batch_n = int(self._config["correction.batch_lines"])
        while not self._stop_event.is_set():
            try:
                seg = self._segment_queue.get(timeout=idle_sec)
            except queue.Empty:
                seg = None  # idle 觸發

            if seg is None:
                self._flush_buffer()
                if self._stop_event.is_set():
                    return
                continue

            self._buffer.append(seg)
            avg_len = sum(len(s.text) for s in self._buffer) / len(self._buffer)
            if avg_len >= 20:
                target = max(2, batch_n - 1)
            elif avg_len < 8:
                target = batch_n + 2
            else:
                target = batch_n
            if len(self._buffer) >= target:
                self._flush_buffer()

    def _flush_buffer(self) -> None:
        """把 buffer 依講者分組後送進 batch_queue。

        分軌模式下「你」「對方」的句子到達順序交錯，若混在同批，LLM 的
        「合併被切斷的句子」會跨講者錯誤合併。依 speaker 分組確保每批單一講者。
        混音模式所有 speaker 皆為 None → 單組，行為與原本相同。
        """
        if not self._buffer:
            return
        groups: dict[str | None, list[Segment]] = {}
        for s in self._buffer:
            groups.setdefault(s.speaker, []).append(s)
        for g in groups.values():
            self._batch_queue.put(g)
        self._buffer = []

    # ── LLM worker thread ───────────────────────────────────────────
    def _llm_loop(self, idx: int) -> None:
        """每個 LLM thread：載入自己的 Llama 實例，從 batch_queue 處理。"""
        llm = self._ensure_llm(idx)
        if llm is None:
            # 載入失敗 → 把後續 batch 都直接丟掉，避免 queue 阻塞
            while True:
                item = self._batch_queue.get()
                if item is None:
                    return
        # 主迴圈
        while True:
            batch = self._batch_queue.get()
            if batch is None:
                return
            # API 模式不用 GPU，不需等；local 模式讓 ASR 優先拿 GPU
            if not self._use_api:
                self._asr_idle.wait(timeout=10.0)
            self._process_batch(llm, batch)

    def _ensure_llm(self, idx: int) -> LlamaCppLLM | ApiLLM | None:
        # 測試注入路徑：所有 worker 共用同一個 mock
        if self._inject_llm is not None:
            return self._inject_llm

        # API 模式：不載入本地模型，直接建立 ApiLLM（無啟動延遲）
        if self._use_api:
            try:
                api_key = self._config.get("correction.api_key", "")
                if not api_key:
                    self._log({"type": "stream.log", "message": "[correction] api_key 未設定，請在 settings 填入"})
                    return None
                llm = ApiLLM(
                    base_url=self._config.get("correction.api_base_url", "https://api.openai.com/v1"),
                    api_key=api_key,
                    model=self._config.get("correction.api_model", "gpt-4o-mini"),
                    timeout=float(self._config.get("correction.timeout_seconds", 30.0)),
                    api_format=self._config.get("correction.api_format", "openai"),
                )
                if idx == 1:
                    self._log({"type": "stream.log", "message": f"[correction] API mode — {llm._model} @ {llm._base_url}"})
                return llm
            except Exception as err:  # noqa: BLE001
                self._log({"type": "stream.log", "message": f"[correction] API init failed: {err}"})
                return None

        # Local 模式：載入 GGUF 模型
        try:
            if idx == 1:
                self._log({
                    "type": "stream.log",
                    "message": f"[correction] loading model {self._config['correction.model_repo'][0]}...",
                })
            t0 = time.time()
            llm = LlamaCppLLM(
                model_repo=self._config["correction.model_repo"][0],
                model_file=self._config["correction.model_file"][0],
                n_ctx=int(self._config["correction.n_ctx"]),
                n_gpu_layers=int(self._config["correction.n_gpu_layers"]),
            )
            self._log({
                "type": "stream.log",
                "message": f"[correction] worker#{idx} ready in {time.time()-t0:.1f}s",
            })
            return llm
        except Exception as err:  # noqa: BLE001
            self._log({
                "type": "stream.log",
                "message": f"[correction] worker#{idx} model load failed: {type(err).__name__}: {err}",
            })
            return None

    def _emit_clean(self, line_ids: list[int]) -> None:
        """送「已處理、無需修改」完成訊號。

        前端只有收到 transcript.correction 才會把行從 pending 轉為完成；
        0-change / skip / timeout / 失敗的批次若靜默 return，那些行會永遠卡在
        pending（轉圈）。送 correction_clean 讓前端把行標記為 clean（原文已確認）。
        """
        if not line_ids:
            return
        self._emit({"type": "transcript.correction_clean", "line_ids": line_ids})

    def _process_batch(self, llm: LlamaCppLLM, batch: list[Segment]) -> None:
        if not batch:
            return

        # F7: 先查快取
        if self._cache is not None:
            cached = self._cache.get(batch)
            if cached is not None:
                with self._corrected_lock:
                    for c in cached:
                        self._corrected_lines[c["line_ids"][0]] = c["text"]
                covered: set[int] = set()
                for c in cached:
                    self._emit({
                        "type": "transcript.correction",
                        "correction": {
                            "line_id": c["line_ids"][0],
                            "line_ids": c["line_ids"],
                            "text": c["text"],
                        },
                    })
                    covered.update(c["line_ids"])
                self._emit_clean([s.line_id for s in batch if s.line_id not in covered])
                self._log({
                    "type": "stream.log",
                    "message": (
                        f"[correction] batch={len(batch)} cache hit, "
                        f"output {len(cached)} 句 "
                        f"(hits={self._cache.hits} misses={self._cache.misses})"
                    ),
                })
                return
        # 單行且明顯未完成的片段（< 5字 或以 ... 結尾）→ 跳過，模型會陷入迴圈
        if len(batch) == 1:
            txt = batch[0].text.strip()
            if len(txt) <= 5 or txt.endswith("...") or txt.endswith("…"):
                self._log({"type": "stream.log", "message": f"[correction] skip fragment L{batch[0].line_id}: '{txt[:20]}'"})
                self._emit_clean([batch[0].line_id])
                return
        n_ctx_lines = int(self._config["correction.context_lines"])
        with self._corrected_lock:
            ctx_lids = sorted(self._corrected_lines.keys())[-n_ctx_lines:] if n_ctx_lines > 0 else []
            ctx_lines = [self._corrected_lines[lid] for lid in ctx_lids]

        summary_block = self._summary_worker.get_summary_block() if self._summary_worker else ""
        prompt = build_prompt(ctx_lines, batch, self._glossary, summary_block)
        timeout = float(self._config["correction.timeout_seconds"])

        t0 = time.time()
        try:
            raw_holder: list[str] = []
            err_holder: list[BaseException] = []

            def _do_gen():
                try:
                    raw_holder.append(llm.generate(prompt))
                except BaseException as e:
                    err_holder.append(e)

            t = threading.Thread(target=_do_gen, daemon=True, name="llm-gen")
            t.start()
            t.join(timeout=timeout)
            if t.is_alive():
                self._log({"type": "stream.log", "message": f"[correction] LLM timeout {timeout}s"})
                self._emit_clean([s.line_id for s in batch])
                return
            if err_holder:
                raise err_holder[0]
            raw = raw_holder[0] if raw_holder else ""
        except Exception as err:  # noqa: BLE001
            self._log({"type": "stream.log", "message": f"[correction] LLM call failed: {type(err).__name__}: {err}"})
            self._emit_clean([s.line_id for s in batch])
            return
        dt = time.time() - t0

        corrections = parse_response(raw)
        valid_lids = {s.line_id for s in batch}
        valid = [
            c for c in corrections
            if c["line_ids"] and all(lid in valid_lids for lid in c["line_ids"])
        ]

        # F4: 信心評分 + 過濾低品質校正
        min_conf = float(self._config.get("correction.min_confidence", 0.5))
        batch_text_by_lid = {s.line_id: s.text for s in batch}
        scored: list[dict[str, Any]] = []
        low_conf_drops: list[dict[str, Any]] = []
        for c in valid:
            orig = "".join(batch_text_by_lid.get(lid, "") for lid in c["line_ids"])
            conf = round(compute_confidence(orig, c["text"]), 3)
            c_with_conf = {**c, "confidence": conf}
            if conf >= min_conf:
                scored.append(c_with_conf)
            else:
                low_conf_drops.append(c_with_conf)
        if low_conf_drops:
            self._log({
                "type": "stream.log",
                "message": (
                    f"[correction] 過濾低信心校正 ({len(low_conf_drops)}): "
                    + "; ".join(f"L{c['line_ids']} conf={c['confidence']}" for c in low_conf_drops[:3])
                ),
            })
        valid = scored

        if self._log_path is not None:
            try:
                entry = {
                    "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
                    "batch": [{"line_id": s.line_id, "text": s.text} for s in batch],
                    "prompt": prompt,
                    "raw": raw,
                    "corrections": valid,
                    "duration_s": round(dt, 3),
                }
                with open(self._log_path, "a", encoding="utf-8") as f:
                    f.write(json.dumps(entry, ensure_ascii=False) + "\n")
            except Exception:
                pass

        # F7: 寫入快取（即使 valid 為空也存，避免相同 batch 再跑 LLM）
        if self._cache is not None:
            self._cache.put(batch, valid)

        if not valid:
            self._log({"type": "stream.log", "message": f"[correction] batch={len(batch)} done in {dt:.1f}s, 0 changes"})
            self._emit_clean([s.line_id for s in batch])
            # 即使無校正，仍把原文推給滾動摘要（不推則 _texts 永遠空，摘要永不觸發）
            if self._summary_worker is not None:
                for s in batch:
                    self._summary_worker.push(s.text)
            return

        with self._corrected_lock:
            for c in valid:
                # 用第一個 line_id 當主鍵（供下次 context 用）
                self._corrected_lines[c["line_ids"][0]] = c["text"]

        covered: set[int] = set()
        for c in valid:
            # 同時送 line_id（第一個，舊版兼容）+ line_ids（新版陣列）
            self._emit({
                "type": "transcript.correction",
                "correction": {
                    "line_id": c["line_ids"][0],
                    "line_ids": c["line_ids"],
                    "text": c["text"],
                },
            })
            covered.update(c["line_ids"])
        # 同批中未被任何校正涵蓋的行 → 標記為 clean（已確認原文）
        self._emit_clean([s.line_id for s in batch if s.line_id not in covered])
        # 把校正後文字(或原文)推給摘要 worker
        if self._summary_worker is not None:
            corrected_by_lid = {lid: c["text"] for c in valid for lid in c["line_ids"]}
            for s in batch:
                self._summary_worker.push(corrected_by_lid.get(s.line_id, s.text))
        self._log({
            "type": "stream.log",
            "message": (
                f"[correction] batch={len(batch)} done in {dt:.1f}s, "
                f"output {len(valid)} 句（涵蓋 {sum(len(c['line_ids']) for c in valid)} 原行）"
            ),
        })
