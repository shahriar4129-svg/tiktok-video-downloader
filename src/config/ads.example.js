/**
 * Ad configuration template.
 *
 * Copy this file to src/config/ads.js and replace the encoded placeholder
 * values with base64-encoded versions of your own AdSense client ID and
 * slot ID. Encoding keeps the literal IDs out of the built JavaScript and
 * view-source; it is not a secret store — AdSense IDs are public by design.
 *
 *   cp src/config/ads.example.js src/config/ads.js
 *
 * src/config/ads.js is gitignored so ad credentials stay local.
 */

function decodeAdValue(encoded) {
  if (!encoded) return ''
  try {
    return atob(encoded)
  } catch {
    return ''
  }
}

const ADS_CLIENT = decodeAdValue('Y2EtcHViLVhYWFhYWFhYWFhYWFhYWFg=')
const ADS_SLOT = decodeAdValue('WU9VUl9BRF9TTE9UX0lE')

function deepFreeze(value) {
  if (value && typeof value === 'object' && !Object.isFrozen(value)) {
    Object.freeze(value)
    Object.values(value).forEach(deepFreeze)
  }
  return value
}

export const ADS = {
  // Overall switch for showing ads on the site.
  enabled: true,

  // Optional banners. Each entry accepts: { enabled, image, href, alt }.
  slots: {
    header: { enabled: false, image: '', href: '', alt: 'Advertisement' },
    afterHero: { enabled: false, image: '', href: '', alt: 'Advertisement' },
    afterResult: { enabled: false, image: '', href: '', alt: 'Advertisement' },
    afterFeatures: { enabled: false, image: '', href: '', alt: 'Advertisement' },
    afterPlatforms: { enabled: false, image: '', href: '', alt: 'Advertisement' },
    afterHowTo: { enabled: false, image: '', href: '', alt: 'Advertisement' },
    afterTool: { enabled: false, image: '', href: '', alt: 'Advertisement' },
    beforeFooter: { enabled: false, image: '', href: '', alt: 'Advertisement' },
  },

  // Optional HTML snippets for custom banners.
  html: {
    header: '',
    afterHero: '',
    afterResult: '',
    afterFeatures: '',
    afterPlatforms: '',
    afterHowTo: '',
    afterTool: '',
    beforeFooter: '',
  },

  // Google AdSense — the only active ad network.
  adsense: {
    enabled: true,
    client: ADS_CLIENT,
    auto: true,
    // Shared responsive slot used by every banner position that has no own ID.
    // This keeps all ssstik-style placements live with a single publisher ID.
    fallbackSlot: ADS_SLOT,
    slots: {
      header: '',
      afterHero: '',
      afterResult: ADS_SLOT,
      afterFeatures: '',
      afterPlatforms: '',
      afterHowTo: '',
      afterTool: '',
      beforeFooter: '',
    },
  },

  // Header chip linking to the Android APK / iPhone Home Screen guide.
  promo: {
    enabled: true,
    label: 'Get App',
    href: '/guides/install-app/',
    androidHref: '',
    iosHref: '',
    alt: 'Open the TikTok Video Downloader HD app install guide',
  },

  // Full-screen interstitial shown after a download option is selected.
  vignette: {
    enabled: true,
    slot: '',
    html: '',
    showBadge: true,
    oncePerSession: false,
    // Minimum time the Close button stays hidden (ms). Fixed 4s; the file is
    // warmed in the background from the link paste, so Close is not gated on it.
    minMs: 4000,
    closeLabel: 'Close',
  },
}

// Visitor-side scripts cannot change ad settings at runtime.
deepFreeze(ADS)
