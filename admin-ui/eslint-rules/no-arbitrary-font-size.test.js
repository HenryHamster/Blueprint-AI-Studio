import { RuleTester } from 'eslint'
import rule from './no-arbitrary-font-size.js'

const tester = new RuleTester({
  languageOptions: {
    ecmaVersion: 2020,
    parserOptions: {
      ecmaFeatures: { jsx: true },
    },
  },
})

tester.run('viewport/no-arbitrary-font-size', rule, {
  valid: [
    // Named Tailwind text scale utilities — no arbitrary pixel value
    { code: '<div className="text-sm" />' },
    { code: '<div className="text-base" />' },
    { code: '<div className="text-lg" />' },
    { code: '<div className="text-xl" />' },
    { code: '<div className="text-xs" />' },
    // Arbitrary non-px units — rule targets px only
    { code: '<div className="text-[1rem]" />' },
    { code: '<div className="text-[1.25rem]" />' },
    { code: '<div className="text-[calc(1rem+2px)]" />' },
    { code: '<div className="text-[var(--font-size)]" />' },
    // Other arbitrary classes that are NOT font-size
    { code: '<div className="max-w-[16px]" />' },
    { code: '<div className="w-[16px]" />' },
    { code: '<div className="h-[14px]" />' },
    { code: '<div className="gap-[14px]" />' },
    // Inline style with non-string numeric value — outside this rule's scope
    { code: '<div style={{ fontSize: 14 }} />' },
    // Non-className attributes — rule only checks className/class
    { code: '<div data-size="text-[14px]" />' },
    { code: '<input placeholder="text-[14px]" />' },
    // Empty className
    { code: '<div className="" />' },
  ],
  invalid: [
    // Literal className — single px font-size
    {
      code: '<div className="text-[14px]" />',
      errors: [{ messageId: 'arbitraryFontSize' }],
    },
    {
      code: '<div className="text-[16px]" />',
      errors: [{ messageId: 'arbitraryFontSize' }],
    },
    {
      code: '<div className="text-[18px]" />',
      errors: [{ messageId: 'arbitraryFontSize' }],
    },
    // The three most common values in the existing codebase
    {
      code: '<div className="text-[10px]" />',
      errors: [{ messageId: 'arbitraryFontSize' }],
    },
    {
      code: '<div className="text-[11px]" />',
      errors: [{ messageId: 'arbitraryFontSize' }],
    },
    {
      code: '<div className="text-[9px]" />',
      errors: [{ messageId: 'arbitraryFontSize' }],
    },
    // px font-size alongside other valid classes
    {
      code: '<div className="flex items-center text-[13px] font-medium" />',
      errors: [{ messageId: 'arbitraryFontSize' }],
    },
    // Multiple arbitrary font-size classes in one attribute — two errors
    {
      code: '<div className="text-[10px] text-[14px]" />',
      errors: [{ messageId: 'arbitraryFontSize' }, { messageId: 'arbitraryFontSize' }],
    },
    // JSXExpressionContainer with string literal
    {
      code: '<div className={"text-[14px]"} />',
      errors: [{ messageId: 'arbitraryFontSize' }],
    },
    // Template literal className
    {
      code: '<div className={`flex text-[14px]`} />',
      errors: [{ messageId: 'arbitraryFontSize' }],
    },
    {
      code: '<div className={`text-[10px] font-bold`} />',
      errors: [{ messageId: 'arbitraryFontSize' }],
    },
    // class attribute (HTML form, also covered)
    {
      code: '<div class="text-[14px]" />',
      errors: [{ messageId: 'arbitraryFontSize' }],
    },
    // Verify reported message contains the class name and px value
    {
      code: '<div className="text-[13px]" />',
      errors: [
        {
          messageId: 'arbitraryFontSize',
          data: { cls: 'text-[13px]', px: '13' },
        },
      ],
    },
    // Variant-prefixed arbitrary font sizes
    {
      code: '<div className="sm:text-[14px]" />',
      errors: [{ messageId: 'arbitraryFontSize' }],
    },
    {
      code: '<div className="hover:text-[12px]" />',
      errors: [{ messageId: 'arbitraryFontSize' }],
    },
    {
      code: '<div className="md:text-[16px]" />',
      errors: [{ messageId: 'arbitraryFontSize' }],
    },
    {
      code: '<div className="dark:text-[10px]" />',
      errors: [{ messageId: 'arbitraryFontSize' }],
    },
    // Multiple variant-prefixed sizes
    {
      code: '<div className="sm:text-[14px] md:text-[16px]" />',
      errors: [{ messageId: 'arbitraryFontSize' }, { messageId: 'arbitraryFontSize' }],
    },
  ],
})
