# Architecture & Technical Reference

## Architecture Overview

```
Electron Main (Node.js)
  └─ spawn ──→ Python FastAPI backend (127.0.0.1:random port)
                  ├─ /ws        JSON RPC (control commands + event push)
                  └─ /ws/audio  Binary Float32-LE PCM stream

Vue 3 Renderer
  ├─ useBackend.ts        module-level WebSocket singleton
  └─ ScriptProcessorNode  → /ws/audio (16 kHz Float32)
```

### Audio Capture

```
getUserMedia (mic, 16 kHz mono)          → GainNode (you)    ─┐
getDisplayMedia (system audio loopback)  → GainNode (other)  ─┤→ ChannelMerger
                                                               ↓
                                          ScriptProcessorNode (4096 frames = 256 ms)
                                                               ↓
                                          /ws/audio binary WebSocket (Float32-LE)
                                                               ↓
                                          StreamService.push_pcm()
```

### ASR Pipeline

```
push_pcm()
  └─ dual-track de-interleave → _TrackBuf[0] (you) / _TrackBuf[1] (other)
       └─ Silero VAD (per 32 ms frame, sentence-end detection)
            └─ flush chunk_NNNNNN.wav (with 0.5 s overlap)
                 └─ _chunk_queue → _asr_worker (thread)
                      └─ FasterWhisperAsr.transcribe()
                           ├─ MLX path (Apple Silicon): ProcessPoolExecutor subprocess
                           └─ faster-whisper path (CPU fallback)
                      └─ 6-layer hallucination filter → ITN → assign line_id
                           └─ emit transcript.segment

Parallel preview:
  push_pcm() → _preview_buffer (5 s rolling) → every 300 ms → tiny ASR → emit transcript.preview

Parallel LLM correction:
  transcript.segment → CorrectionWorker (batch 3 lines, idle 3 s trigger)
    → local GGUF (llama-cpp-python) or OpenAI/Anthropic-compatible API
    → emit transcript.correction (bound by line_id)
```

---

## Key Design Decisions

### Dual WebSocket
`/ws` handles JSON RPC (request/response + event push). `/ws/audio` carries binary PCM only, avoiding head-of-line blocking on the control channel.

### useBackend module-level singleton
```typescript
let _singleton: ... | null = null
export function useBackend() {
  if (_singleton === null) _singleton = _createBackend()
  return _singleton
}
```
All components share one WebSocket and one event bus. Multiple instances cause duplicate connections and state conflicts.

### MLX subprocess isolation
`mlx_whisper` runs inside `ProcessPoolExecutor(max_workers=1)`. Metal GPU crashes only kill the worker subprocess; the main backend process catches `BrokenProcessPool` and rebuilds the executor automatically.

### Per-track independent state
```
_TrackBuf: PCM buffer / VAD iterator / overlap buffer  (push_pcm side)
_TrackCtx: rolling context / language sticky / speech-rate stats / segments  (_asr_worker side)
```
Mixed mode uses track 0 only. Dual-track mode uses track 0 (mic/you) and track 1 (system audio/other), each fully isolated.

### Six-layer hallucination filter
1. `no_speech_prob > 0.9` → discard; `0.5 < nsp ≤ 0.9` and text < 4 chars → discard
2. Language whitelist (`zh/en/yue`); detections outside the whitelist treated as noise
3. Dynamic speech-rate ceiling: `mean + 5σ` (cold-start: 30 chars/s), hard limit 50 chars/s
4. Rolling-context substring dedup (all language buckets merged to catch loop hallucinations)
5. `_enforce_chronological`: timestamp overlaps shifted forward, preserving duration and text
6. Repeated segment ≥ 3 times in one chunk → whole chunk discarded

### line_id stable binding
Every emitted segment receives a monotonic `line_id` + `is_complete=true`. LLM correction and Breeze final correction both bind results back via `line_id`; the frontend never relies on array index. Multi-line merges use `line_ids[]` to express the range.

---

## Key Files

### Electron / TypeScript

| File | Responsibility |
|------|---------------|
| `src/main/index.ts` | Window management, media/display-media permissions, DisplayMedia loopback handler |
| `src/main/backend.ts` | Spawn Python backend, health polling, graceful kill (process group) |
| `src/preload/index.ts` | contextBridge: `getBackendInfo / pickOutputDir / openPath / requestMicAccess` |
| `src/renderer/src/App.vue` | Full UI + multi-source audio capture + dual-track ChannelMerger |
| `src/renderer/src/composables/useBackend.ts` | Module-level singleton WS + JSON RPC + event bus |
| `src/renderer/src/components/SettingsDialog.vue` | Settings UI with local model download flow |

### Python Backend

| File | Responsibility |
|------|---------------|
| `backend/meeting_minutes_backend/app.py` | FastAPI; `/ws` + `/ws/audio` + message dispatch |
| `backend/meeting_minutes_backend/__main__.py` | Parent-process watchdog (ppid change → os._exit) |
| `backend/meeting_minutes_backend/stream_service.py` | PCM accumulation, VAD segmentation, ASR orchestration, hallucination filtering, line_id assignment |
| `backend/meeting_minutes_backend/faster_asr.py` | MLX / faster-whisper live ASR; ProcessPoolExecutor management |
| `backend/meeting_minutes_backend/asr.py` | Breeze-ASR-25 final correction (HuggingFace Transformers) |
| `backend/meeting_minutes_backend/correction_worker.py` | LLM real-time correction worker (GGUF or API); model download helpers |
| `backend/meeting_minutes_backend/settings.py` | Schema-driven settings system (30+ parameters) |
| `backend/meeting_minutes_backend/itn.py` | Chinese ITN (percentages, phones, dates, times, numbers) |
| `backend/meeting_minutes_backend/transcript.py` | JSON/TXT serialization |
| `backend/meeting_minutes_backend/summarizer.py` | OpenAI / Anthropic API meeting summary generation |
| `backend/meeting_minutes_backend/ipc.py` | WebSocket envelope helpers (make_response / make_error / make_event) |
| `backend/meeting_minutes_backend/glossary_miner.py` | Learn new terms from correction diffs |

---

## Settings Reference

Settings are persisted in `data/settings.json` (gitignored). Changes take effect on the next **Start Recording**.

| Group | Key parameters | Description |
|-------|---------------|-------------|
| ASR | `asr.no_speech_threshold` | Whisper no_speech_prob filter ceiling |
| VAD | `vad.speech_threshold`, `vad.force_flush_seconds` | Silero VAD threshold; forced flush interval |
| Language | `lang.whitelist`, `lang.switch_confirm` | Accepted language codes; switch confirmation count |
| Rate | `rate.fallback`, `rate.sigma`, `rate.hard_limit` | Hallucination speech-rate thresholds |
| LLM | `correction.backend`, `correction.api_key`, `correction.api_model`, `correction.api_format` | `local` or `api`; wire format: `openai` or `anthropic` |
| Preview | `preview.window_seconds`, `preview.interval_seconds` | Rolling preview window and trigger interval |

Full parameter descriptions: open **⚙️ Settings → Advanced** in the app.

---

## Known Limitations

- **macOS only** — `audio: 'loopback'` (getDisplayMedia system audio) and MLX acceleration are macOS-specific.
- **No authentication** — single-user local tool; backend binds to `127.0.0.1` only.
- **Settings not hot-applied** — changes take effect on the next recording session (by design, to avoid mid-session state inconsistency).
- **getDisplayMedia requires re-authorization each session** — system audio permission cannot be persisted.
- **MLX cold-start ~9 s** — mitigated by background warmup; frontend polls `stream.asr_ready` before starting.
- **Non-Apple-Silicon** — falls back to faster-whisper on CPU; `LIVE_ASR_MODEL=small` recommended.
