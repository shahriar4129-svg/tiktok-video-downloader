import { useEffect, useRef } from 'react'
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
 * Supports AdSense auto ads, manual responsive AdSense units, a pasted HTML
 * snippet, or a simple image banner.
 */
export default function BannerAd({ slot, className = '' }) {
  const html = typeof ADS.html?.[slot] === 'string' ? ADS.html[slot].trim() : ''
  const image = ADS.slots?.[slot]
  const adsense = ADS.adsense
  const adsenseSlot =
    typeof adsense?.slots?.[slot] === 'string' ? adsense.slots[slot].trim() : ''

  const adsenseOn = Boolean(ADS.enabled && adsense?.enabled && adsense?.client)
  const autoAds = Boolean(adsenseOn && adsense?.auto)
  const useAdsense = Boolean(adsenseOn && adsenseSlot)
  const useHtml = Boolean(ADS.enabled && !adsenseOn && html)
  const useImage = Boolean(
    ADS.enabled && !adsenseOn && !useHtml && image?.enabled && image?.image,
  )
  const active = autoAds || useAdsense || useHtml || useImage
  const contentRef = useRef(null)
  const pushedRef = useRef(false)

  useEffect(() => {
    if (!active) return

    if (adsenseOn) {
      loadAdsense(adsense.client)
      // Manual units need one explicit push. React StrictMode runs effects
      // twice in dev — guard so Google never sees a duplicate push.
      if (useAdsense && !pushedRef.current) {
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
  }, [active, adsenseOn, useAdsense, useHtml, html, adsense?.client])

  // Auto ads are injected by Google anywhere on the page — no wrapper needed.
  if (!active || (autoAds && !useAdsense && !useHtml && !useImage)) return null

  return (
    <aside
      aria-label="Advertisement"
      data-ad-slot={slot}
      className={`mx-auto w-full max-w-6xl px-4 sm:px-6 ${className}`}
    >
      <div
        ref={contentRef}
        className="my-4 flex min-h-[40px] items-center justify-center overflow-hidden rounded-xl"
      >
        {useAdsense ? (
          <ins
            className="adsbygoogle block w-full"
            style={{ display: 'block' }}
            data-ad-client={adsense.client}
            data-ad-slot={adsenseSlot}
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
