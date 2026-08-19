import { RuleTester } from 'eslint'
import rule from './canonical-radius.js'

const tester = new RuleTester({
  languageOptions: {
    ecmaVersion: 2020,
    parserOptions: {
      ecmaFeatures: { jsx: true },
    },
  },
})

tester.run('viewport/canonical-radius', rule, {
  valid: [
    // The canonical set
    { code: '<div className="rounded" />' },
    { code: '<div className="rounded-md" />' },
    { code: '<div className="rounded-lg" />' },
    { code: '<div className="rounded-full" />' },
    // Directional variants with a canonical size
    { code: '<div className="rounded-r-md" />' },
    { code: '<div className="rounded-tl-lg" />' },
    // Non-radius classes containing "rounded" as a substring elsewhere are irrelevant here
    { code: '<div className="flex items-center rounded-full px-2" />' },
    { code: '<div className="" />' },
  ],
  invalid: [
    {
      code: '<div className="rounded-sm" />',
      errors: [{ messageId: 'nonCanonicalRadius', data: { cls: 'rounded-sm', suggestion: 'rounded' } }],
    },
    {
      code: '<div className="rounded-xl" />',
      errors: [{ messageId: 'nonCanonicalRadius', data: { cls: 'rounded-xl', suggestion: 'rounded-lg' } }],
    },
    {
      code: '<div className="rounded-2xl" />',
      errors: [{ messageId: 'nonCanonicalRadius', data: { cls: 'rounded-2xl', suggestion: 'rounded-lg' } }],
    },
    // bare directional radius with no canonical size suffix
    {
      code: '<div className="rounded-r" />',
      errors: [{ messageId: 'nonCanonicalRadius' }],
    },
    // JSXExpressionContainer with string literal
    {
      code: '<div className={"rounded-xl"} />',
      errors: [{ messageId: 'nonCanonicalRadius' }],
    },
    // Template literal className
    {
      code: '<div className={`flex rounded-2xl`} />',
      errors: [{ messageId: 'nonCanonicalRadius' }],
    },
    // Multiple violations in one attribute
    {
      code: '<div className="rounded-sm rounded-xl" />',
      errors: [{ messageId: 'nonCanonicalRadius' }, { messageId: 'nonCanonicalRadius' }],
    },
    // class attribute (HTML form, also covered)
    {
      code: '<div class="rounded-xl" />',
      errors: [{ messageId: 'nonCanonicalRadius' }],
    },
  ],
})
