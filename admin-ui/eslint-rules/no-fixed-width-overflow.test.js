import { RuleTester } from 'eslint'
import rule from './no-fixed-width-overflow.js'

const tester = new RuleTester({
  languageOptions: {
    ecmaVersion: 2020,
    parserOptions: {
      ecmaFeatures: { jsx: true },
    },
  },
})

tester.run('viewport/no-fixed-width', rule, {
  valid: [
    // Under threshold
    { code: '<div className="w-[374px]" />' },
    { code: '<div className="min-w-[300px]" />' },
    // max-w is intentionally excluded (it constrains, doesn't force overflow)
    { code: '<div className="max-w-[600px]" />' },
    { code: '<div className="max-w-[900px]" />' },
    // Non-px units
    { code: '<div className="w-full" />' },
    { code: '<div style={{ width: "50%" }} />' },
    { code: '<div style={{ width: "2rem" }} />' },
    // max-w suffix check: w-[NNNpx] inside max-w-[NNNpx] must NOT be flagged
    { code: '<div className="max-w-[900px] flex" />' },
    // Inline style under threshold
    { code: '<div style={{ width: "300px" }} />' },
    { code: '<div style={{ minWidth: "374px" }} />' },
    // Numeric style values (outside scope of this rule)
    { code: '<div style={{ width: 900 }} />' },
  ],
  invalid: [
    {
      code: '<div className="w-[900px]" />',
      errors: [{ messageId: 'tailwindWidth' }],
    },
    {
      code: '<div className="w-[375px]" />',
      errors: [{ messageId: 'tailwindWidth' }],
    },
    {
      code: '<div className="min-w-[400px]" />',
      errors: [{ messageId: 'tailwindWidth' }],
    },
    {
      code: '<div className="flex min-w-[400px] overflow-auto" />',
      errors: [{ messageId: 'tailwindWidth' }],
    },
    // Template literal className
    {
      code: '<div className={`flex w-[900px]`} />',
      errors: [{ messageId: 'tailwindWidth' }],
    },
    // Inline style string width
    {
      code: '<div style={{ width: "400px" }} />',
      errors: [{ messageId: 'inlineWidth' }],
    },
    {
      code: '<div style={{ minWidth: "375px" }} />',
      errors: [{ messageId: 'inlineWidth' }],
    },
  ],
})
