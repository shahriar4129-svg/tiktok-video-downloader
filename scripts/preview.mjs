/**
 * One-command production-like preview.
 *
 * `vite preview` only serves static files, so /api/* would 404 and the
 * downloader would look broken. This starts the Flask helper (which owns the
 * API) and Vite preview (which proxies /api to it), waits for both ports, and
 * shuts both down cleanly on Ctrl+C.
 */
import { spawn } from 'node:child_process'
import net from 'node:net'

const apiPort = Number(process.env.API_PORT || 8000)
const webPort = Number(process.env.PORT || 4173)
const host = '127.0.0.1'

function isPortFree(port) {
  return new Promise((resolve) => {
    const server = net.createServer()
    server.once('error', () => resolve(false))
    server.once('listening', () => server.close(() => resolve(true)))
    server.listen(port, host)
  })
}

async function waitForPort(port, timeoutMs = 90000) {
  const deadline = Date.now() + timeoutMs
  while (Date.now() < deadline) {
    const open = await new Promise((resolve) => {
      const socket = net.connect(port, host)
      socket.once('connect', () => {
        socket.destroy()
        resolve(true)
      })
      socket.once('error', () => {
        socket.destroy()
        resolve(false)
      })
    })
    if (open) return true
    await new Promise((resolve) => setTimeout(resolve, 300))
  }
  return false
}

for (const [label, port] of [
  ['API', apiPort],
  ['web', webPort],
]) {
  if (!(await isPortFree(port))) {
    console.error(`${label} port ${port} is already in use. Stop that process or set ${label === 'API' ? 'API_PORT' : 'PORT'} to another port.`)
    process.exit(1)
  }
}

const python = process.platform === 'win32' ? 'python' : 'python3'
const api = spawn(python, ['-u', 'server/app.py'], {
  cwd: process.cwd(),
  env: { ...process.env, HOST: host, PORT: String(apiPort) },
  stdio: ['ignore', 'pipe', 'pipe'],
})
const web = spawn(process.execPath, ['node_modules/vite/bin/vite.js', 'preview', '--host', host, '--port', String(webPort), '--strictPort'], {
  cwd: process.cwd(),
  stdio: ['ignore', 'pipe', 'pipe'],
})

function pipe(stream, prefix, target) {
  stream.setEncoding('utf8')
  stream.on('data', (chunk) => {
    for (const line of chunk.trimEnd().split('\n')) target(`${prefix} ${line}`)
  })
}
pipe(api.stdout, '[api]', console.log)
pipe(api.stderr, '[api]', console.error)
pipe(web.stdout, '[web]', console.log)
pipe(web.stderr, '[web]', console.error)

let stopping = false
function shutdown(code = 0) {
  if (stopping) return
  stopping = true
  api.kill('SIGTERM')
  web.kill('SIGTERM')
  setTimeout(() => process.exit(code), 300).unref()
}
api.once('exit', (code) => {
  if (!stopping) {
    console.error(`API helper exited with code ${code}.`)
    shutdown(code || 1)
  }
})
web.once('exit', (code) => {
  if (!stopping) {
    console.error(`Vite preview exited with code ${code}.`)
    shutdown(code || 1)
  }
})
process.on('SIGINT', () => shutdown(0))
process.on('SIGTERM', () => shutdown(0))

const apiReady = await waitForPort(apiPort)
const webReady = await waitForPort(webPort)
if (!apiReady || !webReady) {
  console.error(`Preview failed to start (api=${apiReady}, web=${webReady}). Run 'npm run build' first.`)
  shutdown(1)
} else {
  console.log('')
  console.log(`Preview ready: http://${host}:${webPort}`)
  console.log(`API helper:   http://${host}:${apiPort}/api/health`)
  console.log('Press Ctrl+C to stop both servers.')
}
