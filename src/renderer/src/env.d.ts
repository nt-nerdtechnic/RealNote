/// <reference types="vite/client" />

interface BackendInfo {
  status: 'starting' | 'ready'
  host?: string
  port?: number
  httpUrl?: string
  wsUrl?: string
}

interface Window {
  meetingMinutes?: {
    appName: string
    version: string
    getBackendInfo: () => Promise<BackendInfo>
    pickOutputDir: () => Promise<string | null>
    openPath: (target: string) => Promise<{ ok: boolean; revealed?: boolean; error?: string }>
    requestMicAccess: () => Promise<{ granted: boolean }>
  }
}
