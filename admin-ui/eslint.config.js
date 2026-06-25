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

const viewportPlugin = {
  rules: {
    'no-fixed-width': noFixedWidthOverflow,
    'min-touch-target': minTouchTarget,
    'no-arbitrary-font-size': noArbitraryFontSize,
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
      'viewport/no-fixed-width': 'error',
      'viewport/min-touch-target': 'error',
      'viewport/no-arbitrary-font-size': 'error',
      'size/no-oversized-file': 'error',
      'react-hooks/set-state-in-effect': 'off',
      'react-hooks/refs': 'off',
      'react-hooks/purity': 'off',
      'react-hooks/preserve-manual-memoization': 'off',
      'react-hooks/immutability': 'off',
    },
  },
])
