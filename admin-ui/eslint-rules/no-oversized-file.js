/**
 * ESLint rule: size/no-oversized-file
 *
 * Fires when a source file exceeds its declared LOC cap from
 * backend/testing/module_size_limits.json (frontend section).
 *
 * The cap is looked up by the file's path relative to admin-ui/src.
 * Pass { limits: { 'components/Foo.tsx': 1500 } } as a rule option to
 * override the loaded JSON (used in tests).
 *
 * Per-file opt-out: // eslint-disable-next-line size/no-oversized-file — reason
 */

import { createRequire } from 'module'
import { fileURLToPath } from 'url'
import { dirname, join, relative } from 'path'

const __filename = fileURLToPath(import.meta.url)
const __dirname = dirname(__filename)
const _require = createRequire(import.meta.url)

// The admin-ui/src root is one level up from eslint-rules/.
const SRC_ROOT = join(__dirname, '..', 'src')

// Loaded once at module init; avoids per-file I/O at lint time.
let _defaultLimits = null
function _getDefaultLimits() {
  if (_defaultLimits === null) {
    try {
      const data = _require('../../backend/testing/module_size_limits.json')
      _defaultLimits = (data && data.frontend) || {}
    } catch {
      _defaultLimits = {}
    }
  }
  return _defaultLimits
}

export default {
  meta: {
    type: 'problem',
    docs: {
      description: 'Prevent source files from exceeding their declared LOC cap in module_size_limits.json',
    },
    schema: [
      {
        type: 'object',
        properties: { limits: { type: 'object' } },
        additionalProperties: false,
      },
    ],
    messages: {
      oversized:
        '{{rel}} has {{count}} lines (cap: {{cap}}). Run `python3 backend/testing/check_size_limits.py --fix --sections frontend` from the repo root to bump the cap if the growth is justified.',
    },
  },
  create(context) {
    const limits = context.options[0]?.limits ?? _getDefaultLimits()
    return {
      Program(node) {
        const filename = context.filename ?? context.getFilename?.()
        if (!filename || filename === '<input>') return
        const rel = relative(SRC_ROOT, filename).replace(/\\/g, '/')
        const cap = limits[rel]
        if (cap === undefined) return
        const src = context.sourceCode ?? context.getSourceCode?.()
        if (!src) return
        // lines.length counts one extra empty entry for trailing newlines; subtract
        // it to match the backend's len(f.readlines()) counting.
        const rawLines = src.lines.length
        const trailingEmpty = src.lines[rawLines - 1] === '' ? 1 : 0
        const lineCount = rawLines - trailingEmpty
        if (lineCount > cap) {
          context.report({
            node,
            messageId: 'oversized',
            data: { rel, count: String(lineCount), cap: String(cap) },
          })
        }
      },
    }
  },
}
