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
    client: decodeAdValue('Y2EtcHViLVhYWFhYWFhYWFhYWFhYWFg='),
    auto: true,
    slots: {
      header: '',
      afterHero: '',
      afterResult: decodeAdValue('WU9VUl9BRF9TTE9UX0lE'),
      afterFeatures: '',
      afterPlatforms: '',
      afterHowTo: '',
      afterTool: '',
      beforeFooter: '',
    },
  },

  // Full-screen interstitial shown after a download option is selected.
  vignette: {
    enabled: true,
    slot: '',
    html: '',
    showBadge: true,
    oncePerSession: false,
    closeLabel: 'Close',
  },
}

// Visitor-side scripts cannot change ad settings at runtime.
deepFreeze(ADS)
