import { useEffect, useRef, useState } from 'react'
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
import { classifyUrl, formatBytes } from '../lib/video.js'
import VignetteAd from './VignetteAd.jsx'
import { ADS } from '../config/ads.js'
import FAQ_DATA from '../data/faqs.json'

// Bundled locally so the demo works offline and without CORS dependencies.
const SAMPLE = '/sample-video.mp4'
// Public TikTok clip — used only as a quick demo chip.
const TIKTOK_DEMO = 'https://www.tiktok.com/@therock/video/6829267836783971589'

// FAQ data is shared with the JSON-LD in index.html; keep questions and answers in sync.
const FAQ_SECTIONS = FAQ_DATA.sections

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
  'rounded-md bg-white/15 px-3 py-1.5 text-[11px] font-semibold text-white ring-1 ring-inset ring-white/30 transition hover:bg-white/25 active:scale-[0.98] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-white'

const OPTION_CLASS =
  'flex w-full items-center justify-center gap-3 rounded-lg ss-gradient px-4 py-3 text-center text-white shadow-md shadow-teal-500/20 transition hover:brightness-110 active:scale-[0.98] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-teal-400 disabled:cursor-not-allowed disabled:opacity-50'

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
      <span className="grid h-9 w-9 place-items-center rounded-xl bg-teal-500/10 text-teal-700 ring-1 ring-inset ring-teal-400/30">
        <Icon className="h-4.5 w-4.5" strokeWidth={2} aria-hidden="true" />
      </span>
      <div>
        <h2 className="text-sm font-bold tracking-tight text-slate-900">{title}</h2>
        <p className="text-xs text-slate-600">{subtitle}</p>
      </div>
    </div>
  )
}

function FeatureCard({ icon: Icon, title, children }) {
  return (
    <div className="rounded-2xl border border-slate-200 bg-slate-50 p-4">
      <span className="grid h-9 w-9 place-items-center rounded-xl bg-teal-500/10 text-teal-700 ring-1 ring-inset ring-teal-400/30">
        <Icon className="h-4.5 w-4.5" aria-hidden="true" />
      </span>
      <h3 className="mt-3 text-sm font-bold text-slate-900">{title}</h3>
      <p className="mt-1 text-xs leading-relaxed text-slate-600">{children}</p>
    </div>
  )
}

function Step({ number, title, children }) {
  return (
    <div className="rounded-2xl bg-slate-50 p-4">
      <span className="grid h-7 w-7 place-items-center rounded-lg bg-teal-500/10 text-xs font-black text-teal-700">
        {number}
      </span>
      <h3 className="mt-3 text-sm font-bold text-slate-900">{title}</h3>
      <p className="mt-1 text-xs leading-relaxed text-slate-600">{children}</p>
    </div>
  )
}

/**
 * The download is deliberately URL-based, not Blob-based.
 *
 * A 100 MB file or a 20-minute clip cannot be assembled in JavaScript inside a
 * four-second ad. Instead the server prepares the file in the background from
 * the moment a link is pasted, and closing the ad simply visits a normal
 * download URL. The browser streams it to disk with the same instant hand-off
 * for a tiny clip and a huge one — no fetch() to wait on, no RAM spike.
 */
export default function VideoDownloader({ showToast, onResultStateChange }) {
  const [url, setUrl] = useState('')
  const [platform, setPlatform] = useState('TikTok')
  const [mode, setMode] = useState('video')
  const [quality, setQuality] = useState('720')
  const [storyId, setStoryId] = useState('')
  const [state, setState] = useState('idle') // idle | social | direct | error | unsupported
  const [info, setInfo] = useState(null)
  const [remoteInfo, setRemoteInfo] = useState(null)
  const [infoLoading, setInfoLoading] = useState(false)
  const [serviceReady, setServiceReady] = useState(true)
  const [error, setError] = useState('')
  const [vignetteOpen, setVignetteOpen] = useState(false)
  const [previewOpen, setPreviewOpen] = useState(false)
  const [previewSrc, setPreviewSrc] = useState('')
  const [previewIsVideo, setPreviewIsVideo] = useState(false)
  const [previewSources, setPreviewSources] = useState([])
  const [previewLoading, setPreviewLoading] = useState(false)
  const [previewError, setPreviewError] = useState('')
  const [fileReady, setFileReady] = useState(false)
  const [lastRequest, setLastRequest] = useState(null)

  const prefetchRef = useRef(new Map())
  const prefetchAbortRef = useRef(null)
  const prefetchKeyRef = useRef('')
  const prefetchPromiseRef = useRef(null)
  const preparedRef = useRef(new Set())
  const variantRef = useRef('')
  const abortRef = useRef(null)
  const resultPanelRef = useRef(null)
  const pendingActionRef = useRef(null)
  const vignetteShownRef = useRef(false)
  const lastRequestRef = useRef(null)
  const previewVideoRef = useRef(null)
  const warmAbortRef = useRef(null)

  async function postJson(path, body, signal) {
    const response = await fetch(path, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
      ...(signal ? { signal } : {}),
    })
    const payload = await response.json().catch(() => ({}))
    if (!response.ok) throw new Error(payload.error || `Request failed (${response.status})`)
    return payload
  }

  // Fire-and-forget warm-up. Repeating it for the same file is free: the server
  // reuses the in-flight job or the cached result.
  function kickPrepare(params) {
    if (!params?.url) return
    // A later warm-up carrying resolved CDN URLs is a distinct (better) request;
    // the server still de-duplicates it by the real file cache key.
    const key = [params.url, params.mode, params.quality, params.storyId || '', params.media ? 'm' : ''].join('|')
    if (preparedRef.current.has(key)) return
    preparedRef.current.add(key)
    postJson('/api/video/prepare', params)
      .then(() => { /* warm-up is best effort */ })
      .catch(() => preparedRef.current.delete(key))
  }

  // GET means the browser owns the transfer: it streams to the Downloads folder
  // and never asks JavaScript to hold the whole file in memory.
  function buildDownloadUrl(params) {
    const query = new URLSearchParams({
      url: params.url,
      mode: params.mode,
      quality: params.quality || '720',
    })
    if (params.storyId) query.set('storyId', params.storyId)
    if (params.variant) query.set('variant', params.variant)
    return `/api/video/download?${query.toString()}`
  }

  function triggerBrowserDownload(href, filename) {
    const anchor = document.createElement('a')
    anchor.href = href
    if (filename) anchor.download = filename
    anchor.rel = 'noopener'
    document.body.appendChild(anchor)
    anchor.click()
    anchor.remove()
  }

  function rememberLink(value) {
    try {
      window.localStorage.setItem('tiktok-video-last-link', value)
    } catch {
      // Storage can be disabled — this is only a warm-up hint.
    }
  }

  function defaultPrepareParams(link, metadata) {
    // Never start a file job before /info answers: a hint-less job can only
    // resolve the link the slow way and ends up racing the real CDN job.
    if (!metadata) return []
    const base = {
      url: link.url,
      variant: metadata.variant || variantRef.current,
      media: metadata.media,
    }
    const heights = (metadata.formats || [])
      .map((item) => Number(item.height))
      .filter((height) => Number.isFinite(height) && height > 0)
    const qualities = heights.length
      ? [...new Set(heights.map(String))]
      : [String(metadata.formats?.[0]?.height || '720')]
    return [
      ...qualities.map((quality) => ({ ...base, mode: 'video', quality })),
      { ...base, mode: 'mp3', quality: '720' },
    ]
  }

  // Start reading metadata the moment the link appears — no spinner on click.
  function prefetchInfo(rawUrl, currentMode) {
    const result = classifyUrl(rawUrl)
    if (!result.valid) return
    if (result.kind !== 'social') return
    const key = `${result.url}|${currentMode}`
    if (prefetchRef.current.has(key) || prefetchKeyRef.current === key) return
    prefetchAbortRef.current?.abort()
    const controller = new AbortController()
    prefetchAbortRef.current = controller
    prefetchKeyRef.current = key

    // /info resolves the real CDN mirrors. File preparation starts the moment
    // it answers (see applyInfo / the .then below), not before — a hint-less
    // download would duplicate the same bytes through a slower route.
    const promise = postJson(
      '/api/video/info',
      { url: result.url, mode: currentMode },
      controller.signal,
    )
      .then((body) => {
        if (!body) return
        prefetchRef.current.set(key, body)
        if (body.variant) variantRef.current = body.variant
        rememberLink(result.url)
        defaultPrepareParams(result, body).forEach(kickPrepare)
        return body
      })
      .catch(() => {
        // Best effort — provisional options on screen stay usable.
      })
    prefetchPromiseRef.current = { key, promise }
  }

  useEffect(() => {
    // A returning visitor often uses the same clip. Warm it on load so the file
    // is already waiting by the time the ad appears.
    fetch('/api/health')
      .then((response) => setServiceReady(response.ok))
      .catch(() => setServiceReady(false))
    try {
      const last = window.localStorage.getItem('tiktok-video-last-link')
      if (last) prefetchInfo(last, 'video')
    } catch {
      // Storage can be disabled — warm-up is best effort only.
    }
  }, [])

  useEffect(() => {
    if (state !== 'idle') return undefined
    // Paste calls prefetchInfo directly (instant). Typing is debounced just
    // enough to skip half-written links without delaying a completed paste.
    const timer = window.setTimeout(() => prefetchInfo(url, mode), 80)
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
    },
    [],
  )

  // Keep the result panel in view, but never add a delay before it renders.
  useEffect(() => {
    if (state !== 'social' && state !== 'direct') return
    const timer = window.setTimeout(() => {
      const target = resultPanelRef.current
      if (!target) return
      const box = target.getBoundingClientRect()
      const block = box.height > window.innerHeight - 24 ? 'start' : 'center'
      target.scrollIntoView({ behavior: 'instant', block })
    }, 0)
    return () => window.clearTimeout(timer)
  }, [state])

  useEffect(() => {
    onResultStateChange?.(state !== 'idle')
  }, [state, onResultStateChange])

  function clearResult() {
    abortRef.current?.abort()
    warmAbortRef.current?.abort()
    setFileReady(false)
    setInfo(null)
    setRemoteInfo(null)
    setInfoLoading(false)
    setError('')
    setQuality('720')
    setStoryId('')
    setLastRequest(null)
    lastRequestRef.current = null
    pendingActionRef.current = null
  }

  // Preview URLs come from the same /info answer that powers the download. The
  // player lives inside the result card — no overlay, no popup dialog.
  function previewCandidates(body) {
    const media = body?.media || {}
    const alternates = [
      media.video,
      ...(Array.isArray(media.video_alternates) ? media.video_alternates : []),
      ...Object.values(media.by_height || {}).flat(),
    ]
    return [...new Set(alternates.filter((item) => typeof item === 'string' && /^https?:/i.test(item)))]
  }

  function closePreview() {
    setPreviewOpen(false)
    setPreviewLoading(false)
    setPreviewError('')
    const video = previewVideoRef.current
    if (video) {
      video.pause()
      video.removeAttribute('src')
      video.load()
    }
  }

  function openPreview(body = remoteInfo, directUrl = '') {
    const candidates = directUrl ? [directUrl] : previewCandidates(body)
    setPreviewError('')
    if (candidates.length) {
      setPreviewSources(candidates)
      setPreviewSrc(candidates[0])
      setPreviewIsVideo(true)
      setPreviewLoading(false)
    } else if (body?.thumbnail) {
      setPreviewSources([])
      setPreviewSrc(body.thumbnail)
      setPreviewIsVideo(false)
      setPreviewLoading(false)
    } else {
      setPreviewSources([])
      setPreviewSrc('')
      setPreviewLoading(false)
      setPreviewError('Preview is not available for this link. You can still choose a format below.')
    }
    setPreviewOpen(true)
  }

  function handlePreviewVideoError() {
    const index = previewSources.indexOf(previewSrc)
    const next = index >= 0 ? previewSources[index + 1] : previewSources[0]
    if (next && next !== previewSrc) {
      setPreviewSrc(next)
      return
    }
    if (remoteInfo?.thumbnail) {
      setPreviewIsVideo(false)
      setPreviewSrc(remoteInfo.thumbnail)
      showToast('Video playback was blocked here — showing the cover image.')
      return
    }
    setPreviewLoading(false)
    setPreviewError('Preview is not available for this link. You can still choose a format below.')
  }

  function resetAll() {
    closePreview()
    clearResult()
    setUrl('')
    setState('idle')
  }

  function handleModeChange(next) {
    if (next === mode) return
    clearResult()
    setMode(next)
    setState('idle')
  }

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
    // The ad is a fixed 4s beat. Close only triggers a URL visit — no await.
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

  function applyInfo(body, link) {
    if (!body) return
    setRemoteInfo(body)
    if (body.variant) variantRef.current = body.variant
    rememberLink(link.url)
    const heights = (body.formats || [])
      .map((item) => Number(item.height))
      .filter((height) => Number.isFinite(height) && height > 0)
    if (heights.length) setQuality(String(Math.max(...heights)))
    const firstStory = body.stories?.[0]?.id
    if (firstStory != null) setStoryId(String(firstStory))
    defaultPrepareParams(link, body).forEach(kickPrepare)
  }

  function handleSubmit(event) {
    event.preventDefault()
    clearResult()

    const result = classifyUrl(url)
    if (!result.valid) {
      setInfo(result)
      setError(result.error)
      setState('error')
      return
    }

    setInfo(result)
    // Preview is opt-in now: Download only resolves the link and renders the
    // result card. The visitor taps the preview option when they want to watch.
    closePreview()

    if (result.kind === 'social') {
      const key = `${result.url}|${mode}`
      const cachedBody = prefetchRef.current.get(key)
      // Render on this tick. Exact sizes/thumbnail fill in when metadata lands,
      // but the visitor never watches a spinner after pressing Download.
      setRemoteInfo(cachedBody || null)
      setState('social')
      defaultPrepareParams(result, cachedBody).forEach(kickPrepare)

      if (cachedBody) {
        applyInfo(cachedBody, result)
        return
      }
      const controller = new AbortController()
      abortRef.current = controller
      setInfoLoading(true)
      const inFlight = prefetchPromiseRef.current
      const promise = inFlight?.key === key
        ? inFlight.promise
        : postJson('/api/video/info', { url: result.url, mode }, controller.signal)
      promise
        .then((body) => {
          if (controller.signal.aborted) return
          const resolved = body || prefetchRef.current.get(key)
          applyInfo(resolved, result)
        })
        .catch(() => { /* provisional options stay usable */ })
        .finally(() => {
          if (!controller.signal.aborted) setInfoLoading(false)
        })
      return
    }

    setState('direct')
  }

  function warmDownload(params) {
    warmAbortRef.current?.abort()
    if (params.kind === 'direct') {
      setFileReady(true)
      return
    }
    setFileReady(false)
    const controller = new AbortController()
    warmAbortRef.current = controller
    // Prepare the file in the download function while the ad is on screen.
    // Response headers mean the server already cached the bytes; stop the body
    // transfer here so the click after Close is a plain stream from cache.
    fetch(buildDownloadUrl(params), { signal: controller.signal })
      .then((response) => {
        if (!response.ok) throw new Error('warm-up failed')
        controller.abort()
        setFileReady(true)
      })
      .catch(() => {
        // Never trap the visitor behind the ad: fall back to the normal path.
        setFileReady(true)
      })
  }

  // The ad opens on the same tick as the tap. Close waits for the 4-second ad
  // beat AND the warm-up above, so pressing Close means the file is ready.
  function handleSaveOption(kind) {
    if (vignetteOpen || !info) return
    let params
    if (info.kind === 'direct') {
      params = { kind: 'direct', url: info.url, filename: info.filename, mode: 'video', quality: '720' }
    } else if (kind === 'mp3') {
      params = { kind, url: info.url, mode: 'mp3', quality: '720', variant: variantRef.current }
    } else if (kind === 'story') {
      params = { kind, url: info.url, mode: 'story', quality: '720', storyId, variant: variantRef.current }
    } else {
      const chosen = kind === 'sd' ? sdQuality : selectedQuality?.value || quality
      params = { kind, url: info.url, mode: 'video', quality: chosen, variant: variantRef.current }
    }
    lastRequestRef.current = params
    setLastRequest(params)
    warmDownload(params)
    runWithAd(() => startDownload(params))
  }

  function startDownload(params) {
    if (!params) return
    if (params.kind === 'direct') {
      triggerBrowserDownload(params.url, params.filename)
    } else {
      triggerBrowserDownload(buildDownloadUrl(params))
    }
    showToast('Download started')
  }

  function handleSaveAgain() {
    startDownload(lastRequest || lastRequestRef.current)
  }

  useEffect(() => {
    if (!previewOpen) return undefined
    function onKeyDown(event) {
      if (event.key === 'Escape') closePreview()
    }
    document.addEventListener('keydown', onKeyDown)
    return () => document.removeEventListener('keydown', onKeyDown)
  }, [previewOpen])

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

  // One shared label keeps every option identical and avoids duplicate markup.
  function optionContent(label, meta) {
    return (
      <>
        <span className="flex items-center gap-2 text-xs font-bold text-white">
          <Download className="h-4 w-4 text-white" aria-hidden="true" />
          {label} <AdBadge />
        </span>
        <span className="shrink-0 text-[10px] font-semibold text-white/85">{meta}</span>
      </>
    )
  }

  return (
    <div className="space-y-6">
      {/* ---------- Hero: hidden once Download is clicked ---------- */}
      {state === 'idle' && (
      <section className="ss-gradient rounded-2xl px-5 py-8 text-white shadow-lg shadow-teal-500/20 sm:px-10 sm:py-10">
        <div className="mx-auto max-w-3xl text-center">
          <h2
            className="animate-rise-in text-2xl font-extrabold tracking-tight text-white sm:text-[32px] motion-reduce:animate-none"
            style={{ animationDelay: '60ms' }}
          >
            TikTok Video Downloader HD
          </h2>
          <p
            className="animate-rise-in mt-2 text-sm leading-relaxed text-white/80 motion-reduce:animate-none"
            style={{ animationDelay: '130ms' }}
          >
            Download TikTok videos without watermark. Paste a link, choose a format and save it in seconds.
          </p>
        </div>

        <form
          onSubmit={handleSubmit}
          className="animate-rise-in mx-auto mt-6 max-w-3xl motion-reduce:animate-none"
          style={{ animationDelay: '200ms' }}
        >
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
              className="inline-flex w-full items-center justify-center gap-2 rounded-lg bg-white px-5 py-4 text-sm font-semibold text-teal-700 transition hover:bg-white/90 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-white sm:w-auto sm:min-w-[6.5rem]"
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
              className="inline-flex w-full items-center justify-center gap-2 rounded-lg bg-teal-700 px-6 py-4 text-base font-semibold text-white shadow-lg shadow-teal-900/20 transition hover:brightness-110 active:scale-[0.98] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-white sm:w-auto sm:min-w-[9rem]"
            >
              <Download className="h-5 w-5" aria-hidden="true" />
              Download
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
                      ? 'bg-white text-teal-700'
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

        <div
          className="animate-rise-in mx-auto mt-5 grid max-w-3xl gap-2 sm:grid-cols-3 motion-reduce:animate-none"
          style={{ animationDelay: '260ms' }}
        >
          <MiniBenefit>Unlimited saves</MiniBenefit>
          <MiniBenefit>No watermark</MiniBenefit>
          <MiniBenefit>HD quality</MiniBenefit>
          <MiniBenefit>MP4, MP3 &amp; Stories</MiniBenefit>
          <MiniBenefit>No login or app</MiniBenefit>
          <MiniBenefit>Fast &amp; free</MiniBenefit>
        </div>

        <div
          className="animate-rise-in mx-auto mt-4 flex max-w-3xl flex-wrap items-center justify-center gap-2 motion-reduce:animate-none"
          style={{ animationDelay: '320ms' }}
        >
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

      {/* ---------- Result: rendered on the same tick as the click ---------- */}
      {state !== 'idle' && (
      <section ref={resultPanelRef} className="card-surface scroll-mt-4 p-5 sm:p-6">
        <div className="mb-5 flex items-center justify-between">
          <h2 className="text-sm font-bold text-slate-900">Your download</h2>
          <button
            type="button"
            onClick={resetAll}
            className="inline-flex items-center gap-1.5 rounded-lg px-2 py-1.5 text-[11px] font-semibold text-slate-600 transition hover:bg-slate-200 hover:text-slate-900 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-teal-500"
          >
            <RotateCcw className="h-3.5 w-3.5" aria-hidden="true" />
            Clear
          </button>
        </div>

        <div className="flex min-h-[300px] flex-col items-center justify-center gap-3 rounded-2xl bg-slate-50 p-5 text-center ring-1 ring-inset ring-slate-200">
          {state === 'unsupported' && info && (
            <div className="w-full max-w-sm">
              <span className="mx-auto grid h-14 w-14 place-items-center rounded-2xl bg-amber-500/10 text-amber-700 ring-1 ring-inset ring-amber-400/20">
                <AlertTriangle className="h-7 w-7" aria-hidden="true" />
              </span>
              <p className="mt-4 text-sm font-bold text-amber-700">
                {info.platform} download failed
              </p>
              <p className="mt-2 text-xs leading-relaxed text-slate-600">
                {serviceReady ? (
                  <>This link could not be processed. The platform may be blocking the request, or the clip may be private or restricted.</>
                ) : (
                  <>The download service is unavailable right now. Start the local helper and try again.</>
                )}
              </p>
              {error && <p className="mt-3 break-words text-[11px] text-rose-600">{error}</p>}
            </div>
          )}

          {state === 'social' && info && (
            <div className="grid w-full max-w-3xl gap-5 sm:grid-cols-[minmax(0,0.8fr)_minmax(0,1.2fr)]">
              <div className="min-w-0">
                {previewOpen && previewLoading ? (
                  <div className="mx-auto flex h-40 items-center justify-center gap-2 rounded-xl bg-slate-100 text-center sm:mx-0 sm:h-56">
                    <Loader2 className="h-6 w-6 animate-spin text-teal-600" aria-hidden="true" />
                    <span className="text-[11px] font-semibold text-slate-600">Loading preview…</span>
                  </div>
                ) : previewOpen && previewError ? (
                  <div className="mx-auto flex h-40 flex-col items-center justify-center gap-2 rounded-xl bg-slate-100 p-4 text-center sm:mx-0 sm:h-56">
                    <AlertTriangle className="h-6 w-6 text-amber-600" aria-hidden="true" />
                    <p className="text-[11px] font-semibold text-slate-700">{previewError}</p>
                    <button
                      type="button"
                      onClick={() => {
                        setPreviewError('')
                        setPreviewOpen(false)
                      }}
                      className="rounded-full bg-white px-3 py-1.5 text-[10px] font-bold text-slate-700 shadow ring-1 ring-slate-200 transition hover:bg-slate-50 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-teal-500"
                    >
                      Show cover
                    </button>
                  </div>
                ) : previewOpen && previewIsVideo && previewSrc ? (
                  <div className="relative">
                    <video
                      ref={previewVideoRef}
                      src={previewSrc}
                      poster={remoteInfo?.thumbnail || undefined}
                      controls
                      autoPlay
                      muted
                      playsInline
                      preload="auto"
                      onCanPlay={(event) => event.currentTarget.play().catch(() => {})}
                      onError={handlePreviewVideoError}
                      className="animate-preview-in mx-auto max-h-64 w-full rounded-xl bg-black shadow-lg motion-reduce:animate-none sm:mx-0"
                    />
                    <button
                      type="button"
                      onClick={closePreview}
                      className="absolute right-2 top-2 rounded-full bg-slate-900/75 px-2.5 py-1 text-[10px] font-bold text-white shadow ring-1 ring-white/40 transition hover:bg-slate-900 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-teal-400"
                    >
                      Stop preview
                    </button>
                  </div>
                ) : previewSrc || remoteInfo?.thumbnail ? (
                  <div className="relative">
                    <img
                      src={previewSrc || remoteInfo.thumbnail}
                      alt=""
                      className="animate-preview-in mx-auto max-h-64 w-full rounded-xl object-cover shadow-lg motion-reduce:animate-none sm:mx-0"
                    />
                    {!previewOpen && (
                      <button
                        type="button"
                        onClick={() => openPreview()}
                        aria-label="Preview this video"
                        className="absolute inset-0 m-auto flex h-fit w-fit items-center gap-1.5 rounded-full bg-slate-900/75 px-3 py-2 text-[11px] font-bold text-white shadow-lg ring-1 ring-white/40 backdrop-blur-sm transition hover:bg-slate-900/90 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-teal-400"
                      >
                        <Play className="h-3.5 w-3.5 fill-white" aria-hidden="true" />
                        Preview
                      </button>
                    )}
                  </div>
                ) : (
                  <div className="flex h-40 flex-col items-center justify-center gap-2 rounded-xl bg-gradient-to-br from-slate-200 to-slate-100 sm:h-56">
                    <Video className="h-10 w-10 text-slate-400" aria-hidden="true" />
                    {!previewOpen && previewCandidates(remoteInfo).length > 0 && (
                      <button
                        type="button"
                        onClick={() => openPreview()}
                        className="inline-flex items-center gap-1.5 rounded-full bg-white px-3 py-1.5 text-[11px] font-bold text-teal-700 shadow ring-1 ring-slate-200 transition hover:bg-teal-50 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-teal-500"
                      >
                        <Play className="h-3.5 w-3.5 fill-teal-700" aria-hidden="true" />
                        Preview
                      </button>
                    )}
                  </div>
                )}
                <div className="mt-3 flex flex-wrap items-center justify-center gap-2 sm:justify-start">
                  <span className="rounded-full bg-teal-500/10 px-2.5 py-1 text-[10px] font-black uppercase tracking-wider text-teal-700">
                    {remoteInfo?.platform || info.platform || 'TikTok'}
                  </span>
                  {remoteInfo?.duration ? (
                    <span className="rounded-full bg-slate-100 px-2.5 py-1 text-[10px] font-semibold text-slate-600">
                      {formatDuration(remoteInfo.duration)}
                    </span>
                  ) : null}
                  <span className="rounded-full bg-emerald-500/10 px-2.5 py-1 text-[10px] font-semibold text-emerald-600">
                    No watermark
                  </span>
                  {infoLoading && (
                    <span className="inline-flex items-center gap-1 rounded-full bg-slate-100 px-2.5 py-1 text-[10px] font-semibold text-slate-600">
                      <Loader2 className="h-3 w-3 animate-spin" aria-hidden="true" />
                      Details
                    </span>
                  )}
                </div>
                <p className="mt-3 text-sm font-bold text-slate-900">
                  {remoteInfo?.uploader || 'TikTok video'}
                </p>
                <p className="mt-1 line-clamp-2 text-xs leading-relaxed text-slate-600">
                  {remoteInfo?.title || 'Your file is being prepared in the background.'}
                </p>

                {mode === 'video' ? (
                  <div className="mt-5">
                    <p className="mb-2 text-[10px] font-bold uppercase tracking-wider text-slate-600">
                      Available qualities
                    </p>
                    <div role="group" aria-label="Video quality" className="space-y-2">
                      {qualityOptions.map((item, index) => (
                        <button
                          key={item.value}
                          type="button"
                          aria-pressed={quality === item.value}
                          onClick={() => setQuality(item.value)}
                          style={{ animationDelay: `${index * 45}ms` }}
                          className={`animate-option-in flex w-full items-center justify-between rounded-xl border px-3 py-2 text-[11px] font-bold transition active:scale-[0.99] motion-reduce:animate-none focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-teal-500 ${
                            quality === item.value
                              ? 'border-teal-400/50 bg-teal-500/15 text-slate-900 shadow-sm'
                              : 'border-slate-200 bg-slate-50 text-slate-600 hover:border-teal-400/40 hover:text-slate-900'
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
                    <p className="mt-2 text-[10px] text-slate-600">
                      Only the qualities this video actually offers are shown.
                    </p>
                  </div>
                ) : mode === 'mp3' ? (
                  <div className="mt-5 rounded-xl border border-slate-200 bg-slate-50 px-3 py-2.5 text-center text-[11px] font-semibold text-slate-600">
                    Audio only · 192 kbps MP3
                  </div>
                ) : (
                  <div className="mt-5">
                    <p className="mb-2 text-[10px] font-bold uppercase tracking-wider text-slate-600">
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
                              <span className="grid h-full w-full place-items-center bg-slate-100 text-[9px] font-bold text-slate-600">
                                {item.type === 'image' ? 'PHOTO' : 'VIDEO'}
                              </span>
                            )}
                          </button>
                        ))}
                      </div>
                    ) : (
                      <p className="rounded-xl border border-slate-200 bg-slate-50 px-3 py-2.5 text-center text-[11px] text-slate-600">
                        {infoLoading ? 'Loading stories…' : 'No active stories on this profile.'}
                      </p>
                    )}
                  </div>
                )}
              </div>

              <div className="mx-auto w-full max-w-md min-w-0 self-center">
                <p className="mb-2 text-center text-[10px] font-bold uppercase tracking-wider text-slate-600">
                  Download options
                </p>
                <div className="space-y-2">
                  {mode === 'video' && (
                    <>
                      <button
                        type="button"
                        onClick={() => handleSaveOption('hd')}
                        style={{ animationDelay: '30ms' }}
                        className={`${OPTION_CLASS} animate-option-in motion-reduce:animate-none`}
                      >
                        {optionContent(
                          'Without watermark HD',
                          `${selectedQuality?.label || ''}${selectedQuality?.size > 0 ? ` · ${formatBytes(selectedQuality.size)}` : ''}`,
                        )}
                      </button>
                      {sdOption && (
                        <button
                          type="button"
                          onClick={() => handleSaveOption('sd')}
                          style={{ animationDelay: '90ms' }}
                          className={`${OPTION_CLASS} animate-option-in motion-reduce:animate-none`}
                        >
                          {optionContent(
                            'Without watermark',
                            `${sdOption.label}${sdOption.size > 0 ? ` · ${formatBytes(sdOption.size)}` : ''}`,
                          )}
                        </button>
                      )}
                      <button
                        type="button"
                        onClick={() => handleSaveOption('mp3')}
                        style={{ animationDelay: '150ms' }}
                        className={`${OPTION_CLASS} animate-option-in motion-reduce:animate-none`}
                      >
                        {optionContent('Download MP3', '192 kbps')}
                      </button>
                    </>
                  )}
                  {mode === 'mp3' && (
                    <button
                      type="button"
                      onClick={() => handleSaveOption('mp3')}
                      style={{ animationDelay: '60ms' }}
                      className={`${OPTION_CLASS} animate-option-in motion-reduce:animate-none`}
                    >
                      {optionContent('Download MP3', '192 kbps')}
                    </button>
                  )}
                  {mode === 'story' && (
                    <button
                      type="button"
                      disabled={!storyId}
                      onClick={() => handleSaveOption('story')}
                      style={{ animationDelay: '60ms' }}
                      className={`${OPTION_CLASS} animate-option-in motion-reduce:animate-none`}
                    >
                      {optionContent('Download Story', 'MP4')}
                    </button>
                  )}
                </div>
                <button
                  type="button"
                  onClick={handleSaveAgain}
                  disabled={!lastRequest}
                  className="mt-3 w-full rounded-lg border border-slate-200 bg-white px-3 py-2 text-[11px] font-semibold text-slate-600 transition hover:bg-slate-100 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-teal-500 disabled:cursor-not-allowed disabled:opacity-40"
                >
                  Save again
                </button>
              </div>
            </div>
          )}

          {state === 'direct' && info && (
            <div className="w-full max-w-sm">
              {previewOpen && previewLoading ? (
                <div className="mb-4 flex h-40 items-center justify-center gap-2 rounded-xl bg-slate-100 text-center">
                  <Loader2 className="h-6 w-6 animate-spin text-teal-600" aria-hidden="true" />
                  <span className="text-[11px] font-semibold text-slate-600">Loading preview…</span>
                </div>
              ) : previewOpen && previewError ? (
                <p className="mb-4 rounded-xl bg-slate-100 p-3 text-[11px] font-semibold text-slate-700">
                  {previewError}
                </p>
              ) : previewOpen && previewIsVideo && previewSrc ? (
                <div className="relative mb-4">
                  <video
                    ref={previewVideoRef}
                    src={previewSrc}
                    controls
                    autoPlay
                    muted
                    playsInline
                    preload="auto"
                    onCanPlay={(event) => event.currentTarget.play().catch(() => {})}
                    onError={handlePreviewVideoError}
                    className="mx-auto max-h-56 w-full rounded-xl bg-black shadow-lg"
                  />
                  <button
                    type="button"
                    onClick={closePreview}
                    className="absolute right-2 top-2 rounded-full bg-slate-900/75 px-2.5 py-1 text-[10px] font-bold text-white shadow ring-1 ring-white/40 transition hover:bg-slate-900 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-teal-400"
                  >
                    Stop preview
                  </button>
                </div>
              ) : !previewOpen ? (
                <button
                  type="button"
                  onClick={() => openPreview(null, info.url)}
                  className="mb-4 inline-flex items-center gap-1.5 rounded-full bg-teal-500/10 px-3 py-1.5 text-[11px] font-bold text-teal-700 transition hover:bg-teal-500/20 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-teal-500"
                >
                  <Play className="h-3.5 w-3.5 fill-teal-700" aria-hidden="true" />
                  Preview video
                </button>
              ) : null}
              <span className="mx-auto grid h-14 w-14 place-items-center rounded-2xl bg-teal-500/10 text-teal-700 ring-1 ring-inset ring-teal-400/30">
                <Link2 className="h-7 w-7" aria-hidden="true" />
              </span>
              <p className="mt-4 text-sm font-bold text-slate-900">Direct video link found</p>
              <p className="mt-1 break-all font-mono text-[11px] text-slate-600">
                {info.filename || info.url}
              </p>
              <div className="mt-5 space-y-2">
                <button
                  type="button"
                  onClick={() => handleSaveOption('direct')}
                  style={{ animationDelay: '60ms' }}
                  className={`${OPTION_CLASS} animate-option-in motion-reduce:animate-none`}
                >
                  {optionContent('Save file', 'Original file')}
                </button>
                <button
                  type="button"
                  onClick={handleSaveAgain}
                  disabled={!lastRequest}
                  className="w-full rounded-lg border border-slate-200 bg-white px-3 py-2 text-[11px] font-semibold text-slate-600 transition hover:bg-slate-100 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-teal-500 disabled:cursor-not-allowed disabled:opacity-40"
                >
                  Save again
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
              <p className="mt-2 break-words text-xs leading-relaxed text-slate-600">{error}</p>
            </div>
          )}
        </div>

        <p className="mt-5 flex items-center justify-center gap-1.5 text-center text-[11px] text-slate-600">
          <ShieldAlert className="h-3.5 w-3.5" aria-hidden="true" />
          Only download content you own or have permission to save.
        </p>
      </section>
      )}


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
            Paste the link above and click <strong className="text-teal-700">Download</strong>. Choose
            a format, then close the ad to start the download.
          </Step>
        </div>
      </section>


      {/* ---------- Detailed guide ---------- */}
      <section className="card-surface p-5 sm:p-6">
        <Heading icon={Video} title="More ways to save" subtitle="Everything you can do with a single link" />

        <div className="space-y-5 text-xs leading-relaxed text-slate-600">
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

        <div className="mt-5 rounded-xl border border-teal-100 bg-teal-50/70 p-4">
          <h3 className="text-sm font-bold text-slate-900">Step-by-step guides</h3>
          <p className="mt-1 text-xs leading-relaxed text-slate-600">
            Want more detail? Read the written guides, then come back and paste your link.
          </p>
          <ul className="mt-3 flex flex-wrap gap-x-5 gap-y-2 text-xs font-semibold">
            <li>
              <a className="text-teal-700 underline decoration-teal-300 underline-offset-2 hover:text-teal-800" href="/guides/how-to-download-tiktok-videos/">
                How to download TikTok videos
              </a>
            </li>
            <li>
              <a className="text-teal-700 underline decoration-teal-300 underline-offset-2 hover:text-teal-800" href="/guides/tiktok-mp3-downloader/">
                TikTok MP3 downloader
              </a>
            </li>
            <li>
              <a className="text-teal-700 underline decoration-teal-300 underline-offset-2 hover:text-teal-800" href="/guides/tiktok-story-downloader/">
                TikTok story downloader
              </a>
            </li>
            <li>
              <a className="text-teal-700 underline decoration-teal-300 underline-offset-2 hover:text-teal-800" href="/guides/tiktok-video-downloader-on-mobile/">
                Download on Android &amp; iPhone
              </a>
            </li>
          </ul>
        </div>
      </section>

      {/* ---------- FAQ (directly above the footer area) ---------- */}
      <section id="faq" className="card-surface p-5 sm:p-6">
        <Heading icon={Info} title="FAQ" subtitle="30 common questions, quick answers" />
        <div className="space-y-6">
          {FAQ_SECTIONS.map((section) => (
            <div key={section.title}>
              <h3 className="mb-2 text-[11px] font-extrabold uppercase tracking-wider text-teal-700">
                {section.title}
              </h3>
              <div className="divide-y divide-slate-100">
                {section.items.map((item) => (
                  <details key={item.q} className="group py-3">
                    <summary className="flex cursor-pointer list-none items-center justify-between gap-3 rounded-lg text-xs font-bold text-slate-800 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-teal-500">
                      {item.q}
                      <span className="text-base leading-none text-slate-600 transition group-open:rotate-45" aria-hidden="true">
                        +
                      </span>
                    </summary>
                    <p className="mt-2 text-xs leading-relaxed text-slate-600">{item.a}</p>
                  </details>
                ))}
              </div>
            </div>
          ))}
        </div>
        <p className="mt-6 border-t border-slate-100 pt-4 text-[10px] leading-relaxed text-slate-600">
          Not affiliated with TikTok or ByteDance. Use this tool only for content you own or have
          permission to save.
        </p>
      </section>

      <VignetteAd open={vignetteOpen} ready={fileReady} onClose={closeVignette} />

    </div>
  )
}
