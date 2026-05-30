# RealNote

本地端 AI 會議記錄桌面工具。使用 Whisper 做即時逐字稿、Silero VAD 智慧切句、LLM 即時校正，停止後可產生結構化會議摘要。全部在本機執行，錄音不離開你的電腦。

> **目前僅支援 macOS**，Apple Silicon（M1 以上）可獲得最佳效能。

---

## Features

- **三層 ASR 引擎**：tiny 即時預覽（300ms 更新）→ mlx-whisper 即時逐字稿（Apple Silicon Metal 加速）→ Breeze-ASR-25 終版校正
- **雙軌講者標記**：麥克風（你）與系統音效（對方）分軌辨識，逐字稿自動標註講者
- **LLM 即時校正**：每 3 行批次送本地 GGUF 模型（Qwen2.5-3B）或 OpenAI-compatible API 修正錯字、補標點、統一術語
- **反幻覺六層過濾**：語速動態閾值、語言白名單、rolling context 重複比對、no_speech_prob、時間戳重疊修正、chunk 內重複偵測
- **ITN 後處理**：中文數字、日期、時間、電話、百分比自動轉符號
- **會議摘要**：錄音結束後一鍵產生結構化 `minutes.md`（需填入 API key）
- **設定中心**：30+ 參數 schema-driven UI，含 VAD、ASR、LLM、語言切換等，持久化於 `data/settings.json`

---

## Requirements

| 工具 | 版本 | 說明 |
|------|------|------|
| macOS | 13+ | Apple Silicon 建議 |
| Node.js | 20+ | |
| pnpm | 9+ | `npm i -g pnpm` |
| Python | 3.10+ | |
| [uv](https://docs.astral.sh/uv/) | 最新 | `curl -LsSf https://astral.sh/uv/install.sh \| sh` |

---

## Quick Start

```bash
git clone https://github.com/nt-nerdtechnic/RealNote.git
cd RealNote
bash install.sh
```

`install.sh` 會自動：建立 `backend/.venv`、執行 `uv sync`、執行 `pnpm install`、複製設定範本。

完成後啟動：

```bash
pnpm dev
```

---

## Manual Setup

```bash
pnpm install
uv --project backend sync
cp .env.example .env
cp data/settings.example.json data/settings.json
```

---

## Usage

### 啟動 App

```bash
pnpm dev
```

Electron 視窗開啟後，backend 會在本機隨機 port 啟動，UI 自動連線。

### 錄音流程

1. **選擇音訊來源**：麥克風、系統音效（螢幕）、虛擬裝置可並存，各自獨立調整 gain / mute
2. **勾選「分軌標記講者」**（預設開啟）：需同時加入麥克風與系統音效來源
3. 按「開始收音」，等待 ASR 暖機完成（約 10 秒）
4. 說話，即時逐字稿會持續更新；右側鏡像欄顯示 LLM 校正版本
5. 按「停止」，錄音結束

### 輸出檔案

```text
data/output/<timestamp>/
├── chunks/               ← 原始 WAV 片段
├── transcript.json/txt   ← 即時逐字稿
├── llm_corrected_transcript.json/txt  ← LLM 校正版
├── display_log.txt       ← 完整事件紀錄（含校正前後對照）
└── minutes.md            ← 會議摘要（需 API key）
```

---

## LLM 校正與會議摘要設定

點右上角 ⚙️ 進入設定，或直接編輯 `data/settings.json`：

| 設定鍵 | 說明 |
|--------|------|
| `correction.backend` | `local`（本地 GGUF）或 `api`（雲端） |
| `correction.api_base_url` | OpenAI-compatible endpoint |
| `correction.api_key` | API 金鑰（不會進入 git） |
| `correction.api_model` | 模型名稱，例如 `gpt-4o-mini` |

**本地 GGUF 模式**（預設，不需 API key）：首次啟用會自動從 HuggingFace 下載 Qwen2.5-3B-Instruct Q4_K_M（約 1.6 GB）。

---

## Security

`data/settings.json`（含 API key）與 `data/output/`（錄音與逐字稿）皆已列入 `.gitignore`，**不會被 commit**。

- 首次使用：複製 `data/settings.example.json` → `data/settings.json` 再填入 key
- `.env` 同樣被排除，請複製 `.env.example` 後自行填寫
- Fork 本專案後，可用 [gitleaks](https://github.com/gitleaks/gitleaks) 掃描歷史紀錄

---

## Architecture

```
Electron Main (Node.js)
  └─ spawn ──→ Python FastAPI backend (127.0.0.1:random)
                  ├─ /ws          JSON RPC（控制指令）
                  └─ /ws/audio    Binary PCM stream（Float32-LE）

Vue 3 Renderer
  ├─ useBackend.ts   WebSocket singleton
  └─ ScriptProcessorNode → /ws/audio（16kHz Float32）
```

| 層 | 技術 |
|----|------|
| 桌面殼 | Electron 33 + electron-vite |
| 前端 | Vue 3 + TypeScript |
| 後端 | FastAPI + uvicorn（uv 管理） |
| 即時 ASR | mlx-whisper（Apple Silicon）/ faster-whisper（CPU） |
| 終版 ASR | Breeze-ASR-25（MediaTek Research） |
| VAD | Silero VAD 5.1 |
| LLM 校正 | llama-cpp-python（GGUF）或 OpenAI-compatible API |

---

## License

[MIT](LICENSE)
