import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import './index.css'
import './i18n' // Initialize i18n
import App from './App.tsx'

// Keep the app in portrait; the lock is only honored in installed/fullscreen
// contexts (Android PWA) — browsers reject it silently elsewhere.
const lockPortrait = () => {
  const orientation = screen.orientation as ScreenOrientation & {
    lock?: (o: string) => Promise<void>
  }
  orientation?.lock?.('portrait').catch(() => {})
}
lockPortrait()
document.addEventListener('visibilitychange', () => {
  if (!document.hidden) lockPortrait()
})

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <App />
  </StrictMode>,
)
