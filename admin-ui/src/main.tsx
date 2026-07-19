/* eslint-disable react-refresh/only-export-components */
import { StrictMode, useEffect } from 'react'
import { createRoot } from 'react-dom/client'
import './index.css'
import './appTools'
import App from './App.tsx'
import { ErrorBoundary } from './components/ErrorBoundary'
import { currentUiSurface, reportUiLoadTiming } from './uiLoadTiming'

const _loadStart = performance.now()

function LoadTimingReporter() {
  useEffect(() => {
    const ms = Math.round(performance.now() - _loadStart)
    reportUiLoadTiming(currentUiSurface(), ms, 'first_commit')
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
