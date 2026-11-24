import { ArrowUpRight, Smartphone } from 'lucide-react'
import { ADS } from '../config/ads.js'

function detectPlatform() {
  if (typeof navigator === 'undefined') return 'desktop'
  const ua = navigator.userAgent || ''
  const isIos =
    /iPad|iPhone|iPod/.test(ua) ||
    (navigator.platform === 'MacIntel' && navigator.maxTouchPoints > 1)
  if (isIos) return 'ios'
  if (/Android/i.test(ua)) return 'android'
  return 'desktop'
}

/**
 * Compact reference-site-style promo chip for the header.
 *
 * All destinations are configured in src/config/ads.js. With no store URLs set,
 * it safely points to the on-site guides instead of pretending an app exists.
 */
export default function HeaderPromo() {
  const config = ADS.promo || {}
  if (!ADS.enabled || !config.enabled) return null

  const platform = detectPlatform()
  const href =
    (platform === 'ios' && config.iosHref) ||
    (platform === 'android' && config.androidHref) ||
    config.href ||
    '#'
  const external = /^https?:\/\//i.test(href)
  const label = config.label || 'Download Guide'

  return (
    <a
      href={href}
      {...(external ? { target: '_blank', rel: 'noopener noreferrer' } : {})}
      aria-label={config.alt || label}
      className="ss-gradient inline-flex min-h-[38px] items-center gap-1.5 rounded-lg px-2.5 py-1.5 text-[11px] font-bold text-white shadow-sm transition hover:brightness-110 active:scale-[0.98] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-teal-500 sm:px-3 sm:text-xs"
    >
      <Smartphone className="h-3.5 w-3.5" aria-hidden="true" />
      <span className="hidden sm:inline">{label}</span>
      <ArrowUpRight className="h-3.5 w-3.5 opacity-80" aria-hidden="true" />
    </a>
  )
}
