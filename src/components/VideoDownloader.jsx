import { useEffect, useRef, useState } from 'react'
import { createPortal } from 'react-dom'
import {
  AlertTriangle,
  Check,
  Clipboard,
  Download,
  Info,
  Link2,
  Loader2,
  Play,
  RotateCcw,
  ShieldAlert,
  Video,
  X,
} from 'lucide-react'
import {
  classifyUrl,
  downloadNameFromDisposition,
  fetchVideo,
  formatBytes,
  videoFilename,
} from '../lib/video.js'
import BannerAd from './BannerAd.jsx'
import VignetteAd from './VignetteAd.jsx'
import { ADS } from '../config/ads.js'

// Bundled locally so the demo works offline and without CORS dependencies.
const SAMPLE = '/sample-video.mp4'
// Public TikTok clip — used only as a quick demo chip.
const TIKTOK_DEMO = 'https://www.tiktok.com/@therock/video/6829267836783971589'
// Safety valve: never trap the visitor in the ad if a download stalls.
const JOB_TIMEOUT_MS = 120000

const FAQS = [
  {
    q: 'Is this tool free?',
    a: 'Yes. There is no login, no quota and no extension to install. Paste a link, click Download, then pick a format.',
  },
  {
    q: 'Do I need to install an app?',
    a: 'No. Safari, Chrome, Firefox, Edge or any modern browser is enough — the file saves straight to your device.',
  },
  {
    q: 'Can I download TikTok videos without a watermark?',
    a: 'Yes. The original clean video stream is used, so the TikTok logo and username overlay are not burned into the file.',
  },
  {
    q: 'What formats can I save?',
    a: 'MP4 keeps the video with the best available audio. MP3 extracts only the audio at 192 kbps. Story mode saves an active TikTok story or a direct story media link.',
  },
  {
    q: 'Which video qualities are available?',
    a: 'Only the resolutions the video actually offers are shown, each with its exact estimated size. Pick a lower resolution for a smaller, faster file.',
  },
  {
    q: 'Can I download private or age-restricted videos?',
    a: 'No. Only public links can be read. Age-gated or login-only clips need the owner’s cookies and are not supported here.',
  },
  {
    q: 'Where is my file saved?',
    a: 'Nothing is saved automatically. Pick a format, close the ad, and the file saves to your default Downloads folder. You land right back on the format options. No preview, no page reload.',
  },
  {
    q: 'Why do I see an ad?',
    a: 'Ads keep this tool free. They appear when you pick a download format, and the file is prepared in the background while the ad is open.',
  },
  {
    q: 'Do you store my links or files?',
    a: 'No. Downloaded files are streamed directly to your browser. Nothing is kept after the request ends.',
  },
]

// Output modes kept as plain text — the FAQ above the footer explains each one.
const MODES = [
  { value: 'video', label: 'MP4' },
  { value: 'mp3', label: 'MP3' },
  { value: 'story', label: 'Story' },
]

// This deployment accepts TikTok links only.
const PLATFORMS = ['TikTok']

// Fallback only — normal links show the exact resolutions reported by yt-dlp.
const FALLBACK_QUALITIES = [
  { value: '480', label: '480p' },
  { value: '720', label: '720p' },
  { value: '1080', label: '1080p' },
]

const CHIP_CLASS =
  'rounded-md bg-white/15 px-3 py-1.5 text-[11px] font-semibold text-white ring-1 ring-inset ring-white/30 transition hover:bg-white/25 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-white'

const OPTION_CLASS =
  'flex w-full items-center justify-center gap-3 rounded-lg ss-gradient px-4 py-3 text-center text-white shadow-md shadow-teal-500/20 transition hover:brightness-110 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-teal-400 disabled:cursor-not-allowed disabled:opacity-50'

function AdBadge() {
  return ADS.enabled && ADS.vignette?.enabled && ADS.vignette?.showBadge ? (
    <span className="ml-1 inline-flex items-center gap-1 rounded bg-white/25 px-1.5 py-0.5 text-[9px] font-black uppercase tracking-wider text-white">
      <span aria-hidden="true">▶</span> AD
    </span>
  ) : null
}

function formatDuration(seconds) {
  const total = Math.round(seconds || 0)
  if (!total) return ''
  return `${Math.floor(total / 60)}:${String(total % 60).padStart(2, '0')}`
}

function MiniBenefit({ children }) {
  return (
    <div className="flex items-center justify-center gap-1.5 rounded-md bg-white/15 px-3 py-2 text-center text-[11px] font-semibold text-white ring-1 ring-inset ring-white/25">
      <Check className="h-3.5 w-3.5 shrink-0 text-white" aria-hidden="true" />
      {children}
    </div>
  )
}

function Heading({ icon: Icon, title, subtitle }) {
  return (
    <div className="mb-4 flex items-center gap-3">
      <span className="grid h-9 w-9 place-items-center rounded-xl bg-teal-500/10 text-teal-600 ring-1 ring-inset ring-teal-400/30">
        <Icon className="h-4.5 w-4.5" strokeWidth={2} aria-hidden="true" />
      </span>
      <div>
        <h2 className="text-sm font-bold tracking-tight text-slate-900">{title}</h2>
        <p className="text-xs text-slate-500">{subtitle}</p>
      </div>
    </div>
  )
}

function FeatureCard({ icon: Icon, title, children }) {
  return (
    <div className="rounded-2xl border border-slate-200 bg-slate-50 p-4">
      <span className="grid h-9 w-9 place-items-center rounded-xl bg-teal-500/10 text-teal-600 ring-1 ring-inset ring-teal-400/30">
        <Icon className="h-4.5 w-4.5" aria-hidden="true" />
      </span>
      <h3 className="mt-3 text-sm font-bold text-slate-900">{title}</h3>
      <p className="mt-1 text-xs leading-relaxed text-slate-500">{children}</p>
    </div>
  )
}

function Step({ number, title, children }) {
  return (
    <div className="rounded-2xl bg-slate-50 p-4">
      <span className="grid h-7 w-7 place-items-center rounded-lg bg-teal-500/10 text-xs font-black text-teal-600">
        {number}
      </span>
      <h3 className="mt-3 text-sm font-bold text-slate-900">{title}</h3>
      <p className="mt-1 text-xs leading-relaxed text-slate-500">{children}</p>
    </div>
  )
}

export default function VideoDownloader({ showToast, onResultStateChange }) {
  const [url, setUrl] = useState('')
  const [platform, setPlatform] = useState('TikTok')
  const [mode, setMode] = useState('video')
  const [quality, setQuality] = useState('720')
  const [storyId, setStoryId] = useState('')
  const [state, setState] = useState('idle') // idle | loading | analyzing | social | ready | unsupported | error
  const [info, setInfo] = useState(null)
  const [remoteInfo, setRemoteInfo] = useState(null)
  const [serviceReady, setServiceReady] = useState(true)
  const [error, setError] = useState('')
  const [blobUrl, setBlobUrl] = useState(null)
  const [fileName, setFileName] = useState('')
  const [fileSize, setFileSize] = useState(0)

  const prefetchRef = useRef(new Map())
  const prefetchAbortRef = useRef(null)
  const prefetchKeyRef = useRef('')
  const prefetchPromiseRef = useRef(null)
  // Prepared files are cached here so the ad's 3-second beat is enough: the
  // download starts when the link appears, not when the visitor taps a format.
  const blobCacheRef = useRef(new Map())
  const blobPrefetchRef = useRef(new Map())
  const variantRef = useRef('')
  const abortRef = useRef(null)
  const blobUrlRef = useRef(null)
  const blobRef = useRef(null)
  const resultPanelRef = useRef(null)
  const [vignetteOpen, setVignetteOpen] = useState(false)
  const pendingActionRef = useRef(null)
  const vignetteShownRef = useRef(false)
  // The ad is open while the file is fetched in the background.
  const downloadJobRef = useRef(null)
  const [jobPending, setJobPending] = useState(false)
  const jobPendingRef = useRef(false)
  const [opening, setOpening] = useState(false)
  const [previewOpen, setPreviewOpen] = useState(false)
  const [previewSrc, setPreviewSrc] = useState('')
  const [previewIsVideo, setPreviewIsVideo] = useState(false)
  const previewObjectUrlRef = useRef('')

  function replaceBlobUrl(next) {
    if (blobUrlRef.current) URL.revokeObjectURL(blobUrlRef.current)
    blobUrlRef.current = next
    setBlobUrl(next)
  }

  // The public resolver can be briefly slow or rate-limited. Retry those
  // transient failures quietly so the visitor rarely sees an error card.
  const RETRYABLE = /try again|busy|temporar|could not read|failed|rate|too many/i
  const wait = (ms) => new Promise((resolve) => window.setTimeout(resolve, ms))

  async function postJsonWithRetry(path, body, attempts = 3, signal) {
    let lastError
    for (let i = 0; i < attempts; i += 1) {
      if (signal?.aborted) throw new DOMException('Aborted', 'AbortError')
      try {
        const response = await fetch(path, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(body),
          ...(signal ? { signal } : {}),
        })
        const payload = await response.json().catch(() => ({}))
        if (response.ok) return payload
        lastError = new Error(payload.error || `Request failed (${response.status})`)
        if (response.status < 500 && !RETRYABLE.test(payload.error || '')) break
      } catch (cause) {
        if (signal?.aborted || cause?.name === 'AbortError') throw cause
        lastError = cause
      }
      if (i < attempts - 1) await wait(500 * (i + 1))
    }
    throw lastError
  }

  // One shared request path for both the foreground save and the background
  // warm-up. `signal` is only passed by the foreground job so a cancelled job
  // cannot abort a file that another visitor session is warming.
  async function requestSocialBlob({ url, quality, mode, storyId, platform, signal, media }) {
    let lastError
    for (let i = 0; i < 3; i += 1) {
      if (signal?.aborted) throw new DOMException('Aborted', 'AbortError')
      let response
      try {
        response = await fetch('/api/video/download', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            url, quality, mode, storyId,
            variant: variantRef.current,
            // Reuse the CDN URLs from the info lookup — no second resolver call.
            media,
          }),
          ...(signal ? { signal } : {}),
        })
      } catch (cause) {
        if (signal?.aborted || cause?.name === 'AbortError') throw cause
        lastError = cause
        if (i < 2) await wait(500 * (i + 1))
        continue
      }
      if (response.ok) {
        const blob = await response.blob()
        const disposition = response.headers.get('content-disposition') || ''
        const fallbackExt = mode === 'mp3' ? '.mp3' : '.mp4'
        const name = downloadNameFromDisposition(
          disposition,
          url,
          `${platform || 'video'}${fallbackExt}`,
        )
        return { blob, name, size: blob.size }
      }
      const body = await response.json().catch(() => ({}))
      lastError = new Error(body.error || `Request failed (${response.status})`)
      if (signal?.aborted || (response.status < 500 && !RETRYABLE.test(body.error || ''))) break
      if (i < 2) await wait(500 * (i + 1))
    }
    throw lastError
  }

  // Start preparing the likely file as soon as a link is pasted. By the time
  // the visitor picks a format and sits through the ad beat, the Blob is usually
  // already here, so "Remove ads" is active the moment it fades in.
  function prefetchBlobs(rawUrl, currentMode, media) {
    if (currentMode !== 'video' && currentMode !== 'mp3') return
    // quality 4320 means "best available" to the backend, so the warm-up does
    // not have to wait for the metadata lookup to learn the exact resolution.
    const plan = currentMode === 'mp3'
      ? [{ key: `${rawUrl}|mp3|mp3`, mode: 'mp3' }]
      : [{ key: `${rawUrl}|video|hd`, mode: 'video', quality: '4320' }]
    plan.forEach(({ key, mode, quality }) => {
      if (blobCacheRef.current.has(key) || blobPrefetchRef.current.has(key)) return
      const promise = requestSocialBlob({ url: rawUrl, quality, mode, media })
      blobPrefetchRef.current.set(key, promise)
      promise
        .then((base) => blobCacheRef.current.set(key, base))
        .catch(() => {
          // Best effort — a normal click still fetches and surfaces any error.
        })
        .finally(() => blobPrefetchRef.current.delete(key))
    })
  }

  function prefetchDirectBlob(result) {
    const key = `direct|${result.url}`
    if (blobCacheRef.current.has(key) || blobPrefetchRef.current.has(key)) return
    const promise = fetchVideo(result.url).then(({ blob, contentType }) => ({
      blob,
      name: videoFilename(result.url, contentType, result.filename),
      size: blob.size,
    }))
    blobPrefetchRef.current.set(key, promise)
    promise
      .then((base) => blobCacheRef.current.set(key, base))
      .catch(() => {
        // Best effort — the real click still reports failures.
      })
      .finally(() => blobPrefetchRef.current.delete(key))
  }

  function cachedBlobJob(key) {
    const hit = blobCacheRef.current.get(key)
    if (hit) return Promise.resolve({ ...hit, objectUrl: URL.createObjectURL(hit.blob) })
    // Reuse the warm-up already in flight instead of starting a second download.
    const inflight = blobPrefetchRef.current.get(key)
    if (inflight) {
      return inflight.then((base) => ({ ...base, objectUrl: URL.createObjectURL(base.blob) }))
    }
    return null
  }

  // Preview uses the file that was already prepared for saving — no second
  // download and no extra request to TikTok.
  function preparedKey() {
    if (!info) return ''
    return info.kind === 'direct'
      ? `direct|${info.url}`
      : mode === 'mp3'
        ? `${info.url}|mp3|mp3`
        : `${info.url}|video|hd`
  }

  function preparedBlob() {
    if (blobRef.current) return blobRef.current
    const key = preparedKey()
    return key ? blobCacheRef.current.get(key)?.blob || null : null
  }

  function closePreview() {
    if (previewObjectUrlRef.current) {
      URL.revokeObjectURL(previewObjectUrlRef.current)
      previewObjectUrlRef.current = ''
    }
    setPreviewOpen(false)
    setPreviewSrc('')
  }

  async function openPreview() {
    let blob = preparedBlob()
    if (!blob) {
      const inflight = blobPrefetchRef.current.get(preparedKey())
      if (inflight) {
        showToast('Preparing preview…')
        try {
          blob = (await inflight).blob
        } catch {
          // Fall back to the cover below if the warm-up failed.
        }
      }
    }
    if (blob) {
      const src = URL.createObjectURL(blob)
      if (previewObjectUrlRef.current) URL.revokeObjectURL(previewObjectUrlRef.current)
      previewObjectUrlRef.current = src
      setPreviewIsVideo(true)
      setPreviewSrc(src)
    } else if (remoteInfo?.thumbnail) {
      // The prepared file may still be warming up — show the cover for now.
      setPreviewIsVideo(false)
      setPreviewSrc(remoteInfo.thumbnail)
    } else {
      showToast('Preview is still preparing — try again in a moment')
      return
    }
    setPreviewOpen(true)
  }

  useEffect(() => {
    if (!previewOpen) return undefined
    function onKeyDown(event) {
      if (event.key === 'Escape') closePreview()
    }
    document.addEventListener('keydown', onKeyDown)
    return () => document.removeEventListener('keydown', onKeyDown)
  }, [previewOpen])

  useEffect(() => () => {
    if (previewObjectUrlRef.current) URL.revokeObjectURL(previewObjectUrlRef.current)
  }, [])

  // Save the Blob directly from the browser — no preview page.
  function startBlobDownload(objectUrl, name) {
    if (!objectUrl || !name) return
    const link = document.createElement('a')
    link.href = objectUrl
    link.download = name
    document.body.appendChild(link)
    link.click()
    link.remove()
  }

  // Save straight to the device's default Downloads folder — this works in
  // every browser and keeps the save immediate after the ad is closed.
  async function saveWithPermission(blob, name) {
    if (!blob || !name) return 'missing'
    const objectUrl = URL.createObjectURL(blob)
    replaceBlobUrl(objectUrl)
    startBlobDownload(objectUrl, name)
    return 'direct'
  }

  useEffect(() => {
    // Only used to give a clearer hint when the helper service is unreachable.
    fetch('/api/health')
      .then((response) => setServiceReady(response.ok))
      .catch(() => setServiceReady(false))
    // A returning visitor often uses the same clip. Warm it on load so the
    // file is already waiting by the time the ad appears.
    try {
      const last = window.localStorage.getItem('tiktok-video-last-link')
      if (last) prefetchInfo(last, 'video')
    } catch {
      // Storage can be disabled — warm-up is best effort only.
    }
  }, [])

  function rememberLink(value) {
    try {
      window.localStorage.setItem('tiktok-video-last-link', value)
    } catch {
      // Ignore private-mode storage errors.
    }
  }

  // Start reading the link the moment it appears — no debounce. A paste can
  // also kick this off before React re-renders, so the network wait overlaps
  // with the visitor's click on Download.
  function prefetchInfo(rawUrl, currentMode) {
    const result = classifyUrl(rawUrl)
    if (!result.valid) return
    if (result.kind === 'direct') {
      prefetchDirectBlob(result)
      return
    }
    if (result.kind !== 'social') return
    const key = `${result.url}|${currentMode}`
    if (prefetchRef.current.has(key) || prefetchKeyRef.current === key) return
    prefetchAbortRef.current?.abort()
    const controller = new AbortController()
    prefetchAbortRef.current = controller
    prefetchKeyRef.current = key
    // One lookup only. Once the CDN URLs arrive we warm the file with them,
    // instead of firing a second resolver call from the download endpoint.
    const promise = postJsonWithRetry(
      '/api/video/info',
      { url: result.url, mode: currentMode },
      3,
      controller.signal,
    )
      .then((body) => {
        if (!body) return
        prefetchRef.current.set(key, body)
        if (body.variant) variantRef.current = body.variant
        rememberLink(result.url)
        prefetchBlobs(result.url, currentMode, body.media)
      })
      .catch(() => {
        // Best effort only — the real click still surfaces any error.
      })
    prefetchPromiseRef.current = { key, promise }
  }

  useEffect(() => {
    if (state !== 'idle') return undefined
    // Paste calls prefetchInfo directly (instant). Typing is debounced so a
    // half-written link cannot start a dozen lookups and file preparations.
    const timer = window.setTimeout(() => prefetchInfo(url, mode), 250)
    return () => window.clearTimeout(timer)
  }, [url, mode, state])

  function handleInputPaste(event) {
    const text = event.clipboardData?.getData('text') || ''
    if (text.trim()) prefetchInfo(text.trim(), mode)
  }

  useEffect(
    () => () => {
      prefetchAbortRef.current?.abort()
      abortRef.current?.abort()
      if (blobUrlRef.current) URL.revokeObjectURL(blobUrlRef.current)
    },
    [],
  )

  // Pick a sane default from the resolutions this video actually offers.
  useEffect(() => {
    const heights = (remoteInfo?.formats || [])
      .map((item) => Number(item.height))
      .filter((height) => Number.isFinite(height) && height > 0)
    if (!heights.length) return
    setQuality(String(Math.max(...heights)))
  }, [remoteInfo])

  useEffect(() => {
    const stories = remoteInfo?.stories || []
    setStoryId(stories.length ? String(stories[0].id) : '')
  }, [remoteInfo])

  // The result replaces the hero. Center the whole card so the thumbnail and
  // every format option are visible at once — centering only the options used
  // to push the heading off the top of the screen.
  useEffect(() => {
    if (state !== 'social' && state !== 'direct' && state !== 'ready') return
    // A short delay lets any focus/scroll already in flight settle first.
    const timer = window.setTimeout(() => {
      const target = resultPanelRef.current
      if (!target) return
      const box = target.getBoundingClientRect()
      const block = box.height > window.innerHeight - 24 ? 'start' : 'center'
      target.scrollIntoView({ behavior: 'instant', block })
    }, 250)
    return () => window.clearTimeout(timer)
  }, [state])

  // Let the page hide its intro heading while a result is on screen.
  useEffect(() => {
    onResultStateChange?.(state !== 'idle')
  }, [state, onResultStateChange])

  function clearResult() {
    if (downloadJobRef.current) {
      abortRef.current?.abort()
      downloadJobRef.current = null
    }
    jobPendingRef.current = false
    setJobPending(false)
    setOpening(false)
    replaceBlobUrl(null)
    blobRef.current = null
    setFileName('')
    setFileSize(0)
    setError('')
    setInfo(null)
    setRemoteInfo(null)
    setQuality('720')
    setStoryId('')
  }

  function resetAll() {
    abortRef.current?.abort()
    clearResult()
    setUrl('')
    setState('idle')
  }

  function handleModeChange(next) {
    if (next === mode) return
    abortRef.current?.abort()
    clearResult()
    setMode(next)
    setState('idle')
  }

  // Show the interstitial before the final download.
  function runWithAd(action) {
    const ad = ADS.vignette
    if (
      !ADS.enabled ||
      !ad?.enabled ||
      (ad.oncePerSession && vignetteShownRef.current)
    ) {
      action()
      return
    }
    pendingActionRef.current = action
    setVignetteOpen(true)
  }

  function closeVignette() {
    // Keep the ad up until the file is ready — the Close click is the gesture
    // that starts the save immediately.
    if (jobPendingRef.current) return
    setVignetteOpen(false)
    vignetteShownRef.current = true
    const action = pendingActionRef.current
    pendingActionRef.current = null
    if (action) action()
  }

  async function handlePaste() {
    try {
      const text = await navigator.clipboard.readText()
      if (!text.trim()) {
        showToast('Nothing in the clipboard')
        return
      }
      prefetchInfo(text.trim(), mode)
      setUrl(text.trim())
      showToast('Link pasted — now click Download')
    } catch {
      showToast('Clipboard blocked by the browser. Click the field and press Ctrl+V.')
    }
  }

  async function handleSubmit(event) {
    event.preventDefault()
    clearResult()

    const result = classifyUrl(url)
    if (!result.valid) {
      setState('error')
      setError(result.error)
      return
    }

    setInfo(result)

    if (result.kind === 'social') {
      const key = `${result.url}|${mode}`
      const cachedBody = prefetchRef.current.get(key)
      const inflight = prefetchPromiseRef.current
      const reusing = !cachedBody && inflight?.key === key
      // When the background lookup is already running, stay on the hero with
      // a quiet "Opening…" label instead of flashing a full spinner screen.
      if (cachedBody || reusing) setOpening(true)
      else setState('analyzing')
      try {
        let body = cachedBody
        // Reuse the background lookup instead of firing a duplicate request.
        if (reusing) {
          await inflight.promise
          body = prefetchRef.current.get(key)
        }
        if (!body) {
          body = await postJsonWithRetry('/api/video/info', { url: result.url, mode })
        }
        setOpening(false)
        setRemoteInfo(body)
        if (body.variant) variantRef.current = body.variant
        rememberLink(result.url)
        // Also warm the file here — a fast click can beat the paste-time prefetch.
        prefetchBlobs(result.url, mode, body.media)

        // Show the thumbnail plus the exact formats this video offers.
        const heights = (body.formats || [])
          .map((item) => Number(item.height))
          .filter((height) => Number.isFinite(height) && height > 0)
        const bestQuality = heights.length ? String(Math.max(...heights)) : '720'
        const firstStory = body.stories?.[0]?.id
        setQuality(bestQuality)
        if (firstStory != null) setStoryId(String(firstStory))
        setState('social')
      } catch (cause) {
        setOpening(false)
        setError(cause.message || 'The download service is unavailable.')
        setState('unsupported')
      }
      return
    }

    // Direct link — show one save option first; the file is prepared on tap.
    setState('direct')
  }

  // Files are prepared in the background while the ad is open. Nothing is saved
  // automatically — the visitor always confirms where the file should go.
  function fetchDirectBlob(result) {
    const warm = cachedBlobJob(`direct|${result.url}`)
    if (warm) return warm
    const controller = new AbortController()
    abortRef.current = controller
    return fetchVideo(result.url, { signal: controller.signal })
      .then(({ blob, contentType }) => ({
        blob,
        objectUrl: URL.createObjectURL(blob),
        name: videoFilename(result.url, contentType, result.filename),
        size: blob.size,
      }))
      .finally(() => {
        if (abortRef.current === controller) abortRef.current = null
      })
  }

  async function fetchSocialBlob({
    quality: wantedQuality = quality,
    mode: wantedMode = mode,
    storyId: wantedStory = storyId,
    link = info,
  } = {}) {
    if (!link) throw new Error('Link not found.')
    const controller = new AbortController()
    abortRef.current = controller
    try {
      const base = await requestSocialBlob({
        url: link.url,
        quality: wantedQuality,
        mode: wantedMode,
        storyId: wantedStory,
        platform: link.platform,
        signal: controller.signal,
        media: remoteInfo?.media,
      })
      return { ...base, objectUrl: URL.createObjectURL(base.blob) }
    } finally {
      if (abortRef.current === controller) abortRef.current = null
    }
  }

  function startJob(job, kind) {
    if (downloadJobRef.current) return

    // The ad keeps "Preparing your file…" until the Blob is fully ready.
    // Close/Remove ads only appear after that, so no work is left when the
    // visitor taps them and the save stays inside the user gesture.
    let timer
    const guarded = new Promise((resolve, reject) => {
      const settle = (error) => {
        window.clearTimeout(timer)
        reject(error)
      }
      timer = window.setTimeout(() => {
        abortRef.current?.abort()
        settle(new Error('The download is taking too long. Please try again.'))
      }, JOB_TIMEOUT_MS)
      job.then(
        (value) => {
          window.clearTimeout(timer)
          resolve(value)
        },
        settle,
      )
    })

    downloadJobRef.current = guarded
    jobPendingRef.current = true
    setJobPending(true)
    guarded.then(
      () => {
        jobPendingRef.current = false
        setJobPending(false)
      },
      () => {
        jobPendingRef.current = false
        setJobPending(false)
      },
    )
    runWithAd(() => finishDownload(guarded, kind))
  }

  // Reuse a file that was prepared while the visitor was reading the options.
  function warmJob(kind) {
    if (!info || info.kind === 'direct') return null
    if (kind === 'hd') return cachedBlobJob(`${info.url}|video|hd`)
    if (kind === 'mp3') return cachedBlobJob(`${info.url}|mp3|mp3`)
    return null
  }

  // Each reference-style option prepares its own file, then shows the ad.
  function handleSaveOption(kind) {
    if (downloadJobRef.current || !info) return
    if (kind === 'direct') {
      startJob(fetchDirectBlob(info), 'direct')
    } else if (kind === 'sd') {
      startJob(warmJob(kind) || fetchSocialBlob({ quality: sdQuality, mode: 'video' }), 'social')
    } else if (kind === 'mp3') {
      startJob(warmJob(kind) || fetchSocialBlob({ mode: 'mp3' }), 'social')
    } else if (kind === 'story') {
      startJob(fetchSocialBlob({ mode: 'story', storyId }), 'social')
    } else {
      startJob(warmJob('hd') || fetchSocialBlob({ quality, mode: 'video' }), 'social')
    }
  }

  function saveResultToast(result) {
    if (result === 'direct') showToast('Download started')
    else if (result !== 'missing') showToast('File saved')
  }

  function handleSaveAgain() {
    if (!blobRef.current) return
    saveWithPermission(blobRef.current, fileName).then(saveResultToast)
  }

  // Called only from the ad's Close button, so the save keeps its user gesture.
  async function finishDownload(job, kind) {
    try {
      const { blob, objectUrl, name, size } = await job
      blobRef.current = blob
      replaceBlobUrl(objectUrl)
      setFileName(name)
      setFileSize(size)

      const saved = await saveWithPermission(blob, name)
      saveResultToast(saved)
      // The file is on its way — go back to the format options instead of
      // parking the visitor on a "File ready to save" screen.
      setState(kind === 'direct' ? 'direct' : 'social')
    } catch (cause) {
      if (cause.name === 'AbortError') {
        setState('idle')
      } else if (kind === 'direct') {
        setState('error')
        setError(
          cause instanceof TypeError
            ? 'The browser blocked this download (CORS). Try another CDN-hosted direct link.'
            : cause.message || 'Download failed.',
        )
      } else {
        setError(cause.message || 'Download failed.')
        setState('unsupported')
      }
    } finally {
      if (downloadJobRef.current === job) downloadJobRef.current = null
    }
  }

  const qualityOptions = (remoteInfo?.formats || []).length
    ? remoteInfo.formats.map((item) => ({
        value: String(item.height),
        label: item.label || `${item.height}p`,
        size: item.filesize || 0,
      }))
    : FALLBACK_QUALITIES
  const selectedQuality = qualityOptions.find((item) => item.value === quality) || qualityOptions[qualityOptions.length - 1]
  const sdOption = qualityOptions.find((item) => item.value !== selectedQuality?.value) || null
  const sdQuality = sdOption?.value || selectedQuality?.value || quality

  return (
    <div className="space-y-6">
      {/* ---------- Hero: hidden once Download is clicked ---------- */}
      {state === 'idle' && (
      <section className="ss-gradient rounded-2xl px-5 py-8 text-white shadow-lg shadow-teal-500/20 sm:px-10 sm:py-10">
        <div className="mx-auto max-w-3xl text-center">
          <h2 className="text-2xl font-extrabold tracking-tight text-white sm:text-[32px]">
            TikTok Video Downloader
          </h2>
          <p className="mt-2 text-sm leading-relaxed text-white/80">
            Download TikTok videos without watermark. Paste a link, choose a format and save it in seconds.
          </p>
        </div>

        <form onSubmit={handleSubmit} className="mx-auto mt-6 max-w-3xl">
          <div className="flex flex-col gap-2 sm:flex-row">
            <div className="relative flex-1">
              <Link2
                className="pointer-events-none absolute left-4 top-1/2 h-4 w-4 -translate-y-1/2 text-teal-500"
                aria-hidden="true"
              />
              <label htmlFor="video-url" className="sr-only">
                TikTok video link
              </label>
              <input
                id="video-url"
                type="text"
                inputMode="url"
                value={url}
                onChange={(event) => setUrl(event.target.value)}
                onPaste={handleInputPaste}
                placeholder="Insert a link here"
                spellCheck={false}
                autoComplete="off"
                style={{ backdropFilter: 'blur(9.48952px)', WebkitBackdropFilter: 'blur(9.48952px)' }}
                className="ss-input w-full pl-11 pr-4 text-base text-slate-900 outline-none transition focus:ring-2 focus:ring-white/40"
              />
            </div>
            {/* One slot: Paste while the field is empty, Clear once a link is in it. */}
            <button
              type="button"
              onClick={url.trim() ? resetAll : handlePaste}
              className="inline-flex w-full items-center justify-center gap-2 rounded-lg bg-white px-5 py-4 text-sm font-semibold text-[#0d9488] transition hover:bg-white/90 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-white sm:w-auto sm:min-w-[6.5rem]"
            >
              {url.trim() ? (
                <>
                  <X className="h-4 w-4" aria-hidden="true" />
                  Clear
                </>
              ) : (
                <>
                  <Clipboard className="h-4 w-4" aria-hidden="true" />
                  Paste
                </>
              )}
            </button>
            <button
              type="submit"
              disabled={state === 'analyzing' || opening}
              className="inline-flex w-full items-center justify-center gap-2 rounded-lg bg-[#0d9488] px-6 py-4 text-base font-semibold text-white shadow-lg shadow-teal-900/20 transition hover:brightness-110 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-white disabled:cursor-not-allowed disabled:opacity-50 sm:w-auto sm:min-w-[9rem]"
            >
              {state === 'analyzing' || opening ? (
                <Loader2 className="h-5 w-5 animate-spin" aria-hidden="true" />
              ) : (
                <Download className="h-5 w-5" aria-hidden="true" />
              )}
              {state === 'analyzing' || opening ? 'Opening…' : 'Download'}
            </button>
          </div>

          <div className="mt-4 flex flex-wrap items-center justify-center gap-2 text-white">
            <label htmlFor="platform" className="text-[11px] font-bold uppercase tracking-wider text-white/70">
              Platform
            </label>
            <select
              id="platform"
              value={platform}
              onChange={(event) => setPlatform(event.target.value)}
              className="rounded-md bg-white/15 px-2.5 py-1.5 text-[11px] font-semibold text-white outline-none ring-1 ring-inset ring-white/30 transition focus:ring-2 focus:ring-white [&>option]:text-slate-900"
            >
              {PLATFORMS.map((item) => (
                <option key={item} value={item}>
                  {item}
                </option>
              ))}
            </select>
            <span className="hidden h-4 w-px bg-white/30 sm:block" />
            <span className="text-[11px] text-white/70">Format</span>
            <div role="group" aria-label="Download mode" className="flex items-center gap-2">
              {MODES.map((item) => (
                <button
                  key={item.value}
                  type="button"
                  aria-pressed={mode === item.value}
                  onClick={() => handleModeChange(item.value)}
                  className={`rounded-md px-2.5 py-1 text-[11px] font-semibold transition focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-white ${
                    mode === item.value
                      ? 'bg-white text-[#0d9488]'
                      : 'bg-white/10 text-white/90 hover:bg-white/20'
                  }`}
                >
                  {item.label}
                </button>
              ))}
            </div>
            <span className="hidden h-4 w-px bg-white/30 sm:block" />
            <span className="text-[11px] text-white/70">Direct .mp4 / .webm links work too</span>
          </div>
        </form>

        <div className="mx-auto mt-5 grid max-w-3xl gap-2 sm:grid-cols-3">
          <MiniBenefit>Unlimited saves</MiniBenefit>
          <MiniBenefit>No watermark</MiniBenefit>
          <MiniBenefit>HD quality</MiniBenefit>
          <MiniBenefit>MP4, MP3 &amp; Stories</MiniBenefit>
          <MiniBenefit>No login or app</MiniBenefit>
          <MiniBenefit>Fast &amp; free</MiniBenefit>
        </div>

        <div className="mx-auto mt-4 flex max-w-3xl flex-wrap items-center justify-center gap-2">
          <span className="text-[11px] font-semibold uppercase tracking-wider text-white/70">Try</span>
          <button type="button" onClick={() => setUrl(SAMPLE)} className={CHIP_CLASS}>
            Sample MP4
          </button>
          <button type="button" onClick={() => setUrl(TIKTOK_DEMO)} className={CHIP_CLASS}>
            TikTok demo
          </button>
        </div>
      </section>
      )}

      {/* ---------- Result: shown only after Download is clicked ---------- */}
      {state !== 'idle' && (
      <section ref={resultPanelRef} className="card-surface scroll-mt-4 p-5 sm:p-6">
        <div className="mb-5 flex items-center justify-between">
          <h2 className="text-sm font-bold text-slate-900">Your download</h2>
          <button
            type="button"
            onClick={resetAll}
            className="inline-flex items-center gap-1.5 rounded-lg px-2 py-1.5 text-[11px] font-semibold text-slate-500 transition hover:bg-slate-200 hover:text-slate-900 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-teal-500"
          >
            <RotateCcw className="h-3.5 w-3.5" aria-hidden="true" />
            Clear
          </button>
        </div>

        <div className="flex min-h-[300px] flex-col items-center justify-center gap-3 rounded-2xl bg-slate-50 p-5 text-center ring-1 ring-inset ring-slate-200">
          {state === 'analyzing' && (
            <div className="w-full max-w-sm">
              <Loader2 className="mx-auto h-8 w-8 animate-spin text-teal-600" aria-hidden="true" />
              <p className="mt-3 text-xs font-semibold text-slate-600">Opening your formats…</p>
            </div>
          )}

          {state === 'unsupported' && info && (
            <div className="w-full max-w-sm">
              <span className="mx-auto grid h-14 w-14 place-items-center rounded-2xl bg-amber-500/10 text-amber-700 ring-1 ring-inset ring-amber-400/20">
                <AlertTriangle className="h-7 w-7" aria-hidden="true" />
              </span>
              <p className="mt-4 text-sm font-bold text-amber-700">
                {info.platform} download failed
              </p>
              <p className="mt-2 text-xs leading-relaxed text-slate-500">
                {serviceReady ? (
                  <>
                    This link could not be processed. The platform may be blocking the request, or the
                    clip may be private or restricted — see the details below.
                  </>
                ) : (
                  <>The download service is unavailable right now. Start the local helper and try again.</>
                )}
              </p>
              {error && <p className="mt-3 break-words text-[11px] text-rose-600">{error}</p>}
              <div className="mt-4 flex flex-wrap justify-center gap-2">
                <button
                  type="button"
                  onClick={() => handleSubmit({ preventDefault() {} })}
                  className="inline-flex items-center justify-center gap-2 rounded-xl ss-gradient px-4 py-2.5 text-xs font-semibold text-white shadow-md shadow-teal-500/20 transition hover:brightness-110 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-teal-400"
                >
                  <RotateCcw className="h-3.5 w-3.5" aria-hidden="true" />
                  Try again
                </button>
              </div>
            </div>
          )}

          {state === 'social' && remoteInfo && (
            <div className="grid w-full max-w-3xl gap-5 sm:grid-cols-[minmax(0,0.8fr)_minmax(0,1.2fr)]">
              <div className="min-w-0">
              {remoteInfo.thumbnail && (
                <div className="relative">
                  <img
                    src={remoteInfo.thumbnail}
                    alt=""
                    className="mx-auto max-h-64 w-full rounded-xl object-cover shadow-lg sm:mx-0"
                  />
                  <button
                    type="button"
                    onClick={openPreview}
                    aria-label="Preview this video"
                    className="absolute inset-0 m-auto flex h-fit w-fit items-center gap-1.5 rounded-full bg-slate-900/75 px-3 py-2 text-[11px] font-bold text-white shadow-lg ring-1 ring-white/40 backdrop-blur-sm transition hover:bg-slate-900/90 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-teal-400"
                  >
                    <Play className="h-3.5 w-3.5 fill-white" aria-hidden="true" />
                    Preview
                  </button>
                </div>
              )}
              <div className="mt-3 flex flex-wrap items-center justify-center gap-2 sm:justify-start">
                <span className="rounded-full bg-teal-500/10 px-2.5 py-1 text-[10px] font-black uppercase tracking-wider text-teal-600">
                  {remoteInfo.platform || 'TikTok'}
                </span>
                {remoteInfo.duration ? (
                  <span className="rounded-full bg-slate-100 px-2.5 py-1 text-[10px] font-semibold text-slate-500">
                    {formatDuration(remoteInfo.duration)}
                  </span>
                ) : null}
                <span className="rounded-full bg-emerald-500/10 px-2.5 py-1 text-[10px] font-semibold text-emerald-600">
                  No watermark
                </span>
              </div>
              <p className="mt-3 text-sm font-bold text-slate-900">
                {remoteInfo.uploader || 'TikTok video'}
              </p>
              <p className="mt-1 line-clamp-2 text-xs leading-relaxed text-slate-500">
                {remoteInfo.title}
              </p>

              {mode === 'video' ? (
                <div className="mt-5">
                  <p className="mb-2 text-[10px] font-bold uppercase tracking-wider text-slate-500">
                    Available qualities
                  </p>
                  <div role="group" aria-label="Video quality" className="space-y-2">
                    {qualityOptions.map((item) => (
                      <button
                        key={item.value}
                        type="button"
                        aria-pressed={quality === item.value}
                        onClick={() => setQuality(item.value)}
                        className={`flex w-full items-center justify-between rounded-xl border px-3 py-2 text-[11px] font-bold transition focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-teal-500 ${
                          quality === item.value
                            ? 'border-teal-400/50 bg-teal-500/15 text-slate-900 shadow-sm'
                            : 'border-slate-200 bg-slate-50 text-slate-500 hover:border-teal-400/40 hover:text-slate-900'
                        }`}
                      >
                        <span>{item.label}</span>
                        {item.size > 0 && (
                          <span className="text-[10px] font-semibold opacity-60">
                            {formatBytes(item.size)}
                          </span>
                        )}
                      </button>
                    ))}
                  </div>
                  <p className="mt-2 text-[10px] text-slate-500">
                    Only the qualities this video actually offers are shown.
                  </p>
                </div>
              ) : mode === 'mp3' ? (
                <div className="mt-5 rounded-xl border border-slate-200 bg-slate-50 px-3 py-2.5 text-center text-[11px] font-semibold text-slate-500">
                  Audio only · 192 kbps MP3
                </div>
              ) : (
                <div className="mt-5">
                  <p className="mb-2 text-[10px] font-bold uppercase tracking-wider text-slate-500">
                    Active stories — choose one
                  </p>
                  {(remoteInfo?.stories || []).length ? (
                    <div className="flex flex-wrap justify-center gap-2">
                      {remoteInfo.stories.map((item) => (
                        <button
                          key={item.id}
                          type="button"
                          aria-pressed={String(storyId) === String(item.id)}
                          onClick={() => setStoryId(String(item.id))}
                          className={`h-24 w-16 overflow-hidden rounded-xl border-2 transition focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-teal-500 ${
                            String(storyId) === String(item.id)
                              ? 'border-teal-500 shadow-lg shadow-teal-500/20'
                              : 'border-slate-200 opacity-70 hover:opacity-100'
                          }`}
                        >
                          {item.thumbnail ? (
                            <img src={item.thumbnail} alt="" className="h-full w-full object-cover" />
                          ) : (
                            <span className="grid h-full w-full place-items-center bg-slate-100 text-[9px] font-bold text-slate-500">
                              {item.type === 'image' ? 'PHOTO' : 'VIDEO'}
                            </span>
                          )}
                        </button>
                      ))}
                    </div>
                  ) : (
                    <p className="rounded-xl border border-slate-200 bg-slate-50 px-3 py-2.5 text-center text-[11px] text-slate-500">
                      No active stories on this profile.
                    </p>
                  )}
                </div>
              )}

              </div>

              <div className="mx-auto w-full max-w-md min-w-0 self-center">
                <p className="mb-2 text-center text-[10px] font-bold uppercase tracking-wider text-slate-500">
                  Download options
                </p>
                <div className="space-y-2">
                  {mode === 'video' && (
                    <>
                      <button type="button" onClick={() => handleSaveOption('hd')} className={OPTION_CLASS}>
                        <span className="flex items-center gap-2 text-xs font-bold text-white">
                          <Download className="h-4 w-4 text-white" aria-hidden="true" />
                          Without watermark HD <AdBadge />
                        </span>
                        <span className="shrink-0 text-[10px] font-semibold text-white/85">
                          {selectedQuality?.label}
                          {selectedQuality?.size > 0 ? ` · ${formatBytes(selectedQuality.size)}` : ''}
                        </span>
                      </button>
                      {sdOption && (
                        <button type="button" onClick={() => handleSaveOption('sd')} className={OPTION_CLASS}>
                          <span className="flex items-center gap-2 text-xs font-bold text-white">
                            <Download className="h-4 w-4 text-white/90" aria-hidden="true" />
                            Without watermark <AdBadge />
                          </span>
                          <span className="shrink-0 text-[10px] font-semibold text-white/85">
                            {sdOption.label}
                            {sdOption.size > 0 ? ` · ${formatBytes(sdOption.size)}` : ''}
                          </span>
                        </button>
                      )}
                      <button type="button" onClick={() => handleSaveOption('mp3')} className={OPTION_CLASS}>
                        <span className="flex items-center gap-2 text-xs font-bold text-white">
                          <Download className="h-4 w-4 text-white/90" aria-hidden="true" />
                          Download MP3 <AdBadge />
                        </span>
                        <span className="shrink-0 text-[10px] font-semibold text-white/85">192 kbps</span>
                      </button>
                    </>
                  )}
                  {mode === 'mp3' && (
                    <button type="button" onClick={() => handleSaveOption('mp3')} className={OPTION_CLASS}>
                      <span className="flex items-center gap-2 text-xs font-bold text-white">
                        <Download className="h-4 w-4 text-white" aria-hidden="true" />
                        Download MP3 <AdBadge />
                      </span>
                      <span className="shrink-0 text-[10px] font-semibold text-white/85">192 kbps</span>
                    </button>
                  )}
                  {mode === 'story' && (
                    <button
                      type="button"
                      disabled={!storyId}
                      onClick={() => handleSaveOption('story')}
                      className={OPTION_CLASS}
                    >
                      <span className="flex items-center gap-2 text-xs font-bold text-white">
                        <Download className="h-4 w-4 text-white" aria-hidden="true" />
                        Download Story <AdBadge />
                      </span>
                      <span className="shrink-0 text-[10px] font-semibold text-white/85">MP4</span>
                    </button>
                  )}
                </div>
              </div>
            </div>
          )}

          {state === 'direct' && info && (
            <div className="w-full max-w-sm">
              <span className="mx-auto grid h-14 w-14 place-items-center rounded-2xl bg-teal-500/10 text-teal-600 ring-1 ring-inset ring-teal-400/30">
                <Link2 className="h-7 w-7" aria-hidden="true" />
              </span>
              <p className="mt-4 text-sm font-bold text-slate-900">Direct video link found</p>
              <p className="mt-1 break-all font-mono text-[11px] text-slate-500">
                {info.filename || info.url}
              </p>
              <div className="mt-5 space-y-2">
                <button type="button" onClick={() => handleSaveOption('direct')} className={OPTION_CLASS}>
                  <span className="flex items-center gap-2 text-xs font-bold text-white">
                    <Download className="h-4 w-4 text-white" aria-hidden="true" />
                    Save file <AdBadge />
                  </span>
                  <span className="shrink-0 text-[10px] font-semibold text-white/85">Original file</span>
                </button>
              </div>
            </div>
          )}

          {state === 'error' && (
            <div className="w-full max-w-sm">
              <span className="mx-auto grid h-14 w-14 place-items-center rounded-2xl bg-rose-500/10 text-rose-600 ring-1 ring-inset ring-rose-400/20">
                <AlertTriangle className="h-7 w-7" aria-hidden="true" />
              </span>
              <p className="mt-4 text-sm font-bold text-rose-600">Couldn’t download that</p>
              <p className="mt-2 break-words text-xs leading-relaxed text-slate-500">{error}</p>
            </div>
          )}

          {state === 'ready' && blobUrl && (
            <div className="w-full max-w-sm">
              <span className="mx-auto grid h-14 w-14 place-items-center rounded-2xl bg-emerald-500/10 text-emerald-600 ring-1 ring-inset ring-emerald-400/20">
                <Check className="h-7 w-7" aria-hidden="true" />
              </span>
              <p className="mt-4 text-sm font-bold text-slate-900">File ready to save</p>
              <p className="mt-1 text-xs text-slate-500">
                Saved to your default Downloads folder. Use the buttons below to save it again.
              </p>
              <p className="mt-3 truncate font-mono text-xs text-slate-500" title={fileName}>
                {fileName}
              </p>
              {fileSize > 0 && (
                <p className="mt-1 text-[11px] font-semibold text-teal-600">
                  File size: {formatBytes(fileSize)}
                </p>
              )}
              <div className="mt-4 space-y-2">
                <button
                  type="button"
                  onClick={() => handleSaveAgain()}
                  className="inline-flex w-full items-center justify-center gap-2 rounded-xl bg-gradient-to-r from-[#0d9488] to-[#14b8a6] px-4 py-3 text-xs font-bold text-white shadow-lg shadow-teal-500/20 transition hover:brightness-110 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-teal-400"
                >
                  <Download className="h-4 w-4" aria-hidden="true" />
                  Save file
                </button>
                <button
                  type="button"
                  onClick={() => handleSaveAgain()}
                  className="inline-flex w-full items-center justify-center gap-2 rounded-xl border border-slate-200 bg-slate-100 px-4 py-2.5 text-xs font-bold text-slate-700 transition hover:bg-slate-200 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-teal-500"
                >
                  Save to device (default folder)
                </button>
              </div>
            </div>
          )}
        </div>

        <p className="mt-5 flex items-center justify-center gap-1.5 text-center text-[11px] text-slate-500">
          <ShieldAlert className="h-3.5 w-3.5" aria-hidden="true" />
          Only download content you own or have permission to save.
        </p>
      </section>
      )}

      <BannerAd slot="afterResult" />

      {/* ---------- Why use it ---------- */}
      <section className="card-surface p-5 sm:p-6">
        <Heading icon={Check} title="Why use this downloader" subtitle="Simple, fast and free — every time" />
        <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
          <FeatureCard icon={Download} title="Unlimited & free">
            Save as many TikTok videos as you like. No daily limit, no account and no hidden fee.
          </FeatureCard>
          <FeatureCard icon={Video} title="No watermark">
            Get the original clean video stream without the TikTok logo or username overlay.
          </FeatureCard>
          <FeatureCard icon={Check} title="HD quality">
            Choose from the exact resolutions the video offers, each with its exact size.
          </FeatureCard>
          <FeatureCard icon={Info} title="MP4, MP3 & Stories">
            Save the full video, extract just the audio, or grab an active story from a profile.
          </FeatureCard>
          <FeatureCard icon={Link2} title="Any device">
            Works in Chrome, Safari, Firefox and Edge on desktop, tablet and mobile — nothing to install.
          </FeatureCard>
          <FeatureCard icon={ShieldAlert} title="Private by design">
            Downloads go straight to your browser. Nothing is stored.
          </FeatureCard>
        </div>
      </section>

      <BannerAd slot="afterFeatures" />

      {/* ---------- Supported links ---------- */}
      <section className="card-surface p-5 sm:p-6">
        <Heading icon={Link2} title="Supported links" subtitle="This edition handles TikTok and direct media files" />
        <div className="flex flex-wrap gap-2">
          {['TikTok video links', 'Direct .mp4 / .webm', 'MP3 audio', 'TikTok Stories'].map((item) => (
            <span
              key={item}
              className="rounded-full border border-slate-200 bg-slate-50 px-3 py-1.5 text-[11px] font-semibold text-slate-700"
            >
              {item}
            </span>
          ))}
        </div>
      </section>

      <BannerAd slot="afterPlatforms" />

      {/* ---------- How it works ---------- */}
      <section className="card-surface p-5 sm:p-6">
        <Heading icon={Info} title="How to download" subtitle="Three simple steps to save your file" />
        <div className="grid gap-3 sm:grid-cols-3">
          <Step number="1" title="Find the video">
            Open TikTok, find the clip you want and play it to confirm it is the right one.
          </Step>
          <Step number="2" title="Copy the link">
            Tap Share, then choose <strong className="text-slate-800">Copy link</strong>.
          </Step>
          <Step number="3" title="Pick & save">
            Paste the link above and click <strong className="text-teal-600">Download</strong>. Choose
            a format, then close the ad to start the download.
          </Step>
        </div>
      </section>

      {/* ---------- Detailed guide ---------- */}
      <section className="card-surface p-5 sm:p-6">
        <Heading icon={Video} title="More ways to save" subtitle="Everything you can do with a single link" />

        <div className="space-y-5 text-xs leading-relaxed text-slate-500">
          <div>
            <h3 className="text-sm font-bold text-slate-900">Download TikTok videos without a watermark</h3>
            <p className="mt-1.5">
              Paste any public TikTok video link and press Download. The thumbnail and the exact
              available resolutions appear — pick one, and the clean MP4 is prepared while the ad is
              open. Close the ad to start the download to your chosen folder (computer) or the default
              Downloads folder (phone).
            </p>
          </div>
          <div className="border-t border-slate-100 pt-5">
            <h3 className="text-sm font-bold text-slate-900">Convert TikTok to MP3</h3>
            <p className="mt-1.5">
              Select <strong className="text-slate-800">MP3</strong> under Format, paste the same video
              link and click Download, then choose Download MP3. The audio track is extracted and
              converted to a 192 kbps MP3 using a bundled encoder — there is nothing to install.
            </p>
          </div>
          <div className="border-t border-slate-100 pt-5">
            <h3 className="text-sm font-bold text-slate-900">Download TikTok Stories</h3>
            <p className="mt-1.5">
              Select <strong className="text-slate-800">Story</strong>, then paste the story’s direct
              media link or a profile link. Active stories appear as thumbnails — pick one, choose
              Download Story, then close the ad to save it as an MP4. Stories disappear after 24 hours,
              and TikTok blocks the story endpoint on some networks.
              If a profile link is blocked, paste the direct story media link instead;
              it is saved straight through your browser.
            </p>
          </div>
        </div>
      </section>

      <BannerAd slot="afterHowTo" />

      {/* ---------- FAQ (directly above the footer area) ---------- */}
      <section className="card-surface p-5 sm:p-6">
        <Heading icon={Info} title="FAQ" subtitle="Common questions, quick answers" />
        <div className="divide-y divide-slate-100">
          {FAQS.map((item) => (
            <details key={item.q} className="group py-3">
              <summary className="flex cursor-pointer list-none items-center justify-between gap-3 rounded-lg text-xs font-bold text-slate-800 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-teal-500">
                {item.q}
                <span className="text-base leading-none text-slate-500 transition group-open:rotate-45" aria-hidden="true">
                  +
                </span>
              </summary>
              <p className="mt-2 text-xs leading-relaxed text-slate-500">{item.a}</p>
            </details>
          ))}
        </div>
        <p className="mt-4 border-t border-slate-100 pt-4 text-[10px] leading-relaxed text-slate-500">
          Not affiliated with TikTok or ByteDance. Use this tool only for content you own or have
          permission to save.
        </p>
      </section>

      <VignetteAd open={vignetteOpen} onClose={closeVignette} canClose={!jobPending} />

      {previewOpen && typeof document !== 'undefined' && createPortal(
        <div
          className="fixed inset-0 z-[110] flex items-center justify-center bg-slate-950/90 p-4 backdrop-blur-sm"
          role="dialog"
          aria-modal="true"
          aria-label="Video preview"
          onMouseDown={(event) => {
            if (event.target === event.currentTarget) closePreview()
          }}
        >
          <div className="relative w-full max-w-3xl">
            <button
              type="button"
              onClick={closePreview}
              aria-label="Close preview"
              className="absolute right-2 top-2 z-10 grid h-9 w-9 place-items-center rounded-full bg-white/90 text-slate-900 shadow-lg transition hover:bg-white focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-teal-500"
            >
              <X className="h-4 w-4" aria-hidden="true" />
            </button>
            {previewIsVideo ? (
              <video
                src={previewSrc}
                controls
                autoPlay
                playsInline
                className="mx-auto max-h-[80dvh] w-full rounded-xl bg-black shadow-2xl"
              />
            ) : (
              <img
                src={previewSrc}
                alt="Video preview"
                className="mx-auto max-h-[80dvh] rounded-xl object-contain shadow-2xl"
              />
            )}
          </div>
        </div>,
        document.body,
      )}
    </div>
  )
}
