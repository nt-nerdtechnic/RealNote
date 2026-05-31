# RealNote

**[繁體中文](README.zh-TW.md)** | English

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Platform: macOS](https://img.shields.io/badge/platform-macOS-lightgrey)](https://www.apple.com/macos/)
[![Python](https://img.shields.io/badge/python-3.10%2B-blue)](https://www.python.org/)
[![Node](https://img.shields.io/badge/node-20%2B-green)](https://nodejs.org/)

A local-first AI meeting recorder for macOS. Captures microphone and system audio simultaneously, produces a real-time transcript with speaker labels, applies LLM correction on-the-fly, and generates structured meeting minutes — all on-device. Your recordings never leave your machine.

> **macOS only** (Apple Silicon M1+ recommended for best performance)

---

## Features

- **Three-tier ASR** — tiny preview (300 ms) → mlx-whisper live transcript (Metal GPU) → Breeze-ASR-25 final correction
- **Dual-track speaker labeling** — mic (you) and system audio (other party) captured separately; transcript annotated per speaker
- **Real-time LLM correction** — batches of 3 lines sent to a local GGUF model (Qwen2.5-3B) or any cloud API; fixes typos, adds punctuation, unifies terminology
- **Six-layer hallucination filter** — speech-rate threshold, language whitelist, rolling-context dedup, no_speech_prob, timestamp repair, repeat detection
- **ITN post-processing** — converts spoken Chinese numbers, dates, times, phone numbers, and percentages to written form
- **Meeting summary** — one-click structured `minutes.md` after recording ends (requires API key)
- **Settings UI** — 30+ parameters with schema-driven interface; persisted in `data/settings.json`

---

## Requirements

| Tool | Version | Install |
|------|---------|---------|
| macOS | 13+ | — |
| Node.js | 20+ | [nodejs.org](https://nodejs.org/) |
| pnpm | 9+ | `npm i -g pnpm` |
| Python | 3.10+ | — |
| [uv](https://docs.astral.sh/uv/) | latest | `curl -LsSf https://astral.sh/uv/install.sh \| sh` |

---

## Quick Start

```bash
git clone https://github.com/nt-nerdtechnic/RealNote.git
cd RealNote
bash install.sh   # sets up backend venv, pnpm install, copies config templates
pnpm dev          # launches the Electron app
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

1. **Select audio sources** — mic, system audio (screen loopback), or virtual devices; each with independent gain/mute
2. **Enable dual-track** (default on) — requires both a mic source and a system audio source
3. Click **Start Recording** and wait ~10 s for ASR warm-up
4. Speak — live transcript updates continuously; the right panel shows the LLM-corrected mirror
5. Click **Stop** to end the session

### Output files

```
data/output/<timestamp>/
├── chunks/                              ← raw WAV segments
├── transcript.json / .txt               ← live transcript
├── llm_corrected_transcript.json / .txt ← LLM-corrected version
├── display_log.txt                      ← full event log with correction diffs
└── minutes.md                           ← meeting summary (requires API key)
```

---

## LLM Configuration

Open **⚙️ Settings → ✨ Model** to configure the correction backend.

### Local GGUF (default — no API key needed)

Uses **Qwen2.5-3B-Instruct Q4_K_M** (~1.8 GB) via `llama-cpp-python` with Metal acceleration.  
First-time setup: switch to **Local** mode and click **⬇ Download Model** in the settings dialog.

### Cloud API

Switch to **Cloud API** mode and use the **Quick Apply** dropdown to pre-fill endpoint, model, and format:

| Provider | Format | Free tier |
|----------|--------|-----------|
| OpenAI | OpenAI | — |
| Anthropic | Anthropic | — |
| Groq | OpenAI | ✓ |
| OpenRouter | OpenAI | ✓ (some models) |
| DeepSeek | OpenAI | — |
| MiniMax | Anthropic | — |
| SiliconFlow | OpenAI | ✓ |

`correction.api_format` controls the wire format: `openai` (Chat Completions) or `anthropic` (Messages API).

---

## Security

`data/settings.json` (may contain API keys) and `data/output/` (recordings and transcripts) are listed in `.gitignore` and will never be committed.

- First run: copy `data/settings.example.json` → `data/settings.json` and fill in your key
- `.env` is also excluded — copy `.env.example` and edit as needed
- After forking, scan history with [gitleaks](https://github.com/gitleaks/gitleaks)

---

## Architecture

```
Electron Main (Node.js)
  └─ spawn ──→ Python FastAPI backend (127.0.0.1:random port)
                  ├─ /ws        JSON RPC (control commands + event push)
                  └─ /ws/audio  Binary Float32-LE PCM stream

Vue 3 Renderer
  ├─ useBackend.ts        module-level WebSocket singleton
  └─ ScriptProcessorNode  → /ws/audio (16 kHz Float32)
```

| Layer | Technology |
|-------|-----------|
| Desktop shell | Electron 33 + electron-vite |
| Frontend | Vue 3 + TypeScript |
| Backend | FastAPI + uvicorn (managed by uv) |
| Live ASR | mlx-whisper (Apple Silicon) / faster-whisper (CPU fallback) |
| Final ASR | Breeze-ASR-25 (MediaTek Research) |
| VAD | Silero VAD 5.1 |
| LLM correction | llama-cpp-python (GGUF) or OpenAI / Anthropic compatible API |

---

## License

[MIT](LICENSE)
