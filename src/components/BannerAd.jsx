import { useEffect, useRef, useState } from 'react'
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
 * Renders one ad slot. Everything is configured in src/config/ads.js.
 *
 * Banner behaviour:
 *  - Manual AdSense unit when the slot has its own ID (or the shared
 *    fallbackSlot is set) — this is what makes every position visible.
 *  - Auto-ad anchor when AdSense auto ads are on but no slot ID is available.
 *  - Custom HTML snippet or a simple image banner when AdSense is off.
 *
 * Unfilled slots are hidden, so no blank reserved box is left in the page.
 */
export default function BannerAd({ slot, className = '' }) {
  const html = typeof ADS.html?.[slot] === 'string' ? ADS.html[slot].trim() : ''
  const image = ADS.slots?.[slot]
  const adsense = ADS.adsense
  const client = adsense?.client || ''

  const adsenseOn = Boolean(ADS.enabled && adsense?.enabled && client)
  const autoAds = Boolean(adsenseOn && adsense?.auto)

  const configuredSlot =
    typeof adsense?.slots?.[slot] === 'string' ? adsense.slots[slot].trim() : ''
  const fallbackSlot =
    typeof adsense?.fallbackSlot === 'string' ? adsense.fallbackSlot.trim() : ''
  // A shared slot ID lets every position run as a real responsive ad unit when
  // the owner only has one ID (the reference site behaves the same way).
  const adsenseSlot = configuredSlot || fallbackSlot

  const useAdsense = Boolean(adsenseOn && adsenseSlot)
  const useHtml = Boolean(ADS.enabled && !adsenseOn && html)
  const useImage = Boolean(
    ADS.enabled && !adsenseOn && !useHtml && image?.enabled && image?.image,
  )
  const useAutoAnchor = Boolean(autoAds && !useAdsense && !useHtml && !useImage)
  const active = Boolean(
    adsenseOn ? useAdsense || useAutoAnchor : useHtml || useImage,
  )

  const fallback = adsense?.fallback || {}
  const fallbackSlots = Array.isArray(fallback.slots) ? fallback.slots : []
  const useFallback = Boolean(
    useAdsense &&
      ADS.enabled &&
      fallback.enabled &&
      fallback.image &&
      (fallbackSlots.length === 0 || fallbackSlots.includes(slot)),
  )
  const fallbackExternal = /^https?:\/\//i.test(fallback.href || '')

  const contentRef = useRef(null)
  const pushedRef = useRef(false)
  const [unfilled, setUnfilled] = useState(false)

  useEffect(() => {
    setUnfilled(false)
  }, [slot])

  useEffect(() => {
    if (!useAdsense || !contentRef.current) return undefined
    const ins = contentRef.current.querySelector('ins.adsbygoogle')
    if (!ins) return undefined

    const checkStatus = () => {
      if (ins.getAttribute('data-ad-status') === 'unfilled') setUnfilled(true)
    }
    checkStatus()
    const observer = new MutationObserver(checkStatus)
    observer.observe(ins, { attributes: true, attributeFilter: ['data-ad-status'] })
    return () => observer.disconnect()
  }, [useAdsense, adsenseSlot, slot])

  useEffect(() => {
    if (!active) return

    if (adsenseOn) {
      loadAdsense(client)
      // One push per mounted slot. React StrictMode runs effects twice in dev,
      // so the ref guard prevents Google seeing a duplicate request.
      if ((useAdsense || useAutoAnchor) && !pushedRef.current && contentRef.current) {
        pushedRef.current = true
        try {
          ;(window.adsbygoogle = window.adsbygoogle || []).push({})
        } catch {
          // Ad blocker or script not loaded yet — queued push is harmless.
        }
      }
      return
    }

    // dangerouslySetInnerHTML does not execute <script> tags. Recreate them so
    // ad-network snippets that include a script actually run.
    if (!useHtml || !contentRef.current) return
    contentRef.current.querySelectorAll('script').forEach((old) => {
      if (old.dataset.adExecuted) return
      old.dataset.adExecuted = 'true'
      const fresh = document.createElement('script')
      for (const attr of old.attributes) fresh.setAttribute(attr.name, attr.value)
      fresh.textContent = old.textContent
      old.replaceWith(fresh)
    })
  }, [active, adsenseOn, useAdsense, useAutoAnchor, useHtml, html, client, adsenseSlot])

  // No blank box for unfilled slots — hide the whole aside until an ad lands.
  if (!active || (unfilled && !useFallback)) return null

  return (
    <aside
      aria-label={unfilled && useFallback ? 'Promotion' : 'Advertisement'}
      data-ad-slot={slot}
      className={`mx-auto w-full max-w-6xl px-4 sm:px-6 ${className}`}
    >
      <div
        ref={contentRef}
        className="my-4 flex w-full items-center justify-center overflow-hidden rounded-xl"
      >
        {useAdsense ? (
          unfilled && useFallback ? (
            <a
              href={fallback.href || '#'}
              {...(fallbackExternal ? { target: '_blank', rel: 'noopener noreferrer' } : {})}
              className="block w-full"
            >
              <img
                src={fallback.image}
                alt={fallback.alt || 'Advertisement'}
                className="mx-auto max-h-[120px] w-full rounded-lg object-contain"
              />
            </a>
          ) : (
            <ins
              className="adsbygoogle block w-full"
              style={{ display: 'block' }}
              data-ad-client={client}
              data-ad-slot={adsenseSlot}
              data-ad-format="auto"
              data-full-width-responsive="true"
            />
          )
        ) : useAutoAnchor ? (
          // Auto-ads mode: an unslotted anchor gives Google a clean mount point.
          <ins
            className="adsbygoogle block w-full"
            style={{ display: 'block' }}
            data-ad-client={client}
            data-ad-format="auto"
            data-full-width-responsive="true"
          />
        ) : useHtml ? (
          <div className="w-full" dangerouslySetInnerHTML={{ __html: html }} />
        ) : (
          <a href={image.href || '#'} target="_blank" rel="noopener noreferrer" className="block">
            <img
              src={image.image}
              alt={image.alt || 'Advertisement'}
              className="mx-auto max-h-[120px] w-auto rounded-lg object-contain"
            />
          </a>
        )}
      </div>
    </aside>
  )
}
