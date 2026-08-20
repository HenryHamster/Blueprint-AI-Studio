import js from '@eslint/js'
import tseslint from 'typescript-eslint'

export default tseslint.config(
  js.configs.recommended,
  {
    files: ['src/**/*.ts'],
    extends: [...tseslint.configs.strictTypeChecked],
    languageOptions: {
      parserOptions: {
        project: './tsconfig.json',
        tsconfigRootDir: import.meta.dirname,
      },
    },
    rules: {
      '@typescript-eslint/no-explicit-any': 'error',
      '@typescript-eslint/no-unused-vars': ['error', { argsIgnorePattern: '^_' }],
      'no-console': 'off',  // agent uses console.log for structured logging
      '@typescript-eslint/restrict-template-expressions': ['error', { allowNumber: true }],
    },
  },
  {
    // Vitest asymmetric matchers (expect.any(), expect.objectContaining(), etc.)
    // legitimately return `any` — passing them to typed parameters in
    // expectCalledWith() would otherwise trigger no-unsafe-argument.
    files: ['src/**/*.test.ts'],
    rules: {
      '@typescript-eslint/no-unsafe-argument': 'off',
    },
  },
  {
    ignores: ['node_modules/', 'dist/', 'eslint.config.js', 'sr-pr-section.js', 'screenshots-pr-section.js', 'guard-suppressions-pr-section.js'],
  }
)
