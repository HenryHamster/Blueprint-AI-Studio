import js from '@eslint/js'
import globals from 'globals'
import reactHooks from 'eslint-plugin-react-hooks'
import reactRefresh from 'eslint-plugin-react-refresh'
import tseslint from 'typescript-eslint'
import { defineConfig, globalIgnores } from 'eslint/config'
import noFixedWidthOverflow from './eslint-rules/no-fixed-width-overflow.js'
import minTouchTarget from './eslint-rules/min-touch-target.js'
import noOversizedFile from './eslint-rules/no-oversized-file.js'
import noArbitraryFontSize from './eslint-rules/no-arbitrary-font-size.js'
import noDisabledReactHooks from './eslint-rules/no-disabled-react-hooks.js'
import noSurfaceColour from './eslint-rules/no-surface-colour.js'
import requireWorkbenchFocus from './eslint-rules/require-workbench-focus.js'
import canonicalRadius from './eslint-rules/canonical-radius.js'

const hooksGuardPlugin = { rules: { 'no-disabled-react-hooks': noDisabledReactHooks } }
const viewportPlugin = {
  rules: {
    'no-fixed-width': noFixedWidthOverflow,
    'min-touch-target': minTouchTarget,
    'no-arbitrary-font-size': noArbitraryFontSize,
    'no-surface-colour': noSurfaceColour,
    'require-workbench-focus': requireWorkbenchFocus,
    'canonical-radius': canonicalRadius,
  },
}
const sizePlugin = { rules: { 'no-oversized-file': noOversizedFile } }

export default defineConfig([
  globalIgnores(['dist']),
  {
    files: ['**/*.{ts,tsx}'],
    extends: [
      js.configs.recommended,
      tseslint.configs.strict,
      reactHooks.configs.flat.recommended,
      reactRefresh.configs.vite,
    ],
    plugins: { viewport: viewportPlugin, size: sizePlugin },
    languageOptions: {
      ecmaVersion: 2020,
      globals: globals.browser,
    },
    rules: {
      '@typescript-eslint/no-non-null-assertion': 'warn',
      'no-console': ['warn', { allow: ['warn', 'error'] }],
      'react-hooks/rules-of-hooks': 'error',
      'react-hooks/exhaustive-deps': 'warn',
      'react-hooks/set-state-in-effect': 'error',
      'react-hooks/refs': 'error',
      'react-hooks/purity': 'error',
      'react-hooks/preserve-manual-memoization': 'error',
      'react-hooks/immutability': 'error',
      'viewport/no-fixed-width': 'error',
      'viewport/min-touch-target': 'error',
      'viewport/no-arbitrary-font-size': 'error',
      'viewport/no-surface-colour': 'error',
      'viewport/require-workbench-focus': 'error',
      'viewport/canonical-radius': 'error',
      'size/no-oversized-file': 'error',
    },
  },
  {
    files: ['eslint.config.js'],
    plugins: { hooksGuard: hooksGuardPlugin },
    rules: { 'hooksGuard/no-disabled-react-hooks': 'error' },
  },
])
