import { RuleTester } from 'eslint'
import rule from './min-touch-target.js'

const viewportPlugin = {
  rules: { 'min-touch-target': rule },
}

const tester = new RuleTester({
  languageOptions: {
    ecmaVersion: 2020,
    parserOptions: {
      ecmaFeatures: { jsx: true },
    },
  },
  plugins: { viewport: viewportPlugin },
})

tester.run('viewport/min-touch-target', rule, {
  valid: [
    // Button with no className
    { code: '<button>Click me</button>' },
    // Button with large padding
    { code: '<button className="p-3">Click me</button>' },
    { code: '<button className="p-4">Click me</button>' },
    { code: '<button className="px-4">Click me</button>' },
    // Button with explicit height >= 44px
    { code: '<button className="p-1 h-11">Click me</button>' },
    { code: '<button className="p-1 min-h-[44px]">Click me</button>' },
    { code: '<button className="p-0 h-12">Click me</button>' },
    { code: '<button className="p-1 h-14">Click me</button>' },
    // Anchor tag with large padding
    { code: '<a href="#" className="p-3">Link</a>' },
    // Element with onClick and large padding
    { code: '<div onClick={() => {}} className="p-4">Interactive</div>' },
    // No padding specified
    { code: '<button className="flex items-center">Click me</button>' },
    // Non-interactive element with small padding
    { code: '<div className="p-1">Not interactive</div>' },
    // Mixed padding classes (at least one large)
    { code: '<button className="p-1 pr-4">Click me</button>' },
    // data-viewport-allow-small-target opt-out for dense operator UI
    { code: '<button data-viewport-allow-small-target className="p-0.5">Dense toolbar button</button>' },
    { code: '<button data-viewport-allow-small-target className="p-1 flex items-center">Icon</button>' },
  ],
  invalid: [
    {
      code: '<button className="p-1">Click me</button>',
      errors: [{ messageId: 'minTouchTarget' }],
    },
    {
      code: '<button className="p-0">Click me</button>',
      errors: [{ messageId: 'minTouchTarget' }],
    },
    {
      code: '<button className="p-0.5">Click me</button>',
      errors: [{ messageId: 'minTouchTarget' }],
    },
    {
      code: '<button className="p-1.5">Click me</button>',
      errors: [{ messageId: 'minTouchTarget' }],
    },
    {
      code: '<button className="p-2">Click me</button>',
      errors: [{ messageId: 'minTouchTarget' }],
    },
    {
      code: '<button className="px-1">Click me</button>',
      errors: [{ messageId: 'minTouchTarget' }],
    },
    {
      code: '<button className="py-1">Click me</button>',
      errors: [{ messageId: 'minTouchTarget' }],
    },
    {
      code: '<button className="pl-1">Click me</button>',
      errors: [{ messageId: 'minTouchTarget' }],
    },
    {
      code: '<button className="pr-1">Click me</button>',
      errors: [{ messageId: 'minTouchTarget' }],
    },
    {
      code: '<button className="p-1 flex items-center">Icon button</button>',
      errors: [{ messageId: 'minTouchTarget' }],
    },
    {
      code: '<a href="/path" className="p-1">Link</a>',
      errors: [{ messageId: 'minTouchTarget' }],
    },
    {
      code: '<div onClick={() => {}} className="p-1">Interactive</div>',
      errors: [{ messageId: 'minTouchTarget' }],
    },
    // Template literal className
    {
      code: '<button className={`p-1`}>Click me</button>',
      errors: [{ messageId: 'minTouchTarget' }],
    },
    // Multiple classes with small padding
    {
      code: '<button className="inline-flex items-center justify-center p-0.5">Icon</button>',
      errors: [{ messageId: 'minTouchTarget' }],
    },
  ],
})
