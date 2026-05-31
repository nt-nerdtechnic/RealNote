# RealNote

**繁體中文** | [English](README.md)

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Platform: macOS](https://img.shields.io/badge/platform-macOS-lightgrey)](https://www.apple.com/macos/)
[![Python](https://img.shields.io/badge/python-3.10%2B-blue)](https://www.python.org/)
[![Node](https://img.shields.io/badge/node-20%2B-green)](https://nodejs.org/)

本地端 AI 會議記錄桌面工具。同時擷取麥克風與系統音效，即時產生雙軌講者逐字稿，LLM 即時校正，錄音結束後一鍵產生結構化會議記錄。全部在本機執行，錄音不離開你的電腦。

> **目前僅支援 macOS**，Apple Silicon（M1 以上）可獲得最佳效能。

---

## 功能

- **三層 ASR 引擎**：tiny 即時預覽（300ms 更新）→ mlx-whisper 即時逐字稿（Apple Silicon Metal 加速）→ Breeze-ASR-25 終版校正
- **雙軌講者標記**：麥克風（你）與系統音效（對方）分軌辨識，逐字稿自動標註講者
- **LLM 即時校正**：每 3 行批次送本地 GGUF 模型（Qwen2.5-3B）或雲端 API 修正錯字、補標點、統一術語
- **反幻覺六層過濾**：語速動態閾值、語言白名單、rolling context 重複比對、no_speech_prob、時間戳重疊修正、chunk 內重複偵測
- **ITN 後處理**：中文數字、日期、時間、電話、百分比自動轉符號
- **會議摘要**：錄音結束後一鍵產生結構化 `minutes.md`（需填入 API key）
- **設定中心**：30+ 參數 schema-driven UI，含 VAD、ASR、LLM、語言切換等，持久化於 `data/settings.json`

---

## 環境需求

| 工具 | 版本 | 安裝方式 |
|------|------|---------|
| macOS | 13+ | — |
| Node.js | 20+ | [nodejs.org](https://nodejs.org/) |
| pnpm | 9+ | `npm i -g pnpm` |
| Python | 3.10+ | — |
| [uv](https://docs.astral.sh/uv/) | 最新 | `curl -LsSf https://astral.sh/uv/install.sh \| sh` |

---

## 快速開始

```bash
git clone https://github.com/nt-nerdtechnic/RealNote.git
cd RealNote
bash install.sh   # 自動建立 backend/.venv、pnpm install、複製設定範本
pnpm dev          # 啟動 Electron App
```

---

## 手動安裝

```bash
pnpm install
uv --project backend sync
cp .env.example .env
cp data/settings.example.json data/settings.json
```

---

## 使用方式

1. **選擇音訊來源**：麥克風、系統音效（螢幕）、虛擬裝置可並存，各自獨立調整 gain / mute
2. **勾選「分軌標記講者」**（預設開啟）：需同時加入麥克風與系統音效來源
3. 按「開始收音」，等待 ASR 暖機完成（約 10 秒）
4. 說話，即時逐字稿持續更新；右側鏡像欄顯示 LLM 校正版本
5. 按「停止」，錄音結束

### 輸出檔案

```
data/output/<timestamp>/
├── chunks/                              ← 原始 WAV 片段
├── transcript.json / .txt               ← 即時逐字稿
├── llm_corrected_transcript.json / .txt ← LLM 校正版
├── display_log.txt                      ← 完整事件紀錄（含校正前後對照）
└── minutes.md                           ← 會議摘要（需 API key）
```

---

## LLM 校正設定

點右上角 **⚙️ → ✨ 模型** 進入設定。

### 本地 GGUF（預設，不需 API key）

使用 **Qwen2.5-3B-Instruct Q4_K_M**（約 1.8 GB），透過 `llama-cpp-python` 在本機 Metal 加速執行。  
首次使用：切換至「🖥 本地」模式，在設定對話框中點擊 **⬇ 下載模型** 按鈕。

### 雲端 API

切換至「☁️ 雲端 API」模式，使用「**快速套用**」下拉選單自動填入 endpoint、模型名稱與格式：

| Provider | 格式 | 免費額度 |
|----------|------|---------|
| OpenAI | OpenAI | — |
| Anthropic | Anthropic | — |
| Groq | OpenAI | ✓ |
| OpenRouter | OpenAI | ✓（部分模型） |
| DeepSeek | OpenAI | — |
| MiniMax | Anthropic | — |
| SiliconFlow | OpenAI | ✓ |

`correction.api_format` 控制傳輸格式：`openai`（Chat Completions）或 `anthropic`（Messages API）。

---

## 安全性

`data/settings.json`（可能含 API key）與 `data/output/`（錄音與逐字稿）已列入 `.gitignore`，**不會被 commit**。

- 首次使用：複製 `data/settings.example.json` → `data/settings.json` 再填入 key
- `.env` 同樣被排除，請複製 `.env.example` 後自行填寫
- Fork 本專案後，可用 [gitleaks](https://github.com/gitleaks/gitleaks) 掃描歷史紀錄

---

## 架構

```
Electron Main (Node.js)
  └─ spawn ──→ Python FastAPI backend (127.0.0.1:隨機 port)
                  ├─ /ws        JSON RPC（控制指令 + 事件推送）
                  └─ /ws/audio  Binary Float32-LE PCM stream

Vue 3 Renderer
  ├─ useBackend.ts        module-level WebSocket singleton
  └─ ScriptProcessorNode  → /ws/audio（16kHz Float32）
```

| 層 | 技術 |
|----|------|
| 桌面殼 | Electron 33 + electron-vite |
| 前端 | Vue 3 + TypeScript |
| 後端 | FastAPI + uvicorn（uv 管理） |
| 即時 ASR | mlx-whisper（Apple Silicon）/ faster-whisper（CPU fallback）|
| 終版 ASR | Breeze-ASR-25（MediaTek Research）|
| VAD | Silero VAD 5.1 |
| LLM 校正 | llama-cpp-python（GGUF）或 OpenAI / Anthropic compatible API |

---

## 授權

[MIT](LICENSE)
