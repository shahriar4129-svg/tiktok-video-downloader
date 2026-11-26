import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'
import { ADS } from './src/config/ads.js'

/**
 * Adds a Content-Security-Policy to the production HTML only.
 * Dev is left untouched because Vite's HMR client needs inline scripts.
 * Directives that only work as real HTTP headers (frame-ancestors,
 * base-uri, form-action) are documented in README.md for the host to send.
 */
function securityHeaders() {
  // AdSense needs inline bootstrap + a handful of Google-owned script origins.
  // Keep them explicit — a blanket `https:` would let ANY host execute JS and
  // would effectively remove XSS protection from the page.
  // ('unsafe-inline' stays only because Google's auto ads inject inline scripts.)
  const AD_SCRIPT_ORIGINS = [
    'https://pagead2.googlesyndication.com',
    'https://www.google.com',
    'https://www.gstatic.com',
    'https://securepubads.g.doubleclick.net',
    'https://googleads.g.doubleclick.net',
    'https://www.googleadservices.com',
    'https://adservice.google.com',
    // Google's ad-quality (sodar) script is injected by adsbygoogle.js at runtime.
    'https://*.adtrafficquality.google',
  ]
  const AD_FRAME_ORIGINS = [
    'https://pagead2.googlesyndication.com',
    'https://tpc.googlesyndication.com',
    'https://googleads.g.doubleclick.net',
    'https://securepubads.g.doubleclick.net',
    'https://www.google.com',
    'https://*.adtrafficquality.google',
  ]
  const adScriptSources = ADS.enabled ? ["'unsafe-inline'", ...AD_SCRIPT_ORIGINS] : []
  const csp = [
    "default-src 'self'",
    `script-src 'self' ${adScriptSources.join(' ')}`.trim(),
    "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com",
    "font-src 'self' https://fonts.gstatic.com data:",
    "img-src 'self' data: blob: https:",
    // Video downloader needs to fetch user-supplied direct URLs and play the blob.
    "connect-src 'self' https: http:",
    "media-src 'self' blob: data:",
    "object-src 'none'",
    "base-uri 'self'",
    "form-action 'self'",
    ...(ADS.enabled ? [`frame-src 'self' ${AD_FRAME_ORIGINS.join(' ')}`] : ["frame-src 'none'"]),
  ].join('; ')

  return {
    name: 'security-headers',
    apply: 'build',
    transformIndexHtml(html) {
      return html.replace(
        '<head>',
        `<head>\n    <meta http-equiv="Content-Security-Policy" content="${csp}" />`,
      )
    },
  }
}

export default defineConfig({
  plugins: [react(), tailwindcss(), securityHeaders()],
  server: {
    proxy: {
      '/api': 'http://127.0.0.1:8000',
    },
  },
  // `vite preview` serves only static files. Forward the API to the local
  // helper so the download flow works in the production-like preview too.
  preview: {
    proxy: {
      '/api': `http://127.0.0.1:${process.env.API_PORT || 8000}`,
    },
  },
})
