const { app, BrowserWindow, dialog, session, shell } = require('electron')
const fs = require('node:fs')
const path = require('node:path')

const SITE_URL = 'https://tiktokvideodownloaderhd.vercel.app/'
const APP_ID = 'com.tiktokvideodownloaderhd.desktop'

let mainWindow

function isSiteUrl(rawUrl) {
  try {
    const url = new URL(rawUrl)
    return (
      url.protocol === 'https:' &&
      (url.hostname === 'tiktokvideodownloaderhd.vercel.app' ||
        url.hostname === 'www.tiktokvideodownloaderhd.vercel.app') &&
      (url.pathname === '/' || url.pathname.startsWith('/guides') || url.pathname === '/llms.txt')
    )
  } catch {
    return false
  }
}

function uniqueSavePath(directory, filename) {
  const safeName = path.basename(filename).replace(/[<>:"|?*\x00-\x1f]/g, '_') || 'tiktok-download'
  const parsed = path.parse(safeName)
  let candidate = path.join(directory, safeName)
  let counter = 1
  while (fs.existsSync(candidate)) {
    candidate = path.join(directory, `${parsed.name}-${counter}${parsed.ext}`)
    counter += 1
  }
  return candidate
}

function configureDownloads() {
  session.defaultSession.on('will-download', (_event, item) => {
    const suggested = item.getFilename() || 'tiktok-download.mp4'
    const savePath = uniqueSavePath(app.getPath('downloads'), suggested)
    item.setSavePath(savePath)

    item.once('done', (_doneEvent, state) => {
      if (state === 'completed') {
        const window = BrowserWindow.getFocusedWindow()
        if (window && !window.isDestroyed()) {
          window.webContents.send('download-complete', path.basename(savePath))
        }
      } else if (state !== 'cancelled') {
        dialog.showErrorBox(
          'TikTok Video Downloader HD',
          `The file could not be saved.\n\n${savePath}`,
        )
      }
    })
  })
}

function createWindow() {
  mainWindow = new BrowserWindow({
    width: 1180,
    height: 820,
    minWidth: 360,
    minHeight: 520,
    title: 'TikTok Video Downloader HD',
    icon: path.join(__dirname, 'build', 'icon.ico'),
    backgroundColor: '#f1f1f1',
    webPreferences: {
      nodeIntegration: false,
      contextIsolation: true,
      sandbox: true,
      spellcheck: false,
    },
  })

  mainWindow.setMenuBarVisibility(false)
  mainWindow.loadURL(SITE_URL)

  mainWindow.webContents.setWindowOpenHandler(({ url }) => {
    if (isSiteUrl(url) || url.startsWith(`${SITE_URL}#`)) {
      mainWindow.loadURL(url)
    } else if (/^https?:/i.test(url)) {
      shell.openExternal(url)
    }
    return { action: 'deny' }
  })

  mainWindow.webContents.on('will-navigate', (event, url) => {
    if (isSiteUrl(url)) return
    event.preventDefault()
    if (/^https?:/i.test(url)) shell.openExternal(url)
  })

  mainWindow.webContents.on('did-fail-load', (_event, errorCode, errorDescription, url) => {
    if (errorCode === -3) return // aborted by a navigation we handled above
    dialog.showMessageBox(mainWindow, {
      type: 'error',
      title: 'TikTok Video Downloader HD',
      message: 'The site could not be loaded.',
      detail: `${errorDescription} (${errorCode})\n${url}\n\nCheck your connection and try again.`,
      buttons: ['Retry', 'Quit'],
      defaultId: 0,
    }).then(({ response }) => {
      if (response === 0) mainWindow.loadURL(SITE_URL)
      else app.quit()
    })
  })

  mainWindow.on('closed', () => {
    mainWindow = null
  })
}

app.setAppUserModelId(APP_ID)

app.whenReady().then(() => {
  configureDownloads()
  createWindow()

  app.on('activate', () => {
    if (BrowserWindow.getAllWindows().length === 0) createWindow()
  })
})

app.on('window-all-closed', () => {
  if (process.platform !== 'darwin') app.quit()
})
