import { spawn, type ChildProcess } from 'node:child_process'
import { createServer } from 'node:net'
import { app } from 'electron'

export interface BackendHandle {
  host: string
  port: number
  proc: ChildProcess
  stop: () => Promise<void>
}

function findFreePort(): Promise<number> {
  return new Promise((resolve, reject) => {
    const srv = createServer()
    srv.unref()
    srv.on('error', reject)
    srv.listen(0, '127.0.0.1', () => {
      const addr = srv.address()
      if (addr && typeof addr === 'object') {
        const port = addr.port
        srv.close(() => resolve(port))
      } else {
        reject(new Error('failed to allocate port'))
      }
    })
  })
}

export async function startBackend(): Promise<BackendHandle> {
  const port = await findFreePort()
  const host = '127.0.0.1'
  const projectRoot = app.getAppPath()

  const proc = spawn(
    'uv',
    ['--project', 'backend', 'run', 'python', '-m', 'meeting_minutes_backend', '--port', String(port)],
    {
      cwd: projectRoot,
      env: { ...process.env },
      stdio: ['ignore', 'pipe', 'pipe'],
      // detached=true 讓 uv 及其所有子孫（python uvicorn、MLX worker）
      // 共用同一個 process group，之後用 -pid kill 可一次全殺
      detached: true
    }
  )
  // 把 backend 訊息轉印到 main process stdio。
  // 用 try/catch 包起：parent stdout/stderr 在 pnpm dev 背景 pipe 被關時會 EPIPE，
  // 該錯誤不該讓 main process crash。
  const safeWrite = (stream: NodeJS.WriteStream, msg: string): void => {
    try {
      stream.write(msg)
    } catch {
      /* parent pipe closed (EPIPE / EBADF) — 忽略不影響 backend 運作 */
    }
  }
  // 額外註冊 error listener，避免 Node 把 EPIPE 升為 uncaughtException
  process.stdout.on('error', () => { /* swallow */ })
  process.stderr.on('error', () => { /* swallow */ })
  proc.stdout?.on('data', (chunk: Buffer) => {
    safeWrite(process.stdout, `[backend] ${chunk.toString()}`)
  })
  proc.stderr?.on('data', (chunk: Buffer) => {
    safeWrite(process.stderr, `[backend] ${chunk.toString()}`)
  })

  const handle: BackendHandle = {
    host,
    port,
    proc,
    stop: () =>
      new Promise<void>((resolve) => {
        if (proc.exitCode !== null) {
          resolve()
          return
        }
        proc.once('exit', () => resolve())
        // -pid（負號）= 發給整個 process group（uv + python uvicorn + MLX worker）
        try { process.kill(-proc.pid!, 'SIGTERM') } catch { proc.kill('SIGTERM') }
        setTimeout(() => {
          if (proc.exitCode === null) {
            try { process.kill(-proc.pid!, 'SIGKILL') } catch { proc.kill('SIGKILL') }
          }
        }, 3000)
      })
  }

  await waitForHealth(host, port, 20_000)
  return handle
}

async function waitForHealth(host: string, port: number, timeoutMs: number): Promise<void> {
  const deadline = Date.now() + timeoutMs
  let lastErr: unknown = null
  while (Date.now() < deadline) {
    try {
      const res = await fetch(`http://${host}:${port}/health`)
      if (res.ok) return
      lastErr = new Error(`/health responded ${res.status}`)
    } catch (err) {
      lastErr = err
    }
    await new Promise((r) => setTimeout(r, 250))
  }
  throw new Error(`backend did not become healthy within ${timeoutMs}ms: ${String(lastErr)}`)
}
