import { RuleTester } from 'eslint'
import rule from './no-surface-colour.js'

const tester = new RuleTester({
  languageOptions: {
    ecmaVersion: 2020,
    parserOptions: {
      ecmaFeatures: { jsx: true },
    },
  },
})

tester.run('viewport/no-surface-colour', rule, {
  valid: [
    // Workbench tokens — the governed replacement
    { code: '<div className="bg-workbench-panel" />' },
    { code: '<div className="hover:bg-workbench-hover" />' },
    { code: '<div className="bg-workbench-base/50" />' },
    // Non-background neutral utilities are out of scope for this rule
    { code: '<div className="text-neutral-400 border-neutral-700" />' },
    // Arbitrary hex that does not duplicate a declared workbench token
    { code: '<div className="bg-[#1a1a2e]" />' },
    // Other-family background colours are unaffected
    { code: '<div className="bg-red-500 bg-emerald-400/20" />' },
    { code: '<div className="" />' },
  ],
  invalid: [
    {
      code: '<div className="bg-neutral-800" />',
      errors: [{ messageId: 'noNeutralBackground', data: { cls: 'bg-neutral-800', suggestion: 'bg-workbench-panel' } }],
    },
    {
      code: '<div className="bg-neutral-950/60" />',
      errors: [
        { messageId: 'noNeutralBackground', data: { cls: 'bg-neutral-950/60', suggestion: 'bg-workbench-shell' } },
      ],
    },
    // hover-prefixed always maps to the single hover token, regardless of shade
    {
      code: '<div className="hover:bg-neutral-700" />',
      errors: [
        { messageId: 'noNeutralBackground', data: { cls: 'hover:bg-neutral-700', suggestion: 'hover:bg-workbench-hover' } },
      ],
    },
    // other variant prefixes keep the shade mapping
    {
      code: '<div className="dark:bg-neutral-900" />',
      errors: [
        { messageId: 'noNeutralBackground', data: { cls: 'dark:bg-neutral-900', suggestion: 'dark:bg-workbench-base' } },
      ],
    },
    // hand-copied hex duplicate of a declared workbench token
    {
      code: '<div className="bg-[#10151c]" />',
      errors: [{ messageId: 'noHexDuplicate', data: { cls: 'bg-[#10151c]', suggestion: 'bg-workbench-layer' } }],
    },
    {
      code: '<div className="bg-[#0d1218]" />',
      errors: [{ messageId: 'noHexDuplicate', data: { cls: 'bg-[#0d1218]', suggestion: 'bg-workbench-card' } }],
    },
    // JSXExpressionContainer with string literal
    {
      code: '<div className={"bg-neutral-800"} />',
      errors: [{ messageId: 'noNeutralBackground' }],
    },
    // Template literal className
    {
      code: '<div className={`flex bg-neutral-900`} />',
      errors: [{ messageId: 'noNeutralBackground' }],
    },
    // Multiple violations in one attribute
    {
      code: '<div className="bg-neutral-800 hover:bg-neutral-700" />',
      errors: [{ messageId: 'noNeutralBackground' }, { messageId: 'noNeutralBackground' }],
    },
    // class attribute (HTML form, also covered)
    {
      code: '<div class="bg-neutral-800" />',
      errors: [{ messageId: 'noNeutralBackground' }],
    },
  ],
})
