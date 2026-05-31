<script setup lang="ts">
import { computed, nextTick, onMounted, onUnmounted, ref, watch } from 'vue'
import { useBackend } from './composables/useBackend'
import SettingsDialog from './components/SettingsDialog.vue'

interface AudioDevice {
  id: string
  name: string
  channels: number
  default_sample_rate: number
}

interface CorrectionStatus {
  enabled: boolean
  queue_size: number      // queue 內待批 segment 數
  buffered: number        // worker buffer 累積（不滿一批的）
  corrected_count: number // 累計校正過的行數
}

interface StreamState {
  status: string
  output_dir: string | null
  started_at: number | null
  segment_count: number
  backlog: number
  error: string | null
  correction?: CorrectionStatus
}

interface TranscriptSegment {
  start: number | null
  end: number | null
  speaker: string | null
  text: string                    // 原文（永不變）
  source_chunk?: string
  // 後端分配的穩定 id
  line_id?: number
  is_complete?: boolean
}

// LLM 重組後的校正內容（一筆可涵蓋多個原始 line_id）
interface CorrectionEntry {
  line_ids: number[]              // 涵蓋的原始 line_id 範圍
  text: string                    // 重組後文字
  ts: number                      // 收到時間（用於排序、scroll）
}

interface AudioSource {
  id: string
  kind: 'mic' | 'display' | 'device'
  deviceId?: string   // mic / device 用
  label: string
  gain: number        // 0.0 ~ 2.0
  muted: boolean
}

// ---------------------------------------------------------------------------
// Backend connection
// ---------------------------------------------------------------------------
const backend = useBackend()

// Derive the audio WebSocket URL from the JSON command WebSocket URL.
// e.g. ws://127.0.0.1:PORT/ws  →  ws://127.0.0.1:PORT/ws/audio
const wsAudioUrl = computed(() =>
  backend.wsUrl.value ? backend.wsUrl.value.replace(/\/ws$/, '/ws/audio') : ''
)

// ---------------------------------------------------------------------------
// UI state
// ---------------------------------------------------------------------------
const devices = ref<AudioDevice[]>([])
const selectedDevice = ref('')
const outputDir = ref('')
const segmentSeconds = ref(0.5)
const generateSummary = ref(true)
// 分軌：mic=你(左聲道), 系統音/裝置=對方(右聲道)，逐字稿標記講者
const dualTrack = ref(true)
// 'auto' = 中文 + 英文（自動偵測），'zh' = 純中文，'en' = English only
const selectedLanguage = ref<'auto' | 'zh' | 'en'>('auto')
const segments = ref<TranscriptSegment[]>([])
const livePreview = ref('')
const transcriptListRef = ref<HTMLElement | null>(null)
const correctionListRef = ref<HTMLElement | null>(null)

// auto-scroll：只在使用者未往上滾時才自動捲到底
const transcriptAtBottom = ref(true)
const correctionAtBottom = ref(true)
const SCROLL_THRESHOLD = 60 // px，距底部多少以內視為「在底部」

function checkAtBottom(el: HTMLElement): boolean {
  return el.scrollHeight - el.scrollTop - el.clientHeight <= SCROLL_THRESHOLD
}

function scrollToBottom(el: HTMLElement | null, isAtBottom: boolean): void {
  if (el && isAtBottom) el.scrollTop = el.scrollHeight
}

// 已校正（重組）的內容：依 line_ids 第一個排序
const corrections = ref<CorrectionEntry[]>([])
// 後端已處理但無需修改的 line_id（clean 訊號）→ 從 pending 轉為「已確認原文」
const cleanLineIds = ref<Set<number>>(new Set())
// 滾動摘要
const currentSummary = ref<{ topic: string; keywords: string[] } | null>(null)

function formatLineIdsRange(ids: number[]): string {
  if (ids.length === 0) return ''
  if (ids.length === 1) return `L${ids[0]}`
  const sorted = [...ids].sort((a, b) => a - b)
  const isContinuous = sorted.every((v, i) => i === 0 || v === sorted[i - 1] + 1)
  return isContinuous ? `L${sorted[0]}-L${sorted[sorted.length - 1]}` : sorted.map(v => `L${v}`).join(', ')
}

// 即時鏡像條目：每個 segment 在下方有對應，pending=灰原文 / corrected=藍校正版
// 當校正覆蓋多 line_ids（合併），只在第一個 line_id 留一個 corrected 條目，其他被吸收
interface MirrorEntry {
  primary: number             // 主 line_id（陣列最小值）
  line_ids: number[]          // 涵蓋範圍
  state: 'pending' | 'corrected' | 'clean'
  text: string                // 顯示文字（corrected 時為校正後）
  originalText?: string       // 原文（corrected 時才有，多行以 " / " 合併）
  start: number | null
  end: number | null
}

const mirrorEntries = computed<MirrorEntry[]>(() => {
  // 1. 建 line_id → correction map（每個被涵蓋的 line_id 都指向同一個校正）
  const corrMap = new Map<number, CorrectionEntry>()
  for (const c of corrections.value) {
    for (const lid of c.line_ids) corrMap.set(lid, c)
  }

  // 2. 走 segments，分配 entries
  const out: MirrorEntry[] = []
  const handled = new Set<number>()
  for (const seg of segments.value) {
    if (seg.line_id === undefined) continue
    if (handled.has(seg.line_id)) continue

    const corr = corrMap.get(seg.line_id)
    if (corr) {
      const primary = Math.min(...corr.line_ids)
      // 只在 primary 那個 segment 上推一筆 entry（避免重複）
      if (seg.line_id === primary) {
        const firstSeg = segments.value.find((s) => s.line_id === primary)
        const lastLid = Math.max(...corr.line_ids)
        const lastSeg = segments.value.find((s) => s.line_id === lastLid)
        const sortedIds = [...corr.line_ids].sort((a, b) => a - b)
        const originalText = sortedIds
          .map((lid) => segments.value.find((s) => s.line_id === lid)?.text ?? '')
          .filter(Boolean)
          .join(' / ')
        out.push({
          primary,
          line_ids: sortedIds,
          state: 'corrected',
          text: corr.text,
          originalText: originalText || undefined,
          start: firstSeg?.start ?? seg.start,
          end: lastSeg?.end ?? seg.end,
        })
      }
      for (const lid of corr.line_ids) handled.add(lid)
    } else {
      out.push({
        primary: seg.line_id,
        line_ids: [seg.line_id],
        state: cleanLineIds.value.has(seg.line_id) ? 'clean' : 'pending',
        text: seg.text,
        start: seg.start,
        end: seg.end,
      })
      handled.add(seg.line_id)
    }
  }
  return out
})
const logs = ref<string[]>([])
const starting = ref(false)
const warmingUp = ref(false)   // ASR 暖機中（尚未 ready）
const streamState = ref<StreamState>({
  status: 'idle',
  output_dir: null,
  started_at: null,
  segment_count: 0,
  backlog: 0,
  error: null
})

const APP_VERSION = '0.4.1'

// ---------------------------------------------------------------------------
// LLM 可用狀態（連線後讀一次 settings，錄音前就能顯示）
// ---------------------------------------------------------------------------
interface LlmStatus {
  backend: 'local' | 'api'
  hasKey: boolean
  model: string
}
const llmStatus = ref<LlmStatus | null>(null)

const llmStatusLabel = computed(() => {
  if (!llmStatus.value) return null
  const { backend, hasKey, model } = llmStatus.value
  if (backend === 'api') {
    return hasKey
      ? { text: `API · ${model}`, state: 'ok' }
      : { text: 'API key 未設定', state: 'warn' }
  }
  return { text: `本地 · ${model}`, state: 'ok' }
})

const isRunning = computed(() =>
  starting.value ||
  ['recording', 'transcribing', 'stopping', 'summarizing', 'correcting'].includes(streamState.value.status)
)

// 開始收音前提：必須已選擇語言（always true since selectedLanguage has a default）
const canStart = computed(() => !isRunning.value && !!selectedLanguage.value && backend.status.value === 'connected')

// ---------------------------------------------------------------------------
// Recording elapsed timer
// ---------------------------------------------------------------------------
const nowTs = ref(Date.now())
let _timerHandle: ReturnType<typeof setInterval> | null = null

const elapsedLabel = computed(() => {
  const startedAt = streamState.value.started_at
  if (!startedAt || !isRunning.value) return null
  const secs = Math.max(0, Math.floor((nowTs.value / 1000) - startedAt))
  const m = Math.floor(secs / 60).toString().padStart(2, '0')
  const s = (secs % 60).toString().padStart(2, '0')
  return `${m}:${s}`
})
const statusLabel = computed(() => {
  const map: Record<string, string> = {
    idle: '待命',
    recording: '收音中',
    transcribing: '轉錄中',
    stopping: '停止中',
    summarizing: '摘要中',
    correcting: 'Breeze 校正中',
    error: '錯誤'
  }
  return map[streamState.value.status] ?? streamState.value.status
})

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------
function addLog(message: string): void {
  const stamp = new Date().toLocaleTimeString('zh-TW', { hour12: false })
  const startedAt = streamState.value.started_at
  const elapsedSec = startedAt ? Math.max(0, Math.floor(Date.now() / 1000 - startedAt)) : null
  const elapsedTag = elapsedSec !== null
    ? `+${Math.floor(elapsedSec / 60).toString().padStart(2, '0')}:${(elapsedSec % 60).toString().padStart(2, '0')}`
    : null
  const prefix = elapsedTag ? `[${stamp}][${elapsedTag}]` : `[${stamp}]`
  logs.value.unshift(`${prefix} ${message}`)
  logs.value = logs.value.slice(0, 80)
}

function formatTime(value: number | null): string {
  if (value === null || Number.isNaN(value)) return '--:--'
  const total = Math.max(0, Math.floor(value))
  const minutes = Math.floor(total / 60)
  const seconds = total % 60
  return `${minutes.toString().padStart(2, '0')}:${seconds.toString().padStart(2, '0')}`
}

// ---------------------------------------------------------------------------
// Device listing — uses browser API so no backend round-trip is needed
// ---------------------------------------------------------------------------
// audioinput 裝置（麥克風 + BlackHole 等虛擬裝置）— devices 已在 UI state 區宣告
// audiooutput 裝置（喇叭 / 耳機），僅供顯示參考，無法直接被 getUserMedia 捕捉
const outputDevices = ref<AudioDevice[]>([])

async function refreshDevices(): Promise<void> {
  try {
    const infos = await navigator.mediaDevices.enumerateDevices()
    const inputs = infos.filter((d) => d.kind === 'audioinput')
    const outputs = infos.filter((d) => d.kind === 'audiooutput')
    devices.value = inputs.map((d, i) => ({
      id: d.deviceId,
      name: d.label || `Microphone ${i + 1}`,
      channels: 1,
      default_sample_rate: 16000
    }))
    outputDevices.value = outputs.map((d, i) => ({
      id: d.deviceId,
      name: d.label || `Speaker ${i + 1}`,
      channels: 2,
      default_sample_rate: 48000
    }))
    // 初次取得裝置後初始化預設來源：麥克風 + 系統音效（會議模式）
    if (audioSources.value.length === 0 && devices.value.length > 0) {
      const def = devices.value[0]
      audioSources.value = [
        {
          id: 'mic-default',
          kind: 'mic',
          deviceId: def.id,
          label: def.name,
          gain: 1.0,
          muted: false,
        },
        {
          id: 'display',
          kind: 'display',
          label: '系統音效（螢幕）',
          gain: 0.4,
          muted: false,
        },
      ]
    }
  } catch (err) {
    addLog(`無法取得裝置清單：${String(err)}`)
  }
}

// ---------------------------------------------------------------------------
// Mic capture state
// ---------------------------------------------------------------------------
let micAudioCtx: AudioContext | null = null
let micProcessor: ScriptProcessorNode | null = null
let audioWs: WebSocket | null = null

// 多來源音訊：每個來源獨立 stream + GainNode
const audioSources = ref<AudioSource[]>([])
const activeStreams = new Map<string, MediaStream>()   // source.id → MediaStream
const activeGainNodes = new Map<string, GainNode>()    // source.id → GainNode

const hasDisplaySource = computed(() => audioSources.value.some(s => s.kind === 'display'))
// 分軌需同時有「你」(mic) 與「對方」(系統音/裝置) 兩類來源才有意義
const canDualTrack = computed(() =>
  audioSources.value.some(s => s.kind === 'mic') &&
  audioSources.value.some(s => s.kind === 'display' || s.kind === 'device')
)

/** Open AudioContext + ScriptProcessorNode + 所有 audioSources + audio WebSocket. */
async function startMicCapture(): Promise<void> {
  const audioUrl = wsAudioUrl.value
  if (!audioUrl) throw new Error('audio WebSocket URL not ready')

  // 分軌：你(mic)→左聲道, 對方(系統音/裝置)→右聲道，以 stereo 交錯傳給後端拆軌
  const dual = dualTrack.value && canDualTrack.value

  micAudioCtx = new AudioContext({ sampleRate: 16000 })
  // ScriptProcessorNode: 4096 frames @ 16 kHz = 256 ms per callback
  // 分軌時 2 in/out channels；混音時 1 in/out（原本行為）
  micProcessor = micAudioCtx.createScriptProcessor(4096, dual ? 2 : 1, dual ? 2 : 1)
  // 分軌時用 ChannelMerger 把兩類來源各併到一個聲道
  const merger = dual ? micAudioCtx.createChannelMerger(2) : null

  for (const src of audioSources.value) {
    try {
      let stream: MediaStream

      if (src.kind === 'mic') {
        try {
          stream = await navigator.mediaDevices.getUserMedia({
            audio: {
              channelCount: 1,
              sampleRate: 16000,
              echoCancellation: false,
              noiseSuppression: false,
              autoGainControl: false,
              ...(src.deviceId ? { deviceId: { exact: src.deviceId } } : {}),
            },
            video: false,
          })
        } catch (err) {
          const isPermission =
            err instanceof Error &&
            (err.name === 'NotAllowedError' || err.name === 'PermissionDeniedError')
          if (isPermission) {
            void window.meetingMinutes?.requestMicAccess?.()
            throw new Error(
              '麥克風存取被拒絕。已為您開啟系統設定，請在「隱私權與安全性 → 麥克風」中找到 Electron 並開啟開關，然後重新點「開始收音」。'
            )
          }
          throw err
        }
        // 取得麥克風權限後刷新裝置標籤
        void refreshDevices()

      } else if (src.kind === 'display') {
        // video 必須帶上（部分 Chromium 版本不允許純 audio getDisplayMedia）
        const displayStream = await navigator.mediaDevices.getDisplayMedia({
          audio: {
            systemAudio: 'include',
            suppressLocalAudioPlayback: false,
          } as MediaTrackConstraints,
          video: { frameRate: 1 } as MediaTrackConstraints,
        })
        displayStream.getVideoTracks().forEach((t) => t.stop())
        const tracks = displayStream.getAudioTracks()
        if (tracks.length === 0) throw new Error('getDisplayMedia 未回傳音訊 track')
        stream = new MediaStream(tracks)

      } else {
        // device（BlackHole 等虛擬裝置）
        stream = await navigator.mediaDevices.getUserMedia({
          audio: {
            deviceId: { exact: src.deviceId },
            channelCount: 1,
            sampleRate: 16000,
            echoCancellation: false,
            noiseSuppression: false,
            autoGainControl: false,
          },
          video: false,
        })
      }

      activeStreams.set(src.id, stream)
      const mediaNode = micAudioCtx.createMediaStreamSource(stream)
      const gainNode = micAudioCtx.createGain()
      gainNode.gain.value = src.muted ? 0 : src.gain
      activeGainNodes.set(src.id, gainNode)
      mediaNode.connect(gainNode)
      if (merger) {
        // mic→input 0(你/左)，系統音/裝置→input 1(對方/右)
        gainNode.connect(merger, 0, src.kind === 'mic' ? 0 : 1)
      } else {
        gainNode.connect(micProcessor)
      }

    } catch (err) {
      addLog(`來源「${src.label}」開啟失敗（${String(err)}），跳過`)
      if (src.kind === 'mic' && err instanceof Error && err.message.includes('拒絕')) throw err
    }
  }

  if (merger) merger.connect(micProcessor)

  // Open the binary WebSocket for PCM streaming
  const ws = new WebSocket(audioUrl)
  ws.binaryType = 'arraybuffer'
  audioWs = ws

  await new Promise<void>((resolve, reject) => {
    const timer = setTimeout(() => reject(new Error('audio WebSocket connect timeout')), 5000)
    ws.addEventListener('open', () => { clearTimeout(timer); resolve() })
    ws.addEventListener('error', () => { clearTimeout(timer); reject(new Error('audio WebSocket error')) })
  })

  micProcessor.onaudioprocess = (e) => {
    if (ws.readyState !== WebSocket.OPEN) return
    if (dual) {
      // 交錯成 [L0,R0,L1,R1,...] stereo Float32，後端依聲道拆軌
      const l = e.inputBuffer.getChannelData(0)
      const r = e.inputBuffer.getChannelData(1)
      const n = l.length
      const inter = new Float32Array(n * 2)
      for (let i = 0; i < n; i++) {
        inter[2 * i] = l[i]
        inter[2 * i + 1] = r[i]
      }
      ws.send(inter.buffer)
    } else {
      ws.send(new Float32Array(e.inputBuffer.getChannelData(0)).buffer)
    }
  }

  // ScriptProcessorNode must be connected to a destination to avoid GC
  micProcessor.connect(micAudioCtx.destination)
}

/** Stop mic capture and close the audio WebSocket. */
function stopMicCapture(): void {
  micProcessor?.disconnect()
  micProcessor = null
  for (const stream of activeStreams.values()) {
    stream.getTracks().forEach((t) => t.stop())
  }
  activeStreams.clear()
  activeGainNodes.clear()
  micAudioCtx?.close()
  micAudioCtx = null
  audioWs?.close()
  audioWs = null
}

// ---------------------------------------------------------------------------
// Audio source management
// ---------------------------------------------------------------------------
function addMicSource(): void {
  const dev = devices.value[0]
  if (!dev) return
  audioSources.value = [...audioSources.value, {
    id: `mic-${Date.now()}`,
    kind: 'mic',
    deviceId: dev.id,
    label: dev.name,
    gain: 1.0,
    muted: false,
  }]
}

function addDisplaySource(): void {
  if (hasDisplaySource.value) return
  audioSources.value = [...audioSources.value, {
    id: 'display',
    kind: 'display',
    label: '系統音效（螢幕）',
    gain: 0.4,
    muted: false,
  }]
}

function addDeviceSource(): void {
  const usedIds = new Set(audioSources.value.map(s => s.deviceId))
  const dev = devices.value.find(d => !usedIds.has(d.id))
  if (!dev) { addLog('沒有可用的未使用裝置'); return }
  audioSources.value = [...audioSources.value, {
    id: `device-${dev.id}`,
    kind: 'device',
    deviceId: dev.id,
    label: dev.name,
    gain: 0.5,
    muted: false,
  }]
}

function removeSource(id: string): void {
  if (audioSources.value.length <= 1) return
  audioSources.value = audioSources.value.filter(s => s.id !== id)
}

function updateSourceGain(id: string, gain: number): void {
  const src = audioSources.value.find(s => s.id === id)
  if (!src) return
  src.gain = gain
  if (gain > 0) src.muted = false
  // 錄音中即時生效
  const gainNode = activeGainNodes.get(id)
  if (gainNode) gainNode.gain.value = src.muted ? 0 : gain
}

function sliderTrackStyle(src: AudioSource): Record<string, string> {
  const pct = ((src.muted ? 0 : src.gain) / 2) * 100
  return { '--fill-pct': `${pct}%` }
}

function changeSourceDevice(id: string, deviceId: string): void {
  const src = audioSources.value.find(s => s.id === id)
  if (!src) return
  const dev = devices.value.find(d => d.id === deviceId)
  src.deviceId = deviceId
  if (dev) src.label = dev.name
}

function toggleMute(id: string): void {
  const src = audioSources.value.find(s => s.id === id)
  if (!src) return
  src.muted = !src.muted
  const gainNode = activeGainNodes.get(id)
  if (gainNode) gainNode.gain.value = src.muted ? 0 : src.gain
}

// ---------------------------------------------------------------------------
// Recording control
// ---------------------------------------------------------------------------
async function pickOutputDir(): Promise<void> {
  const path = await window.meetingMinutes?.pickOutputDir?.()
  if (path) outputDir.value = path
}

async function startStream(): Promise<void> {
  segments.value = []
  corrections.value = []
  cleanLineIds.value = new Set()
  currentSummary.value = null
  livePreview.value = ''
  starting.value = true
  addLog('正在啟動收音')
  try {
    // 0. 確認 ASR 模型已暖機完成，否則等待
    const asrResp = await backend.send<{ ready: boolean }>('stream.asr_ready')
    if (asrResp.ok && !asrResp.payload?.ready) {
      warmingUp.value = true
      addLog('ASR 模型暖機中，請稍候...')
      // 每秒輪詢直到 ready（最多等 60s）
      const deadline = Date.now() + 60_000
      while (Date.now() < deadline) {
        await new Promise(r => setTimeout(r, 1000))
        const check = await backend.send<{ ready: boolean }>('stream.asr_ready')
        if (check.ok && check.payload?.ready) break
      }
      warmingUp.value = false
      addLog('ASR 模型準備完成，開始收音')
    }

    // 1. Open mic + audio WebSocket (getUserMedia → permission granted here)
    await startMicCapture()
    const srcLabels = audioSources.value
      .filter(s => activeStreams.has(s.id))
      .map(s => s.label)
      .join(' + ')
    addLog(`音訊來源已開啟：${srcLabels}（${micAudioCtx?.sampleRate ?? '?'} Hz）`)

    // 2. Tell the backend to prepare the ASR pipeline
    const resp = await backend.send<{ state: StreamState }>('stream.start', {
      output_dir: outputDir.value || null,
      segment_seconds: segmentSeconds.value,

      // 'auto' 字串顯式傳遞給後端，後端會解為 None 給 Whisper 真正自動偵測
      // （之前傳 null 會被 faster_asr.py 的 env-var fallback 硬塞成 'zh' → 強制中文 → 英文片段觸發幻覺迴圈）
      language: selectedLanguage.value,
      dual_track: dualTrack.value && canDualTrack.value,
    })
    if (!resp.ok) throw new Error(resp.error?.message ?? 'stream.start failed')
    if (resp.payload?.state) streamState.value = resp.payload.state
    addLog('已開始收音')
  } catch (err) {
    stopMicCapture()
    addLog(`開始收音失敗：${String((err as Error).message ?? err)}`)
  } finally {
    starting.value = false
  }
}

async function stopStream(): Promise<void> {
  try {
    // Stop mic first — this closes the audio WebSocket which triggers
    // notify_recording_ended() on the backend (tail flush + stop signal).
    stopMicCapture()
    addLog('已停止麥克風收音')
    // Also send the explicit stop command so status updates propagate.
    await backend.send('stream.stop')
    addLog('已送出停止收音')
  } catch (err) {
    addLog(`停止失敗：${String((err as Error).message ?? err)}`)
  }
}

async function openOutput(): Promise<void> {
  const dir = streamState.value.output_dir
  if (dir) await window.meetingMinutes?.openPath?.(dir)
}

function resetUI(): void {
  segments.value = []
  corrections.value = []
  cleanLineIds.value = new Set()
  currentSummary.value = null
  livePreview.value = ''
  logs.value = []
  addLog('畫面已清空，可以開始新的錄音')
}

// ---------------------------------------------------------------------------
// Backend event listeners
// ---------------------------------------------------------------------------
backend.on('stream.state', (payload) => {
  const event = payload as { state?: StreamState; message?: string }
  if (event.state) streamState.value = event.state
})

backend.on('transcript.segment', (payload) => {
  const event = payload as { segment?: TranscriptSegment }
  if (event.segment) {
    segments.value.push(event.segment)
    livePreview.value = ''
    nextTick(() => {
      scrollToBottom(transcriptListRef.value, transcriptAtBottom.value)
      scrollToBottom(correctionListRef.value, correctionAtBottom.value)
    })
  }
})

backend.on('transcript.preview', (payload) => {
  const event = payload as { text?: string }
  if (typeof event.text === 'string') livePreview.value = event.text
})

// 預覽行出現／更新時補捲到底，確保最後一行可見
watch(livePreview, () => {
  nextTick(() => {
    scrollToBottom(transcriptListRef.value, transcriptAtBottom.value)
  })
})

// 收音停止後清空預覽（避免殘留文字在 stopping→idle 期間繼續顯示）
watch(streamState, (newState) => {
  if (newState && ['idle', 'error'].includes(newState.status)) {
    livePreview.value = ''
  }
})

backend.on('stream.log', (payload) => {
  const event = payload as { message?: string }
  if (event.message) addLog(event.message)
})

backend.on('transcript.correction', (payload) => {
  // 後端送來重組後的校正：可能涵蓋多個原始 line_ids。
  // 上方逐字稿維持原文不動；下方校正面板新增一筆（可能合併多個原行）
  const event = payload as {
    correction?: { line_id?: number; line_ids?: number[]; text: string }
  }
  if (!event.correction) return
  const { line_id, line_ids, text } = event.correction
  const ids = Array.isArray(line_ids) && line_ids.length > 0
    ? line_ids
    : (typeof line_id === 'number' ? [line_id] : [])
  if (ids.length === 0) return

  corrections.value.push({ line_ids: ids, text, ts: Date.now() })

  nextTick(() => {
    scrollToBottom(correctionListRef.value, correctionAtBottom.value)
  })
})

backend.on('transcript.summary', (payload) => {
  const event = payload as { topic?: string; keywords?: string[] }
  if (event.topic || (event.keywords && event.keywords.length > 0)) {
    currentSummary.value = {
      topic: event.topic ?? '',
      keywords: event.keywords ?? [],
    }
  }
})

backend.on('transcript.correction_clean', (payload) => {
  // 後端送來「已處理、無需修改」的 line_ids → 把這些行從 pending 轉為 clean
  const event = payload as { line_ids?: number[] }
  if (!Array.isArray(event.line_ids) || event.line_ids.length === 0) return
  const next = new Set(cleanLineIds.value)
  for (const id of event.line_ids) next.add(id)
  cleanLineIds.value = next
})

// ---------------------------------------------------------------------------
// Lifecycle
// ---------------------------------------------------------------------------
// 設定視窗
const settingsOpen = ref(false)

// ---------------------------------------------------------------------------
// 會議記錄摘要 Modal（錄音結束後獨立觸發，不影響主流程）
// ---------------------------------------------------------------------------
type SummaryModalState = 'hidden' | 'generating' | 'done' | 'error' | 'skipped'
const summaryModal = ref<SummaryModalState>('hidden')
const summaryFilePath = ref('')
const summaryErrorMsg = ref('')

// 偵測 idle：當狀態從「執行中」變為 idle，且使用者有勾選生成摘要，自動彈出 Modal
watch(() => streamState.value.status, async (newStatus, oldStatus) => {
  const wasRunning = ['recording', 'transcribing', 'stopping', 'correcting'].includes(oldStatus ?? '')
  if (wasRunning && newStatus === 'idle' && generateSummary.value) {
    summaryFilePath.value = ''
    summaryErrorMsg.value = ''
    summaryModal.value = 'generating'
    // 發出請求後立即返回（後端收到即 ack），實際結果由 summary.done / summary.error 事件推送
    backend.send('summary.generate').catch((err) => {
      summaryErrorMsg.value = String(err)
      summaryModal.value = 'error'
    })
  }
})

// 後端完成後推送的事件
backend.on('summary.done', (payload) => {
  const event = payload as { path?: string }
  summaryFilePath.value = event.path ?? ''
  summaryModal.value = 'done'
})

backend.on('summary.error', (payload) => {
  const event = payload as { message?: string }
  const msg = event.message ?? '未知錯誤'
  if (msg.includes('api_key') || msg.includes('backend') || msg.includes('API Key')) {
    summaryModal.value = 'skipped'
    summaryErrorMsg.value = msg
  } else {
    summaryErrorMsg.value = msg
    summaryModal.value = 'error'
  }
})

async function openMinutesFile(): Promise<void> {
  if (summaryFilePath.value) await window.meetingMinutes?.openPath?.(summaryFilePath.value)
}

function triggerSummaryManually(): void {
  summaryFilePath.value = ''
  summaryErrorMsg.value = ''
  summaryModal.value = 'generating'
  backend.send('summary.generate').catch((err) => {
    summaryErrorMsg.value = String(err)
    summaryModal.value = 'error'
  })
}

async function fetchLlmStatus(): Promise<void> {
  try {
    const r = await backend.send<{ values: Record<string, unknown> }>('settings.get')
    if (!r.ok || !r.payload?.values) return
    const v = r.payload.values as Record<string, unknown>
    const bk = (v['correction.backend'] as string) ?? 'local'
    const apiKey = (v['correction.api_key'] as string) ?? ''
    const apiModel = (v['correction.api_model'] as string) ?? 'gpt-4o-mini'
    const localFile = Array.isArray(v['correction.model_file'])
      ? String(v['correction.model_file'][0])
      : 'GGUF'
    llmStatus.value = {
      backend: bk === 'api' ? 'api' : 'local',
      hasKey: apiKey.trim().length > 0,
      model: bk === 'api' ? apiModel : localFile,
    }
  } catch {
    // 讀取失敗不影響其他功能
  }
}

watch(() => backend.status.value, async (s) => {
  if (s === 'connected') await fetchLlmStatus()
})

onMounted(async () => {
  await refreshDevices()
  _timerHandle = setInterval(() => { nowTs.value = Date.now() }, 500)
})

onUnmounted(() => {
  if (_timerHandle !== null) clearInterval(_timerHandle)
})
</script>

<template>
  <main class="app-shell">
    <header class="topbar">
      <div>
        <h1>Meeting Minutes ASR <span class="version-badge">{{ APP_VERSION }}</span></h1>
        <p>Breeze-ASR 近即時會議收音與逐字稿</p>
      </div>
      <div class="topbar-actions">
        <button class="gear-btn" @click="settingsOpen = true" title="進階設定" aria-label="進階設定">
          ⚙️
        </button>
        <div class="connection" :data-status="backend.status.value">
          <span class="dot"></span>
          {{ backend.status.value }}
        </div>
      </div>
    </header>

    <SettingsDialog :open="settingsOpen" @close="settingsOpen = false" />

    <!-- 會議記錄摘要 Modal：錄音結束後獨立彈出，不影響主流程 -->
    <Teleport to="body">
      <div v-if="summaryModal !== 'hidden'" class="modal-backdrop" @click.self="summaryModal = 'hidden'">
        <div class="modal-card">
          <template v-if="summaryModal === 'generating'">
            <div class="modal-icon">📝</div>
            <h2 class="modal-title">正在生成會議記錄摘要</h2>
            <p class="modal-desc">請稍候，AI 整理逐字稿中…</p>
            <div class="progress-bar"><div class="progress-bar__fill"></div></div>
          </template>
          <template v-else-if="summaryModal === 'done'">
            <div class="modal-icon">✅</div>
            <h2 class="modal-title">會議記錄摘要已完成</h2>
            <p class="modal-path">{{ summaryFilePath }}</p>
            <div class="modal-actions">
              <button class="primary" @click="openMinutesFile">開啟 minutes.md</button>
              <button class="secondary" @click="summaryModal = 'hidden'">關閉</button>
            </div>
          </template>
          <template v-else-if="summaryModal === 'skipped'">
            <div class="modal-icon">⚠️</div>
            <h2 class="modal-title">無法生成摘要</h2>
            <p class="modal-desc">{{ summaryErrorMsg || '請至設定確認 correction.backend=api 且填入 API Key' }}</p>
            <div class="modal-actions">
              <button class="secondary" @click="settingsOpen = true; summaryModal = 'hidden'">開啟設定</button>
              <button class="secondary" @click="summaryModal = 'hidden'">關閉</button>
            </div>
          </template>
          <template v-else-if="summaryModal === 'error'">
            <div class="modal-icon">❌</div>
            <h2 class="modal-title">摘要生成失敗</h2>
            <p class="modal-desc">{{ summaryErrorMsg }}</p>
            <div class="modal-actions">
              <button class="secondary" @click="summaryModal = 'hidden'">關閉</button>
            </div>
          </template>
        </div>
      </div>
    </Teleport>

    <section class="workspace">
      <aside class="control-pane">
        <section class="section">
          <div class="section-title">收音控制</div>
          <div class="status-row">
            <span>狀態</span>
            <strong :class="warmingUp ? 'warming' : ''">
              {{ warmingUp ? '⏳ ASR 暖機中' : starting ? '啟動中' : statusLabel }}
            </strong>
          </div>
          <div class="status-row">
            <span>已收音</span>
            <strong class="elapsed">{{ warmingUp ? '等待中...' : (elapsedLabel ?? '--:--') }}</strong>
          </div>
          <div class="status-row">
            <span>段落</span>
            <strong>{{ streamState.segment_count }}</strong>
          </div>
          <div class="status-row">
            <span>佇列</span>
            <strong>{{ streamState.backlog }}</strong>
          </div>
          <!-- LLM 狀態：錄音前顯示可用性，錄音中顯示即時校正進度 -->
          <div v-if="streamState.correction?.enabled" class="status-row correction-status">
            <span>
              ✨ LLM 校正
              <small class="correction-substatus" v-if="(streamState.correction?.queue_size ?? 0) + (streamState.correction?.buffered ?? 0) > 0">
                · 處理中
              </small>
            </span>
            <strong>
              {{ streamState.correction.corrected_count }}
              <small class="correction-pending" v-if="(streamState.correction.queue_size + streamState.correction.buffered) > 0">
                ({{ streamState.correction.queue_size + streamState.correction.buffered }} 待批)
              </small>
            </strong>
          </div>
          <div v-else-if="llmStatusLabel" class="status-row" :data-llm-state="llmStatusLabel.state">
            <span>✨ LLM 校正</span>
            <strong :class="llmStatusLabel.state === 'warn' ? 'llm-warn' : 'llm-ok'">
              {{ llmStatusLabel.text }}
            </strong>
          </div>

          <button class="primary" :disabled="!canStart" @click="startStream">
            開始收音
          </button>
          <button class="danger" :disabled="!isRunning" @click="stopStream">停止</button>
          <button class="secondary" :disabled="isRunning" @click="resetUI">清空</button>
          <button
            v-if="backend.status.value === 'disconnected' || backend.status.value === 'error'"
            class="secondary"
            @click="backend.reconnect()"
          >重新連線</button>
        </section>

        <section class="section">
          <div class="section-title">設定</div>
          <label>
            <span>
              主要語言
              <small class="field-hint">辨識永遠自動偵測，此為冷啟動偏好</small>
            </span>
            <select v-model="selectedLanguage" :disabled="isRunning">
              <option value="auto">🌐 不指定（純自動）</option>
              <option value="zh">🇹🇼 中文（繁體）為主</option>
              <option value="en">🇺🇸 English 為主</option>
            </select>
          </label>
          <!-- 多來源音訊 -->
          <div class="audio-sources-header">
            <span>音訊來源</span>
            <button class="btn-refresh-small" :disabled="isRunning" @click="refreshDevices" title="重新整理裝置">↺</button>
          </div>
          <div class="audio-sources-list">
            <div
              v-for="src in audioSources"
              :key="src.id"
              class="audio-source-row"
            >
              <!-- 第一行：icon + 裝置選擇 + 移除 -->
              <div class="source-row-top">
                <span class="source-kind-icon">{{ src.kind === 'mic' ? '🎙' : src.kind === 'display' ? '🖥️' : '🔌' }}</span>
                <template v-if="src.kind !== 'display'">
                  <select
                    class="source-device-select"
                    :value="src.deviceId"
                    :disabled="isRunning"
                    @change="(e) => changeSourceDevice(src.id, (e.target as HTMLSelectElement).value)"
                  >
                    <option v-for="dev in devices" :key="dev.id" :value="dev.id">{{ dev.name }}</option>
                  </select>
                </template>
                <template v-else>
                  <span class="source-label-text">{{ src.label }}</span>
                </template>
                <button
                  class="source-btn-remove"
                  @click="removeSource(src.id)"
                  :disabled="isRunning || audioSources.length <= 1"
                  title="移除"
                >✕</button>
              </div>
              <!-- 第二行：gain 滑桿 + pct + mute -->
              <div class="source-row-bottom">
                <input
                  type="range" min="0" max="2" step="0.05"
                  :value="src.muted ? 0 : src.gain"
                  class="source-gain-slider"
                  :style="sliderTrackStyle(src)"
                  @input="(e) => updateSourceGain(src.id, parseFloat((e.target as HTMLInputElement).value))"
                />
                <span class="source-gain-pct">{{ src.muted ? '0%' : Math.round(src.gain * 100) + '%' }}</span>
                <button
                  class="source-btn-mute"
                  :class="{ 'is-muted': src.muted }"
                  @click="toggleMute(src.id)"
                  :title="src.muted ? '取消靜音' : '靜音'"
                >{{ src.muted ? '🔇' : '🔊' }}</button>
              </div>
            </div>
          </div>
          <div v-if="!isRunning" class="audio-source-add-row">
            <button class="secondary source-add-btn" @click="addMicSource">+ 麥克風</button>
            <button class="secondary source-add-btn" @click="addDisplaySource" :disabled="hasDisplaySource">+ 系統音效</button>
            <button class="secondary source-add-btn" @click="addDeviceSource">+ 虛擬裝置</button>
          </div>

          <label>
            <span>呼吸停頓秒數</span>
            <input v-model.number="segmentSeconds" :disabled="isRunning" type="number" min="0.1" max="3" step="0.1" />
          </label>

          <label>
            <span>輸出資料夾</span>
            <input v-model="outputDir" :disabled="isRunning" placeholder="預設 data/output/desktop-..." />
          </label>
          <button class="secondary" :disabled="isRunning" @click="pickOutputDir">選擇資料夾</button>

          <label class="toggle">
            <input v-model="generateSummary" :disabled="isRunning" type="checkbox" />
            <span>停止後生成會議記錄摘要</span>
          </label>
          <label class="toggle" :class="{ disabled: !canDualTrack }">
            <input v-model="dualTrack" :disabled="isRunning || !canDualTrack" type="checkbox" />
            <span>分軌標記講者（你／對方）</span>
          </label>
          <p v-if="!canDualTrack" class="hint">需同時加入「麥克風」與「系統音效／虛擬裝置」才能分軌</p>
        </section>

        <section class="section">
          <div class="section-title">輸出</div>
          <p class="path">{{ streamState.output_dir || '尚未開始' }}</p>
          <button class="secondary" :disabled="!streamState.output_dir" @click="openOutput">打開輸出資料夾</button>
        </section>

        <section class="section">
          <div class="section-title">會議記錄摘要</div>
          <button
            class="primary"
            :disabled="!streamState.output_dir || isRunning || summaryModal === 'generating'"
            @click="triggerSummaryManually"
          >{{ summaryModal === 'generating' ? '摘要生成中…' : '生成會議摘要' }}</button>
        </section>
      </aside>

      <section class="main-pane">
        <!-- 上半：即時逐字稿（原文，即時更新） -->
        <div class="pane-half pane-transcript">
          <div class="transcript-header">
            <div>
              <h2>即時逐字稿</h2>
              <p>背景持續收音，ASR 逐段更新</p>
            </div>
            <span v-if="streamState.error" class="error">{{ streamState.error }}</span>
          </div>

          <div ref="transcriptListRef" class="transcript-list"
            @scroll="transcriptAtBottom = checkAtBottom(transcriptListRef!)"
          >
            <div v-if="segments.length === 0 && !livePreview" class="empty">開始收音後，轉錄內容會顯示在這裡。</div>
            <article v-for="(segment, index) in segments" :key="segment.line_id ?? `${segment.source_chunk}-${index}`" class="segment">
              <time>{{ formatTime(segment.start) }} - {{ formatTime(segment.end) }}</time>
              <p class="segment-text">
                <span
                  v-if="segment.speaker"
                  class="speaker-tag"
                  :class="segment.speaker === '你' ? 'speaker-tag--me' : 'speaker-tag--other'"
                >{{ segment.speaker }}</span>{{ segment.text }}
              </p>
            </article>
            <article v-if="livePreview && isRunning" class="segment segment--preview">
              <time>即時預覽</time>
              <p>{{ livePreview }}</p>
            </article>
          </div>
        </div>

        <!-- 下半：LLM 即時鏡像（每個 segment 都有對應，pending → corrected 即時轉變） -->
        <div class="pane-half pane-correction">
          <div class="transcript-header">
            <div>
              <h2>✨ LLM 校正鏡像</h2>
              <p>原文與上方同步出現（灰色等待），LLM 完成後轉成藍色校正版</p>
            </div>
          </div>

          <!-- 滾動摘要 banner -->
          <div v-if="currentSummary" class="summary-banner">
            <span class="summary-icon">🧠</span>
            <div class="summary-content">
              <span v-if="currentSummary.topic" class="summary-topic">{{ currentSummary.topic }}</span>
              <span v-if="currentSummary.keywords.length" class="summary-keywords">
                {{ currentSummary.keywords.slice(0, 12).join('・') }}
              </span>
            </div>
          </div>

          <div ref="correctionListRef" class="transcript-list"
            @scroll="correctionAtBottom = checkAtBottom(correctionListRef!)"
          >
            <div v-if="mirrorEntries.length === 0" class="empty">開始收音後，校正鏡像會在這裡與上方同步。</div>
            <transition-group name="mirror-fade" tag="div">
              <article
                v-for="entry in mirrorEntries"
                :key="`mirror-${entry.primary}`"
                :class="['segment', 'segment--mirror', `segment--${entry.state}`]"
              >
                <time>
                  <span
                    class="line-id"
                    :class="{ 'line-id--merged': entry.line_ids.length > 1 }"
                  >
                    {{ formatLineIdsRange(entry.line_ids) }}
                    <span v-if="entry.line_ids.length > 1" class="merged-badge">合併 {{ entry.line_ids.length }} 行</span>
                  </span>
                </time>
                <div v-if="entry.state === 'corrected' && entry.originalText" class="original-quote">
                  <span class="original-label">原文</span>
                  <span class="original-quote-text">{{ entry.originalText }}</span>
                </div>
                <p class="mirror-text">
                  <span class="mirror-icon">
                    <template v-if="entry.state === 'corrected'">✨</template>
                    <template v-else-if="entry.state === 'clean'">✓</template>
                    <template v-else><span class="pending-dot"></span></template>
                  </span>{{ entry.text }}
                </p>
              </article>
            </transition-group>
          </div>
        </div>
      </section>

      <aside class="log-pane">
        <div class="section-title">事件</div>
        <div class="log-list">
          <p v-if="logs.length === 0">尚無事件</p>
          <p v-for="(log, index) in logs" :key="index">{{ log }}</p>
        </div>
      </aside>
    </section>
  </main>
</template>
