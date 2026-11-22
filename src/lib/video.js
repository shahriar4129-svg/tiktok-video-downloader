/** Trim a pasted value and add https:// to bare domains. */
function normalizeValue(raw) {
  const value = String(raw || '').trim()
  if (!value) return ''
  if (/^[a-z][a-z\d+.-]*:/i.test(value)) return value
  if (/^([\w-]+\.)+[a-z]{2,}(:\d+)?(\/\S*)?$/i.test(value)) return `https://${value}`
  return value
}

/** Don't let a browser try to hold an entire giant file in memory. */
export const MAX_VIDEO_BYTES = 500 * 1024 * 1024

const VIDEO_EXTENSION = /\.(mp4|webm|ogv|ogg|mov|m4v|mkv|avi|m4a)(\?.*)?$/i

/** This deployment handles TikTok links only (plus direct video files). */
const TIKTOK_HOSTS = ['tiktok.com', 'vtiktok.com']

function isTikTok(hostname) {
  const host = hostname.toLowerCase().replace(/^www\./, '')
  return TIKTOK_HOSTS.some((domain) => host === domain || host.endsWith(`.${domain}`))
}

/**
 * Decide what a pasted link is. Only http(s) is ever accepted — javascript:,
 * data:, file: and blob: URLs are rejected before they reach fetch().
 */
export function classifyUrl(raw) {
  const normalized = normalizeValue(raw)
  if (!normalized) return { valid: false, error: 'Paste a video link first.' }

  let parsed
  try {
    // A base lets locally bundled sample files (/clip.mp4) work as well.
    parsed = new URL(normalized, window.location.href)
  } catch {
    return { valid: false, error: 'That doesn’t look like a valid link.' }
  }

  if (!['http:', 'https:'].includes(parsed.protocol)) {
    return { valid: false, error: 'Only http:// and https:// links are allowed.' }
  }

  const tiktok = isTikTok(parsed.hostname)
  const looksLikeFile = VIDEO_EXTENSION.test(parsed.pathname)

  if (!tiktok && !looksLikeFile) {
    return {
      valid: false,
      error: 'Only TikTok links or direct video files (.mp4 / .webm) are supported.',
    }
  }

  return {
    valid: true,
    url: parsed.href,
    platform: tiktok ? 'TikTok' : null,
    kind: tiktok ? 'social' : 'direct',
    filename: looksLikeFile ? parsed.pathname.split('/').filter(Boolean).pop() : null,
  }
}

/** Build a safe, human-readable filename for the downloaded file. */
export function videoFilename(url, contentType, suggested) {
  let name = suggested || ''
  if (!name) {
    try {
      const path = new URL(url).pathname
      name = decodeURIComponent(path.split('/').filter(Boolean).pop() || '')
    } catch {
      name = ''
    }
  }

  name = name.replace(/[\\/:*?"<>|\x00-\x1f]/g, '').trim()

  if (!/\.[a-z0-9]{2,5}$/i.test(name)) {
    const rawType = (contentType || '').match(/video\/([\w.+-]+)/)?.[1] || 'mp4'
    const extension = rawType === 'x-matroska' ? 'mkv' : rawType.replace('x-', '')
    name = `${name || 'video'}.${extension}`
  }

  return name.slice(0, 120)
}

/**
 * Prefer the RFC 5987 `filename*` value (UTF-8 title) over the ASCII fallback.
 * Older regexes grabbed `filename=` first and showed ", ....mp4" for titles
 * written in Bengali.
 */
export function downloadNameFromDisposition(disposition, url, fallback = 'video.mp4') {
  const source = disposition || ''
  const encoded = source.match(/filename\*\s*=\s*(?:UTF-8''\s*)?([^;]+)/i)?.[1]
  if (encoded) {
    try {
      const decoded = decodeURIComponent(encoded.trim().replace(/^"|"$/g, ''))
      if (decoded) return videoFilename(url, 'video/mp4', decoded)
    } catch {
      // Malformed header — fall through to the plain filename below.
    }
  }

  const plain = source.match(/filename\s*=\s*(?:"([^"]*)"|([^;]+))/i)
  const candidate = (plain?.[1] || plain?.[2] || '').trim()
  return videoFilename(url, 'video/mp4', candidate || fallback)
}

/**
 * Stream a direct video URL into a Blob, reporting progress.
 * Requires the remote host to allow cross-origin requests (CORS).
 */
export async function fetchVideo(url, { onProgress, signal } = {}) {
  const response = await fetch(url, { mode: 'cors', credentials: 'omit', signal })
  if (!response.ok) {
    throw new Error(`Request failed (${response.status} ${response.statusText || ''})`.trim())
  }

  const contentType = response.headers.get('content-type') || ''
  const total = Number(response.headers.get('content-length')) || 0
  if (total > MAX_VIDEO_BYTES) {
    throw new Error('This file is over 500 MB — too large to keep in a browser tab.')
  }

  // A pasted webpage (no video extension) that answers with HTML is not a video.
  if (contentType.startsWith('text/html') && !VIDEO_EXTENSION.test(new URL(url).pathname)) {
    throw new Error('That link returned a webpage, not a video. Use a direct .mp4 / .webm file URL.')
  }

  const reader = response.body?.getReader()
  if (!reader) {
    const blob = await response.blob()
    onProgress?.(blob.size, blob.size)
    return { blob, contentType, total: blob.size }
  }

  const chunks = []
  let loaded = 0
  for (;;) {
    const { done, value } = await reader.read()
    if (done) break
    loaded += value.length
    if (loaded > MAX_VIDEO_BYTES) {
      await reader.cancel()
      throw new Error('This file is over 500 MB — too large to keep in a browser tab.')
    }
    chunks.push(value)
    onProgress?.(loaded, total)
  }

  return {
    blob: new Blob(chunks, { type: contentType || 'video/mp4' }),
    contentType,
    total: loaded,
  }
}

export function formatBytes(bytes) {
  if (!bytes && bytes !== 0) return ''
  if (bytes < 1024) return `${bytes} B`
  // Video sizes are always shown in MB so the chips and the final file match.
  return `${(bytes / (1024 * 1024)).toFixed(2)} MB`
}
