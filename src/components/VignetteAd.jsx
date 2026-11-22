import { useEffect, useRef, useState } from 'react'
import { createPortal } from 'react-dom'
import { X } from 'lucide-react'
import { ADS } from '../config/ads.js'

const LOADER_ID = 'adsbygoogle-loader'

function loadAdsense(client) {
  if (document.getElementById(LOADER_ID)) return
  const script = document.createElement('script')
  script.id = LOADER_ID
  script.async = true
  script.crossOrigin = 'anonymous'
  script.src = `https://pagead2.googlesyndication.com/pagead/js/adsbygoogle.js?client=${encodeURIComponent(client)}`
  document.head.appendChild(script)
}

/**
 * Interstitial ad. Rendered when the visitor taps a download
 * option: a full-screen ad with Close and Remove ads controls. Both use the
 * same guarded action — closing starts the save step.
 *
 * The modal is portalled to <body> so no animated/transformed page wrapper can
 * turn `position: fixed` into a document-relative box. It stays pinned to the
 * viewport and scales down cleanly on phones and very small windows.
 * Everything is controlled from src/config/ads.js (vignette section).
 */
export default function VignetteAd({ open, onClose, canClose = true }) {
  const contentRef = useRef(null)
  const pushedRef = useRef(false)
  const scriptsRef = useRef(false)
  // The close control appears only after the ad beat AND the file is fully
  // prepared. Showing it early (even disabled) makes visitors think the file
  // is processing *after* they close the ad.
  const [showClose, setShowClose] = useState(false)

  const client = ADS.adsense?.client || ''
  const config = ADS.vignette || {}
  const canRun = Boolean(ADS.enabled && config.enabled)
  const slot = typeof config.slot === 'string' ? config.slot.trim() : ''
  const html = typeof config.html === 'string' ? config.html.trim() : ''

  const useCustom = Boolean(canRun && html)
  const useAdsense = Boolean(canRun && !useCustom && client)
  const useSlot = useAdsense && Boolean(slot)

  function handleClose() {
    if (canClose) onClose()
  }

  // Both close controls stay hidden while the file is still being prepared.
  const readyToClose = showClose && canClose

  useEffect(() => {
    if (!open || !canRun) {
      setShowClose(false)
      return undefined
    }
    const timer = window.setTimeout(() => setShowClose(true), 3000)
    return () => window.clearTimeout(timer)
  }, [open, canRun])

  useEffect(() => {
    if (!open) {
      pushedRef.current = false
      scriptsRef.current = false
      return undefined
    }

    // Keep the page behind the modal from scrolling.
    const previousOverflow = document.body.style.overflow
    document.body.style.overflow = 'hidden'

    if (useCustom && contentRef.current && !scriptsRef.current) {
      // dangerouslySetInnerHTML does not run <script> tags. Recreate them so
      // Adsterra / Monetag snippets that include a script actually work.
      scriptsRef.current = true
      contentRef.current.querySelectorAll('script').forEach((old) => {
        if (old.dataset.adExecuted) return
        old.dataset.adExecuted = 'true'
        const fresh = document.createElement('script')
        for (const attr of old.attributes) fresh.setAttribute(attr.name, attr.value)
        fresh.textContent = old.textContent
        old.replaceWith(fresh)
      })
    }

    if (useAdsense) {
      loadAdsense(client)
      if (useSlot && contentRef.current && !pushedRef.current) {
        // The <ins> only exists while the modal is open — push once per open.
        pushedRef.current = true
        try {
          ;(window.adsbygoogle = window.adsbygoogle || []).push({})
        } catch {
          // Script blocked / ad blocker — the queued push is harmless.
        }
      }
    }

    function onKeyDown(event) {
      if (event.key === 'Escape' && canClose) onClose()
    }
    document.addEventListener('keydown', onKeyDown)

    return () => {
      document.body.style.overflow = previousOverflow
      document.removeEventListener('keydown', onKeyDown)
    }
  }, [open, onClose, canClose, useCustom, useAdsense, useSlot, client])

  if (!open || !canRun || typeof document === 'undefined') return null

  const modal = (
    <div
      className="fixed inset-0 z-[100] flex items-center justify-center bg-slate-900/80 p-3 backdrop-blur-sm sm:p-4"
      role="dialog"
      aria-modal="true"
      aria-label="Advertisement"
      onMouseDown={(event) => {
        if (canClose && event.target === event.currentTarget) onClose()
      }}
    >
      <div className="relative flex max-h-[calc(100dvh-1.5rem)] w-full max-w-3xl flex-col overflow-hidden rounded-2xl border border-slate-200 bg-white shadow-2xl sm:max-h-[calc(100dvh-2rem)]">
        {readyToClose && (
          <button
            type="button"
            disabled={!canClose}
            aria-disabled={!canClose}
            title={canClose ? undefined : 'Preparing your file…'}
            onClick={handleClose}
            className="animate-ad-close-in absolute right-2.5 top-2.5 z-10 inline-flex items-center gap-1 rounded-full bg-white px-2.5 py-1.5 text-[11px] font-bold text-slate-800 shadow-lg ring-1 ring-slate-200 transition hover:bg-teal-50 hover:text-teal-700 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-teal-500 disabled:cursor-not-allowed disabled:opacity-75 disabled:hover:bg-white disabled:hover:text-slate-800 sm:right-4 sm:top-4 sm:gap-1.5 sm:px-3.5 sm:py-2 sm:text-xs"
          >
            <X className="h-3.5 w-3.5 sm:h-4 sm:w-4" aria-hidden="true" />
            {config.closeLabel || 'Close'}
          </button>
        )}

        <div className="flex min-h-[52px] items-center justify-between gap-2 border-b border-slate-200 py-2 pl-3 pr-24 sm:min-h-[56px] sm:py-2.5 sm:pl-4 sm:pr-32">
          <span className="text-[10px] font-bold uppercase tracking-wider text-slate-500 sm:text-[11px]">
            Advertisement
          </span>
          <span className="text-right text-[9px] font-semibold text-slate-400 sm:text-[10px]">
            {!canClose
              ? 'Preparing your file…'
              : !showClose
                ? 'Close appears in a moment'
                : ''}
          </span>
        </div>

        <div
          ref={contentRef}
          className="flex min-h-[200px] flex-1 items-center justify-center overflow-y-auto p-3 sm:min-h-[280px] sm:p-4"
        >
          {useCustom ? (
            <div className="w-full" dangerouslySetInnerHTML={{ __html: html }} />
          ) : useSlot ? (
            <ins
              className="adsbygoogle block w-full"
              style={{ display: 'block' }}
              data-ad-client={client}
              data-ad-slot={slot}
              data-ad-format="auto"
              data-full-width-responsive="true"
            />
          ) : useAdsense ? (
            // Auto-ads mode: Google places the interstitial itself. This small
            // unstyled unit gives it a mount point inside the modal.
            <ins className="adsbygoogle block w-full" style={{ display: 'block' }} data-ad-client={client} />
          ) : (
            <p className="text-center text-xs text-slate-600">
              Ad network config missing — closing will start the save step.
            </p>
          )}
        </div>

        {readyToClose && (
          <div className="flex flex-col items-center gap-1.5 border-t border-slate-200 bg-slate-50 px-3 py-2 text-center sm:flex-row sm:justify-between sm:gap-3 sm:px-4 sm:text-left">
            <span className="text-[9px] text-slate-400 sm:text-[10px]">Ads keep this tool free.</span>
            <button
              type="button"
              disabled={!canClose}
              onClick={handleClose}
              className="text-[11px] font-semibold text-slate-600 underline decoration-slate-300 underline-offset-2 transition hover:text-teal-700 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-teal-500 disabled:cursor-not-allowed disabled:opacity-75 disabled:no-underline"
            >
              Remove ads
            </button>
          </div>
        )}
      </div>
    </div>
  )

  return createPortal(modal, document.body)
}
