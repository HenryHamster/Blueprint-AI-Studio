import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import './index.css'
import App from './App.tsx'
import { ErrorBoundary } from './components/ErrorBoundary'

const rootEl = document.getElementById('root')
if (!rootEl) throw new Error('root element not found in document')
createRoot(rootEl).render(
  <StrictMode>
    <ErrorBoundary level="app">
      <App />
    </ErrorBoundary>
  </StrictMode>,
)
