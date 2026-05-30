import { ref, shallowRef } from 'vue'

export type BackendStatus = 'starting' | 'connecting' | 'connected' | 'disconnected' | 'error'

export interface WsRequest<TPayload = Record<string, unknown>> {
  id: string
  type: string
  payload: TPayload
  timestamp: string
}

export interface WsResponse<TPayload = unknown> {
  id: string
  type: string
  ok: boolean
  payload: TPayload | null
  error: { code: string; message: string; details?: Record<string, unknown> } | null
  timestamp: string
}

function nowIso(): string {
  return new Date().toISOString()
}

// Module-level singleton：所有元件共用同一條 WebSocket、同一份狀態。
// 避免每個 useBackend() 呼叫都建立新 WS（會造成連線重複、各自 reconnect）。
let _singleton: ReturnType<typeof _createBackend> | null = null

export function useBackend() {
  if (_singleton === null) _singleton = _createBackend()
  return _singleton
}

function _createBackend() {
  const status = ref<BackendStatus>('starting')
  const wsUrl = ref('')
  const httpUrl = ref('')
  const lastError = ref('')
  const ws = shallowRef<WebSocket | null>(null)
  const pending = new Map<string, (resp: WsResponse) => void>()
  const listeners = new Map<string, Set<(payload: unknown) => void>>()

  let pingTimer: number | null = null
  let reconnectTimer: number | null = null
  let disposed = false

  function emit(type: string, payload: unknown): void {
    const set = listeners.get(type)
    if (!set) return
    for (const cb of set) cb(payload)
  }

  function on(type: string, cb: (payload: unknown) => void): () => void {
    let set = listeners.get(type)
    if (!set) {
      set = new Set()
      listeners.set(type, set)
    }
    set.add(cb)
    return () => set!.delete(cb)
  }

  function send<T = unknown>(type: string, payload: Record<string, unknown> = {}): Promise<WsResponse<T>> {
    return new Promise((resolve, reject) => {
      const socket = ws.value
      if (!socket || socket.readyState !== WebSocket.OPEN) {
        reject(new Error('ws not open'))
        return
      }
      const req: WsRequest = {
        id: crypto.randomUUID(),
        type,
        payload,
        timestamp: nowIso()
      }
      pending.set(req.id, resolve as (resp: WsResponse) => void)
      socket.send(JSON.stringify(req))
      window.setTimeout(() => {
        if (pending.has(req.id)) {
          pending.delete(req.id)
          reject(new Error(`request ${type} timeout`))
        }
      }, 15_000)
    })
  }

  function connect(): void {
    if (!wsUrl.value || disposed) return
    status.value = 'connecting'
    const socket = new WebSocket(wsUrl.value)
    ws.value = socket

    socket.addEventListener('open', () => {
      status.value = 'connected'
      lastError.value = ''
      if (pingTimer !== null) window.clearInterval(pingTimer)
      pingTimer = window.setInterval(() => {
        send('ping', { t: Date.now() }).catch(() => undefined)
      }, 10_000)
    })

    socket.addEventListener('message', (ev) => {
      let msg: WsResponse | (WsRequest & { ok?: undefined })
      try {
        msg = JSON.parse(typeof ev.data === 'string' ? ev.data : '') as WsResponse
      } catch {
        return
      }
      if ('ok' in msg && msg.ok !== undefined && pending.has(msg.id)) {
        const resolve = pending.get(msg.id)!
        pending.delete(msg.id)
        resolve(msg)
        return
      }
      emit(msg.type, (msg as WsRequest).payload)
    })

    socket.addEventListener('close', () => {
      ws.value = null
      if (pingTimer !== null) window.clearInterval(pingTimer)
      if (!disposed) {
        status.value = 'disconnected'
        // 重連前重新 fetch URL（backend 重啟後 port 可能改變）
        reconnectTimer = window.setTimeout(async () => {
          const info = (await window.meetingMinutes?.getBackendInfo?.()) ?? { status: 'starting' }
          if (info.status === 'ready' && info.wsUrl) {
            wsUrl.value = info.wsUrl
            httpUrl.value = info.httpUrl ?? ''
          }
          connect()
        }, 1500)
      }
    })

    socket.addEventListener('error', () => {
      status.value = 'error'
      lastError.value = 'WebSocket error'
    })
  }

  async function init(): Promise<void> {
    const deadline = Date.now() + 20_000
    let info: BackendInfo = { status: 'starting' }
    while (Date.now() < deadline) {
      info = (await window.meetingMinutes?.getBackendInfo?.()) ?? { status: 'starting' }
      if (info.status === 'ready') break
      await new Promise((r) => setTimeout(r, 300))
    }
    if (info.status !== 'ready' || !info.wsUrl) {
      status.value = 'error'
      lastError.value = 'backend did not start'
      return
    }
    wsUrl.value = info.wsUrl
    httpUrl.value = info.httpUrl ?? ''
    connect()
  }

  void init()

  // 移除 onScopeDispose：singleton 不該因為單一元件 unmount 就關閉 WS。
  // 連線一直留到頁面（renderer）關閉，自然會由瀏覽器回收。

  async function reconnect(): Promise<void> {
    if (reconnectTimer !== null) window.clearTimeout(reconnectTimer)
    ws.value?.close()
    status.value = 'connecting'
    const info = (await window.meetingMinutes?.getBackendInfo?.()) ?? { status: 'starting' }
    if (info.status === 'ready' && info.wsUrl) {
      wsUrl.value = info.wsUrl
      httpUrl.value = info.httpUrl ?? ''
    }
    connect()
  }

  return { status, wsUrl, httpUrl, lastError, send, on, reconnect }
}
