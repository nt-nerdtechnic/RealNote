import { contextBridge, ipcRenderer } from 'electron'

export interface BackendInfo {
  status: 'starting' | 'ready'
  host?: string
  port?: number
  httpUrl?: string
  wsUrl?: string
}

contextBridge.exposeInMainWorld('meetingMinutes', {
  appName: 'Meeting Minutes ASR',
  version: '0.1.0',
  getBackendInfo: (): Promise<BackendInfo> => ipcRenderer.invoke('backend:info'),
  pickOutputDir: (): Promise<string | null> => ipcRenderer.invoke('dialog:pickOutputDir'),
  openPath: (target: string): Promise<{ ok: boolean; revealed?: boolean; error?: string }> =>
    ipcRenderer.invoke('shell:openPath', target),
  requestMicAccess: (): Promise<{ granted: boolean }> => ipcRenderer.invoke('mic:requestAccess')
})
