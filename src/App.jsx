import { useEffect, useRef, useState } from 'react'
import { Analytics } from '@vercel/analytics/react'
import { Check, ShieldCheck, Video, X } from 'lucide-react'
import VideoDownloader from './components/VideoDownloader.jsx'
import BannerAd from './components/BannerAd.jsx'

export default function App() {
  const [videoHasResult, setVideoHasResult] = useState(false)
  const [toolKey, setToolKey] = useState(0)
  const [toast, setToast] = useState(null)
  const toastTimer = useRef(null)

  useEffect(() => () => clearTimeout(toastTimer.current), [])

  function showToast(message, type = 'success') {
    clearTimeout(toastTimer.current)
    setToast({ message, type, id: Date.now() })
    toastTimer.current = setTimeout(() => setToast(null), 1600)
  }

  // Logo/brand is the home button: clear any result, reset the tool and jump
  // back to the top — same as a refresh, without a slow full page reload.
  function goHome() {
    setVideoHasResult(false)
    setToast(null)
    setToolKey((value) => value + 1)
    window.scrollTo({ top: 0, behavior: 'instant' })
  }

  return (
    <div className="relative min-h-screen bg-[#f1f1f1]">
      {/* Flat header */}
      <header className="border-b border-slate-200 bg-white">
        <div className="mx-auto flex h-16 max-w-6xl items-center justify-between gap-4 px-4 sm:px-6">
          <button
            type="button"
            onClick={goHome}
            title="Back to home"
            aria-label="TikTok Video Downloader — back to home"
            className="-ml-1 flex items-center gap-2.5 rounded-lg p-1 text-left transition hover:opacity-80 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-teal-500"
          >
            <span className="grid h-9 w-9 place-items-center rounded-lg ss-gradient shadow-sm">
              <Video className="h-5 w-5 text-white" strokeWidth={2.2} aria-hidden="true" />
            </span>
            <div>
              <h1 className="text-base font-extrabold tracking-tight text-slate-900 sm:text-lg">TikTok Video Downloader</h1>
              <p className="text-[10px] font-medium text-slate-500 sm:text-[11px]">Download TikTok videos in HD.</p>
            </div>
          </button>
          <nav className="hidden items-center gap-5 text-[13px] font-semibold text-slate-600 md:flex" aria-label="Main">
            <a href="#tool" className="transition hover:text-teal-600">TikTok Downloader</a>
            <a href="#faq" className="transition hover:text-teal-600">FAQ</a>
          </nav>
          <span className="inline-flex items-center gap-1.5 rounded-md bg-teal-50 px-2.5 py-1.5 text-[11px] font-semibold text-teal-700">
            <ShieldCheck className="h-3.5 w-3.5" aria-hidden="true" />
            Free &amp; private
          </span>
        </div>
      </header>

      <BannerAd slot="header" />

      <main id="tool" className="mx-auto max-w-6xl px-4 pb-16 sm:px-6">
        {!videoHasResult && (
          <div className="mb-6 max-w-2xl">
            <h2 className="text-3xl font-extrabold leading-tight tracking-tight text-slate-900 sm:text-4xl">
              Download TikTok videos in{' '}
              <span className="bg-gradient-to-r from-[#0d9488] to-[#14b8a6] bg-clip-text text-transparent">
                HD
              </span>{' '}
              without the fuss
            </h2>
            <p className="mt-3 text-sm leading-relaxed text-slate-500 sm:text-base">
              Paste a link — save as MP4, MP3 or Story. No login needed.
            </p>
          </div>
        )}

        <BannerAd slot="afterHero" />

        <div className="animate-panel-in motion-reduce:animate-none">
          <VideoDownloader key={toolKey} showToast={showToast} onResultStateChange={setVideoHasResult} />
        </div>

        <BannerAd slot="afterTool" />
      </main>

      <BannerAd slot="beforeFooter" />

      <footer className="bg-[#0f3b38] py-8 text-center text-xs text-white/80">
        <p className="font-semibold">© 2026 TikTok Video Downloader. All rights reserved.</p>
        <p className="mt-2 text-[11px] text-white/50">Not affiliated with TikTok or ByteDance.</p>
      </footer>

      {toast && (
        <div
          key={toast.id}
          role="status"
          aria-live="polite"
          className={`animate-toast-in fixed bottom-6 left-1/2 z-50 flex -translate-x-1/2 items-center gap-2 rounded-lg border px-4 py-2.5 text-xs font-semibold shadow-xl ${
            toast.type === 'error'
              ? 'border-rose-200 bg-white text-rose-600'
              : 'border-emerald-200 bg-white text-emerald-700'
          }`}
        >
          {toast.type === 'error' ? (
            <X className="h-3.5 w-3.5" aria-hidden="true" />
          ) : (
            <Check className="h-3.5 w-3.5" aria-hidden="true" />
          )}
          {toast.message}
        </div>
      )}

      <Analytics />
    </div>
  )
}
