import { app, BrowserWindow, desktopCapturer, dialog, ipcMain, nativeImage, session, shell } from 'electron'
import { join } from 'node:path'
import { startBackend, type BackendHandle } from './backend'

// 設定唯一 process title，讓 pkill -f 'meeting-minutes-asr-electron' 能精確識別
process.title = 'meeting-minutes-asr-electron'

// macOS Dock icon（打包後由 .icns 決定，dev 模式需手動設定）
if (process.platform === 'darwin') {
  app.dock.setIcon(nativeImage.createFromPath(join(__dirname, '../../resources/icon.png')))
}

let backend: BackendHandle | null = null
let mainWindow: BrowserWindow | null = null

function loadWindow(win: BrowserWindow): void {
  if (process.env['ELECTRON_RENDERER_URL']) {
    void win.loadURL(process.env['ELECTRON_RENDERER_URL'])
  } else {
    void win.loadFile(join(__dirname, '../renderer/index.html'))
  }
}

async function createWindow(): Promise<void> {
  const win = new BrowserWindow({
    width: 1280,
    height: 820,
    minWidth: 1080,
    minHeight: 700,
    title: 'Meeting Minutes ASR',
    icon: join(__dirname, '../../resources/icon.png'),
    webPreferences: {
      preload: join(__dirname, '../preload/index.js'),
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: false
    }
  })
  mainWindow = win
  win.on('closed', () => {
    if (mainWindow === win) mainWindow = null
  })
  loadWindow(win)
}

ipcMain.handle('backend:info', () => {
  if (!backend) return { status: 'starting' as const }
  return {
    status: 'ready' as const,
    host: backend.host,
    port: backend.port,
    httpUrl: `http://${backend.host}:${backend.port}`,
    wsUrl: `ws://${backend.host}:${backend.port}/ws`
  }
})

ipcMain.handle('dialog:pickOutputDir', async () => {
  const opts: Electron.OpenDialogOptions = {
    title: 'Choose output folder',
    properties: ['openDirectory', 'createDirectory'],
    buttonLabel: 'Use this folder'
  }
  const result = mainWindow
    ? await dialog.showOpenDialog(mainWindow, opts)
    : await dialog.showOpenDialog(opts)
  if (result.canceled || result.filePaths.length === 0) return null
  return result.filePaths[0]
})

ipcMain.handle('shell:openPath', async (_event, target: string) => {
  if (!target || typeof target !== 'string') return { ok: false, error: 'invalid path' }
  const err = await shell.openPath(target)
  if (err) {
    shell.showItemInFolder(target)
    return { ok: true, revealed: true }
  }
  return { ok: true }
})

// Open System Settings → Privacy → Microphone.
// Called by the renderer after getUserMedia() throws NotAllowedError.
ipcMain.handle('mic:requestAccess', () => {
  if (process.platform === 'darwin') {
    void shell.openExternal(
      'x-apple.systempreferences:com.apple.preference.security?Privacy_Microphone'
    )
  }
  return { granted: false }
})

app.whenReady().then(async () => {
  // Grant microphone permission to the renderer (getUserMedia).
  // Without these handlers Electron may silently deny media access on macOS.
  // 允許麥克風 + 螢幕錄製（getDisplayMedia 需要 display-media 權限）
  session.defaultSession.setPermissionRequestHandler((_webContents, permission, callback) => {
    callback(permission === 'media' || permission === 'display-media')
  })
  session.defaultSession.setPermissionCheckHandler((_webContents, permission) => {
    return permission === 'media' || permission === 'display-media'
  })

  // getDisplayMedia() 在 Electron 中不會自動彈 OS 對話框；
  // 需透過 setDisplayMediaRequestHandler 明確回傳來源。
  // audio: 'loopback' = macOS/Windows 系統音效迴路（全部 app 的輸出聲音）
  // 不需要 BlackHole 或任何虛擬裝置。
  session.defaultSession.setDisplayMediaRequestHandler(async (_request, callback) => {
    try {
      const sources = await desktopCapturer.getSources({ types: ['screen'] })
      if (sources.length > 0) {
        // video 必須提供一個來源；renderer 拿到後立刻 stop，只保留 audio track
        callback({ video: sources[0], audio: 'loopback' })
      } else {
        // 找不到螢幕來源（極罕見），仍嘗試只給音效
        callback({ video: sources[0] ?? null, audio: 'loopback' } as Parameters<typeof callback>[0])
      }
    } catch {
      callback({} as Parameters<typeof callback>[0])
    }
  })

  async function launchBackend(): Promise<void> {
    try {
      backend = await startBackend()
      console.log(`[main] backend ready at ${backend.host}:${backend.port}`)
      // 監聽意外退出（Metal GPU crash 等），自動重啟
      backend.proc.once('exit', (code) => {
        if (backend === null) return  // 正常 stop，不重啟
        console.warn(`[main] backend exited unexpectedly (code=${code}), restarting...`)
        backend = null
        void launchBackend()
      })
    } catch (err) {
      console.error('[main] backend failed to start', err)
    }
  }

  await launchBackend()

  await createWindow()

  app.on('activate', async () => {
    if (BrowserWindow.getAllWindows().length === 0) await createWindow()
  })
})

app.on('window-all-closed', () => {
  // macOS：視窗關閉後留在 Dock 是正常行為，但直接呼叫 quit 確保 backend 被清理。
  // 若想要「留在 Dock」體驗，可改成只在 Dock 沒有點擊時才 quit；
  // 目前選擇直接 quit 以避免 Python 孤兒殘留。
  app.quit()
})

app.on('before-quit', async (e) => {
  if (backend) {
    e.preventDefault()
    const b = backend
    backend = null
    await b.stop()
    app.quit()
  }
})
