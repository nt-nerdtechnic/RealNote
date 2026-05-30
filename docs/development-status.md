# Architecture & Technical Reference

## Architecture Overview

```
Electron Main (Node.js)
  └─ spawn ──→ Python FastAPI backend (127.0.0.1:random port)
                  ├─ /ws          JSON RPC（控制指令 + 事件推送）
                  └─ /ws/audio    Binary Float32-LE PCM stream

Vue 3 Renderer
  ├─ useBackend.ts       module-level singleton WebSocket
  └─ ScriptProcessorNode → /ws/audio（16kHz Float32）
```

### 音訊採集流程

```
getUserMedia (mic, 16kHz mono)          → GainNode(你)  ─┐
getDisplayMedia (system audio loopback) → GainNode(對方) ─┤→ ChannelMerger
                                                           ↓
                                          ScriptProcessorNode (4096 frames = 256ms)
                                                           ↓
                                          /ws/audio binary WebSocket (Float32-LE)
                                                           ↓
                                          StreamService.push_pcm()
```

### ASR Pipeline

```
push_pcm()
  └─ 分軌 de-interleave → _TrackBuf[0](你) / _TrackBuf[1](對方)
       └─ Silero VAD（逐 32ms 幀偵測句尾）
            └─ flush chunk_NNNNNN.wav（含 0.5s overlap）
                 └─ _chunk_queue → _asr_worker (thread)
                      └─ FasterWhisperAsr.transcribe()
                           ├─ MLX path（Apple Silicon）: ProcessPoolExecutor subprocess
                           └─ faster-whisper path（CPU fallback）
                      └─ 反幻覺六層過濾 → ITN → 分配 line_id
                           └─ emit transcript.segment

並行 Preview:
  push_pcm() → _preview_buffer（5s 滾動）→ 每 300ms 觸發 tiny ASR → emit transcript.preview

並行 LLM 校正:
  transcript.segment → CorrectionWorker（批次 3 行，閒置 3s 觸發）
    → 本地 GGUF（llama-cpp-python）或 OpenAI-compatible API
    → emit transcript.correction（以 line_id 回綁）
```

---

## Key Design Decisions

### 雙 WebSocket 分離
`/ws` 處理 JSON RPC（send/response + event push），`/ws/audio` 專門傳 binary PCM，避免 head-of-line blocking。

### useBackend module-level singleton
```typescript
let _singleton: ... | null = null
export function useBackend() {
  if (_singleton === null) _singleton = _createBackend()
  return _singleton
}
```
所有元件共用同一條 WS 與事件 bus。多 instance 會導致連線重複、各自 reconnect、狀態互相覆寫。

### MLX subprocess 隔離
`mlx_whisper` 在 `ProcessPoolExecutor(max_workers=1)` 內執行。Metal GPU crash 只殺 worker subprocess，主 backend process 捕捉 `BrokenProcessPool` 後自動重建 executor，不影響錄音。

### 雙軌 per-track 獨立狀態
```
_TrackBuf: PCM buffer / VAD iterator / overlap buffer（push_pcm 側）
_TrackCtx: rolling context / 語言 sticky / 語速統計 / segments（_asr_worker 側）
```
混音模式只用 track 0；分軌模式 track 0=你（mic）/ track 1=對方（system audio），各自不互相污染。

### 六層反幻覺過濾
1. `no_speech_prob > 0.9` 丟棄；`0.5 < nsp ≤ 0.9` 且文字 < 4 字丟棄
2. 語言白名單（`zh/en/yue`），白名單外偵測結果視為雜訊
3. 動態語速上限：`mean + 5σ`（冷啟動 30 字/秒），硬上限 50 字/秒
4. Rolling context 字面重複比對（跨語言合併所有桶，防迴圈幻覺）
5. `_enforce_chronological`：時間戳重疊整段平移，保留 duration 不丟文字
6. Chunk 內相同 segment ≥ 3 次 → 整 chunk 丟棄

### line_id 穩定回綁機制
每個 emit 出去的 segment 分配 monotonic `line_id` + `is_complete=true`。LLM 校正、Breeze 終版校正皆以 `line_id` 回綁，前端不依賴陣列 index，合併多行時以 `line_ids[]` 表達範圍。

---

## Key Files

### Electron / TypeScript

| 檔案 | 職責 |
|------|------|
| `src/main/index.ts` | 視窗管理、media/display-media 權限、DisplayMedia loopback handler |
| `src/main/backend.ts` | spawn Python backend、health polling、graceful kill（process group）|
| `src/preload/index.ts` | contextBridge：`getBackendInfo / pickOutputDir / openPath / requestMicAccess` |
| `src/renderer/src/App.vue` | 全部 UI + 多來源音訊擷取 + 雙軌 ChannelMerger |
| `src/renderer/src/composables/useBackend.ts` | module-level singleton WS + JSON RPC + event bus |
| `src/renderer/src/components/SettingsDialog.vue` | 設定中心 UI |

### Python Backend

| 檔案 | 職責 |
|------|------|
| `backend/meeting_minutes_backend/app.py` | FastAPI；`/ws` + `/ws/audio` + message dispatch |
| `backend/meeting_minutes_backend/__main__.py` | 父程序 watchdog（ppid 變 1 → os._exit）|
| `backend/meeting_minutes_backend/stream_service.py` | PCM 累積、VAD 切句、ASR 編排、反幻覺、line_id 分配 |
| `backend/meeting_minutes_backend/faster_asr.py` | MLX / faster-whisper live ASR；ProcessPoolExecutor 管理 |
| `backend/meeting_minutes_backend/asr.py` | Breeze-ASR-25 終版校正（HuggingFace Transformers）|
| `backend/meeting_minutes_backend/correction_worker.py` | LLM 即時校正 worker（GGUF 或 API）|
| `backend/meeting_minutes_backend/settings.py` | schema-driven 設定系統（30+ 參數）|
| `backend/meeting_minutes_backend/itn.py` | 中文 ITN（百分比/電話/日期/時間/數字）|
| `backend/meeting_minutes_backend/transcript.py` | JSON/TXT 序列化 |
| `backend/meeting_minutes_backend/summarizer.py` | OpenAI-compatible API 摘要生成 |
| `backend/meeting_minutes_backend/ipc.py` | WebSocket envelope（make_response/make_error/make_event）|
| `backend/meeting_minutes_backend/glossary_miner.py` | 從校正結果學習新術語 |

---

## Settings Reference

設定持久化於 `data/settings.json`（gitignored），透過 UI 或直接編輯 JSON 均可。
錄音中變更不影響當次，下次「開始收音」時生效。

| 群組 | 關鍵參數 | 說明 |
|------|----------|------|
| ASR | `asr.no_speech_threshold` | Whisper no_speech_prob 過濾上限 |
| VAD | `vad.speech_threshold`、`vad.force_flush_seconds` | Silero VAD 閾值、強制切句上限 |
| 語言 | `lang.whitelist`、`lang.switch_confirm` | 接受語言白名單、切換確認次數 |
| 速率 | `rate.fallback`、`rate.sigma`、`rate.hard_limit` | 反幻覺語速閾值 |
| LLM 校正 | `correction.backend`、`correction.api_key`、`correction.api_model` | `local` 或 `api` |
| 預覽 | `preview.window_seconds`、`preview.interval_seconds` | 滾動預覽視窗與觸發間隔 |

完整參數說明請開啟 App → ⚙️ 設定 → 進階。

---

## Known Limitations

- **macOS only**：`audio: 'loopback'`（getDisplayMedia 系統音效）與 MLX 加速僅在 macOS 驗證。
- **無認證**：單使用者本地工具設計，backend 僅監聽 127.0.0.1。
- **設定非熱套用**：設定變更需重新「開始錄音」才生效（刻意設計，避免錄音中狀態混亂）。
- **getDisplayMedia 需每次授權**：無法持久化系統音效權限。
- **MLX 冷啟動約 9 秒**：已用背景暖機抵銷，前端以 `stream.asr_ready` 輪詢等待。
- **非 Apple Silicon**：fallback 至 faster-whisper CPU，速度較慢，`LIVE_ASR_MODEL=small` 建議。
