/* eslint-disable react-refresh/only-export-components */
import { StrictMode, useEffect } from 'react'
import { createRoot } from 'react-dom/client'
import './index.css'
import './appTools'
import App from './App.tsx'
import { ErrorBoundary } from './components/ErrorBoundary'
import { getIdToken } from './firebase'

const _loadStart = performance.now()

function LoadTimingReporter() {
  useEffect(() => {
    const ms = Math.round(performance.now() - _loadStart)
    const surface = (window.location.hash.replace(/^#\/?/, '').split('/')[0] || 'chat').slice(0, 64)
    getIdToken().then(token => {
      if (!token) return
      fetch('/api/admin/ui-load-timing', {
        method: 'POST',
        headers: { 'Authorization': `Bearer ${token}`, 'Content-Type': 'application/json' },
        body: JSON.stringify({ surface, page_ready_ms: ms, sample_type: 'first_commit' }),
      }).catch(() => {})
    }).catch(() => {})
  }, [])
  return null
}

const rootEl = document.getElementById('root')
if (!rootEl) throw new Error('root element not found in document')
createRoot(rootEl).render(
  <StrictMode>
    <ErrorBoundary level="app">
      <LoadTimingReporter />
      <App />
    </ErrorBoundary>
  </StrictMode>,
)
