"""執行期參數設定。

職責：
- 定義所有「可調」數字參數的預設值、範圍、UI 分區
- 從 data/settings.json 載入（不存在則用 defaults）
- 寫回 data/settings.json
- 驗證輸入值（範圍、型別）

不可動的參數（SAMPLE_RATE、VAD_FRAME_SIZE 等）不在此模組，保留硬編碼。

生效時機：在 StreamService.start() 內呼叫 load_settings() 取最新值。
錄音中變更不影響當下 chunk 處理；下次「開始錄音」生效。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------
SettingType = Literal["int", "float", "str_list", "str_set", "string"]


@dataclass
class SettingDef:
    """單一參數定義。"""
    key: str                                  # 例：'asr.no_speech_threshold'
    label: str                                # UI 顯示名稱
    type: SettingType
    default: Any
    min: float | None = None                  # int / float 限制
    max: float | None = None
    choices: list[str] | None = None          # str_list / str_set 候選
    section: Literal["main", "advanced", "model"] = "advanced"
    group: str = "其他"                        # UI 分組標題
    help: str = ""                             # tooltip / 說明文字
    step: float | None = None                  # UI input step（None = 自動）


# ---------------------------------------------------------------------------
# 所有可調參數
# ---------------------------------------------------------------------------
SETTING_DEFS: list[SettingDef] = [
    # ─── 主要：ASR 閾值 ───────────────────────────────────────────────
    SettingDef(
        key="asr.no_speech_threshold",
        label="無語音機率上限",
        type="float", default=0.9, min=0.0, max=1.0, step=0.05,
        section="main", group="ASR 閾值",
        help="nsp > 此值的 segment 視為靜音/幻覺直接丟。值越大保留越多（也容易引入幻覺）",
    ),
    SettingDef(
        key="asr.nsp_gray_min_len",
        label="灰色地帶最低字數",
        type="int", default=4, min=1, max=20,
        section="main", group="ASR 閾值",
        help="0.5 < nsp ≤ 上限的灰色地帶：文字 < 此字數 → 丟。中文單字回應建議 ≤2",
    ),

    # ─── 主要：Context / Prompt ──────────────────────────────────────
    SettingDef(
        key="context.max_chars",
        label="Rolling Context 上限",
        type="int", default=40, min=0, max=200,
        section="main", group="Context",
        help="作為 initial_prompt 的歷史字數上限。0 = 不餵 prompt（防迴圈但失去連續性）",
    ),

    # ─── 主要：語言切換 ─────────────────────────────────────────────
    SettingDef(
        key="lang.switch_confirm",
        label="語言切換確認次數",
        type="int", default=2, min=1, max=5,
        section="main", group="語言偵測",
        help="連續 N 個 chunk 偵測到同語言才正式切換 / init",
    ),
    SettingDef(
        key="lang.whitelist",
        label="允許的偵測語言",
        type="str_set", default=["zh", "en", "yue"],
        choices=["zh", "en", "yue", "ja", "ko", "de", "fr", "es", "it", "ru", "vi", "th"],
        section="main", group="語言偵測",
        help="偵測到非此清單的語言 → 視為雜訊忽略（不寫 context、不切換）",
    ),

    # ─── 主要：佇列 / 效能 ─────────────────────────────────────────
    SettingDef(
        key="queue.max_backlog",
        label="積壓 chunk 上限",
        type="int", default=3, min=1, max=10,
        section="main", group="效能",
        help="ASR 跟不上時，超過此值會丟舊 chunk。值小延遲低、值大保留多",
    ),

    # ─── 主要：音訊閾值 ─────────────────────────────────────────────
    SettingDef(
        key="audio.rms_silence_threshold",
        label="RMS 靜音門檻",
        type="float", default=0.01, min=0.0, max=0.5, step=0.005,
        section="main", group="音訊",
        help="chunk RMS < 此值 → 跳過 ASR。廣播類含背景音樂可降低（如 0.005）",
    ),

    # ─── 主要：速率 ────────────────────────────────────────────────
    SettingDef(
        key="rate.fallback",
        label="冷啟動速率上限 (字/秒)",
        type="float", default=30.0, min=1.0, max=200.0,
        section="main", group="速率",
        help="樣本不足時的保守上限。中文約 6/s、英文約 15/s",
    ),

    # ─── 主要：VAD ────────────────────────────────────────────────
    SettingDef(
        key="vad.speech_threshold",
        label="VAD 語音閾值",
        type="float", default=0.5, min=0.0, max=1.0, step=0.05,
        section="main", group="VAD",
        help="Silero VAD 機率 > 此值 → 有語音",
    ),

    # ─── 主要：預覽 ───────────────────────────────────────────────
    SettingDef(
        key="preview.window_seconds",
        label="預覽視窗長度（秒）",
        type="float", default=5.0, min=1.0, max=30.0, step=0.5,
        section="main", group="即時預覽",
        help="即時預覽看最近幾秒音訊",
    ),

    # ─── 進階：VAD ────────────────────────────────────────────────
    SettingDef(
        key="vad.silence_threshold",
        label="VAD 靜音閾值",
        type="float", default=0.35, min=0.0, max=1.0, step=0.05,
        section="advanced", group="VAD",
        help="Silero VAD 機率 < 此值 → 靜音",
    ),
    SettingDef(
        key="vad.silence_frames",
        label="連續靜音幀數",
        type="int", default=9, min=3, max=30,
        section="advanced", group="VAD",
        help="連續 N 幀（每幀 32ms）靜音 → 觸發切句",
    ),
    SettingDef(
        key="vad.min_speech_seconds",
        label="最少語音長度（秒）",
        type="float", default=1.0, min=0.2, max=10.0, step=0.1,
        section="advanced", group="VAD",
        help="緩衝最少累積到此長度才 flush",
    ),
    SettingDef(
        key="vad.peak_ratio",
        label="RMS 峰值比例",
        type="float", default=0.5, min=0.1, max=0.9, step=0.05,
        section="advanced", group="VAD",
        help="RMS > peak × 此比例 → 視為語音幀（句末偵測用）",
    ),
    SettingDef(
        key="vad.force_flush_seconds",
        label="強制 flush 時長（秒）",
        type="float", default=2.5, min=1.0, max=30.0, step=0.5,
        section="advanced", group="VAD",
        help="buffer 累積這麼久還沒切點 → 強制送 ASR",
    ),
    SettingDef(
        key="vad.leading_silence_trim_seconds",
        label="前段靜音裁切閾值（秒）",
        type="float", default=0.5, min=0.0, max=5.0, step=0.1,
        section="advanced", group="VAD",
        help="chunk 開頭靜音超過此值 → 裁掉",
    ),
    SettingDef(
        key="vad.min_speech_in_chunk_seconds",
        label="Chunk 內最少語音（秒）",
        type="float", default=0.3, min=0.0, max=5.0, step=0.1,
        section="advanced", group="VAD",
        help="chunk 內語音 < 此值 → 不送 ASR",
    ),

    # ─── 進階：Chunk 邊界 ─────────────────────────────────────────
    SettingDef(
        key="chunk.overlap_seconds",
        label="Chunk 重疊長度（秒）",
        type="float", default=0.5, min=0.0, max=3.0, step=0.1,
        section="advanced", group="Chunk",
        help="chunk 之間重疊音訊，補救邊界字",
    ),

    # ─── 進階：速率 ───────────────────────────────────────────────
    SettingDef(
        key="rate.hard_limit",
        label="速率絕對上限 (字/秒)",
        type="float", default=50.0, min=10.0, max=500.0, step=5.0,
        section="advanced", group="速率",
        help="超過此值一律視為幻覺迴圈，整段丟",
    ),
    SettingDef(
        key="rate.min_samples",
        label="動態閾值最少樣本",
        type="int", default=5, min=1, max=50,
        section="advanced", group="速率",
        help="累積 N 個樣本才啟用 mean+5σ 動態閾值",
    ),
    SettingDef(
        key="rate.sigma",
        label="動態閾值 sigma 倍數",
        type="float", default=5.0, min=1.0, max=10.0, step=0.5,
        section="advanced", group="速率",
        help="動態上限 = mean + N × std",
    ),
    SettingDef(
        key="rate.min_seg_duration",
        label="速率計算最低時長（秒）",
        type="float", default=0.3, min=0.1, max=2.0, step=0.1,
        section="advanced", group="速率",
        help="seg 短於此值不納入速率統計",
    ),
    SettingDef(
        key="rate.repeat_in_chunk_threshold",
        label="同 chunk 重複容忍",
        type="int", default=3, min=2, max=10,
        section="advanced", group="速率",
        help="同 chunk 內相同 seg ≥ 此數 → 整 chunk 視為迴圈丟",
    ),

    # ─── 進階：Segment 過濾 ────────────────────────────────────────
    SettingDef(
        key="segment.min_text_len",
        label="Segment 最低字數",
        type="int", default=2, min=1, max=10,
        section="advanced", group="Segment",
        help="文字 < 此值 → 丟。設 1 可保留中文單字回應",
    ),
    SettingDef(
        key="segment.merge_max_gap_seconds",
        label="合併間隔上限（秒）",
        type="float", default=0.3, min=0.0, max=2.0, step=0.1,
        section="advanced", group="Segment",
        help="相鄰 segment 間距 ≤ 此值才考慮合併",
    ),
    SettingDef(
        key="segment.merge_min_duration_seconds",
        label="合併長度上限（秒）",
        type="float", default=1.0, min=0.0, max=5.0, step=0.1,
        section="advanced", group="Segment",
        help="長度 < 此值的 segment 才會被合併",
    ),

    # ─── 進階：預覽 ────────────────────────────────────────────────
    SettingDef(
        key="preview.interval_seconds",
        label="預覽觸發間隔（秒）",
        type="float", default=0.3, min=0.1, max=2.0, step=0.05,
        section="advanced", group="即時預覽",
        help="預覽 worker 每隔多久跑一次",
    ),
    SettingDef(
        key="preview.min_audio_seconds",
        label="預覽最低音訊長（秒）",
        type="float", default=0.5, min=0.1, max=2.0, step=0.1,
        section="advanced", group="即時預覽",
        help="預覽音訊不足此長度 → 略過",
    ),
    SettingDef(
        key="preview.silence_clear_seconds",
        label="預覽靜音清除（秒）",
        type="float", default=1.0, min=0.2, max=5.0, step=0.1,
        section="advanced", group="即時預覽",
        help="連續靜音超過此值 → 清空預覽",
    ),

    # ─── 模型 tab：LLM 即時校正（永遠啟用、無 enable 開關）─────────
    SettingDef(
        key="correction.model_repo",
        label="模型 HF Repo",
        type="str_list", default=["bartowski/Qwen2.5-3B-Instruct-GGUF"],
        choices=[
            "bartowski/Qwen2.5-3B-Instruct-GGUF",       # 預設，中文校正能力強
            "bartowski/Qwen2.5-7B-Instruct-GGUF",       # 更強但較慢、檔案較大
        ],
        section="model", group="LLM 模型",
        help="HuggingFace repo ID。首次使用會自動下載到本機快取，改後需重啟錄音重新載入",
    ),
    SettingDef(
        key="correction.model_file",
        label="模型檔名（Q4_K_M 推薦）",
        type="str_list", default=["Qwen2.5-3B-Instruct-Q4_K_M.gguf"],
        section="model", group="LLM 模型",
        help="Repo 內檔名。Q4_K_M = 4-bit 量化、品質/速度最佳平衡",
    ),
    SettingDef(
        key="correction.batch_lines",
        label="批次行數",
        type="int", default=3, min=1, max=30,
        section="model", group="校正觸發",
        help="累積 N 行才送 LLM。3-5 行：給 LLM 跨行 context 可重組句子；1 = 逐句立即校正（無重組能力）",
    ),
    SettingDef(
        key="correction.idle_seconds",
        label="閒置觸發時間（秒）",
        type="float", default=3.0, min=0.5, max=60.0, step=0.5,
        section="model", group="校正觸發",
        help="連續 N 秒沒新 segment 時，把累積的不滿一批也送出。太小（< 2s）會導致 batch 永遠湊不滿、跨行合併失效",
    ),
    SettingDef(
        key="correction.parallel_workers",
        label="並行 LLM 數",
        type="int", default=1, min=1, max=4,
        section="model", group="校正觸發",
        help="同時跑幾顆 LLM。每多 1 → 多載一份模型（~1.8GB RAM）。單 GPU 機器建議 1，多 GPU 再加",
    ),
    SettingDef(
        key="correction.glossary_path",
        label="術語表路徑",
        type="str_list", default=["data/glossary.txt"],
        section="model", group="校正觸發",
        help="純文字檔，每行一個術語/人名/產品名。空檔或不存在則跳過",
    ),
    SettingDef(
        key="correction.n_ctx",
        label="Context window 大小",
        type="int", default=512, min=512, max=32768,
        section="model", group="進階模型參數",
        help="KV cache 分配上限（tokens）。本任務 prompt+輸出 < 400 tokens，512 已足夠且顯著節省記憶體頻寬",
    ),
    SettingDef(
        key="correction.n_gpu_layers",
        label="GPU 加速層數",
        type="int", default=-1, min=-1, max=99,
        section="model", group="進階模型參數",
        help="-1 = 全部上 GPU（Metal）、0 = 純 CPU、其他正值 = 指定層數",
    ),
    SettingDef(
        key="correction.timeout_seconds",
        label="LLM 呼叫超時（秒）",
        type="float", default=60.0, min=5.0, max=300.0, step=5.0,
        section="model", group="進階模型參數",
        help="單次 LLM 呼叫超時。超過視為失敗，保留原文",
    ),
    SettingDef(
        key="correction.context_lines",
        label="Prompt 前文行數",
        type="int", default=2, min=0, max=10,
        section="model", group="進階模型參數",
        help="送 LLM 時附帶前 N 行已校正內容當參考",
    ),
    SettingDef(
        key="correction.min_confidence",
        label="校正最低信心",
        type="float", default=0.5, min=0.0, max=1.0, step=0.05,
        section="model", group="進階模型參數",
        help="低於此值的校正不會 emit。0=不過濾，0.5=擋掉明顯亂編，0.7=只保留高信心",
    ),
    SettingDef(
        key="correction.cache_size",
        label="校正結果快取大小",
        type="int", default=256, min=0, max=4096,
        section="model", group="進階模型參數",
        help="LRU 快取相同 batch text 的校正結果，跨 session 共用。0 = 關閉快取",
    ),
    SettingDef(
        key="correction.summary_enabled",
        label="滾動摘要",
        type="string", default="true",
        section="model", group="滾動摘要",
        help="啟用後，獨立 thread 會持續摘要已轉錄內容，輔助校正近音字與專有名詞",
    ),
    SettingDef(
        key="correction.summary_idle_seconds",
        label="摘要閒置觸發秒數",
        type="float", default=15.0, min=5.0, max=120.0, step=5.0,
        section="model", group="滾動摘要",
        help="每隔幾秒自動觸發一次摘要更新（秒）",
    ),
    SettingDef(
        key="correction.backend",
        label="校正後端",
        type="string", default="local",
        section="model", group="API 校正",
        help="local = 本地 GGUF 模型；api = OpenAI-compatible 雲端 API（MiniMax 等）",
    ),
    SettingDef(
        key="correction.api_base_url",
        label="API Base URL",
        type="string", default="https://api.openai.com/v1",
        section="model", group="API 校正",
        help="OpenAI-compatible endpoint（OpenAI、MiniMax、Groq 等均可）",
    ),
    SettingDef(
        key="correction.api_key",
        label="API Key",
        type="string", default="",
        section="model", group="API 校正",
        help="雲端 API 金鑰，correction.backend=api 時必填",
    ),
    SettingDef(
        key="correction.api_model",
        label="API 模型名稱",
        type="string", default="gpt-4o-mini",
        section="model", group="API 校正",
        help="呼叫 API 時使用的模型 ID（依所選 endpoint 填入）",
    ),
    SettingDef(
        key="correction.api_format",
        label="API 格式",
        type="string", default="openai",
        section="model", group="API 校正",
        choices=["openai", "anthropic"],
        help="openai = Chat Completions（/chat/completions, Authorization: Bearer）；anthropic = Messages API（/messages, x-api-key）",
    ),
]


# ---------------------------------------------------------------------------
# 取值 / 驗證
# ---------------------------------------------------------------------------
def defaults() -> dict[str, Any]:
    return {d.key: (list(d.default) if isinstance(d.default, list) else d.default) for d in SETTING_DEFS}


def schema() -> list[dict[str, Any]]:
    """給前端 UI 用的 schema（含 label/help/range/section/group）。"""
    out = []
    for d in SETTING_DEFS:
        item: dict[str, Any] = {
            "key": d.key,
            "label": d.label,
            "type": d.type,
            "default": d.default,
            "section": d.section,
            "group": d.group,
            "help": d.help,
        }
        if d.min is not None: item["min"] = d.min
        if d.max is not None: item["max"] = d.max
        if d.choices is not None: item["choices"] = d.choices
        if d.step is not None: item["step"] = d.step
        out.append(item)
    return out


def _coerce(d: SettingDef, value: Any) -> Any:
    """強制轉型 + 範圍夾。"""
    if d.type == "int":
        v = int(value)
        if d.min is not None: v = max(int(d.min), v)
        if d.max is not None: v = min(int(d.max), v)
        return v
    if d.type == "float":
        v = float(value)
        if d.min is not None: v = max(d.min, v)
        if d.max is not None: v = min(d.max, v)
        return v
    if d.type in ("str_list", "str_set"):
        if not isinstance(value, list):
            raise ValueError(f"{d.key} must be list, got {type(value).__name__}")
        if d.choices:
            return [v for v in value if v in d.choices]
        return [str(v) for v in value]
    if d.type == "string":
        return str(value)
    raise ValueError(f"unknown type {d.type}")


def validate(values: dict[str, Any]) -> dict[str, Any]:
    """套用 defaults + 收斂 + 範圍夾。"""
    out = defaults()
    for d in SETTING_DEFS:
        if d.key in values and values[d.key] is not None:
            try:
                out[d.key] = _coerce(d, values[d.key])
            except (ValueError, TypeError):
                pass  # 忽略不合法值，沿用 default
    return out


# ---------------------------------------------------------------------------
# 持久化
# ---------------------------------------------------------------------------
_DEFAULT_PATH = Path(__file__).parent.parent.parent / "data" / "settings.json"


def settings_path() -> Path:
    return _DEFAULT_PATH


def load() -> dict[str, Any]:
    p = settings_path()
    if not p.exists():
        return defaults()
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
        return validate(raw)
    except (json.JSONDecodeError, OSError):
        return defaults()


def save(values: dict[str, Any]) -> dict[str, Any]:
    validated = validate(values)
    p = settings_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(validated, ensure_ascii=False, indent=2), encoding="utf-8")
    return validated
