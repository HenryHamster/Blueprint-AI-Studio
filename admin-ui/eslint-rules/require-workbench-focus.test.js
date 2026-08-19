import { RuleTester } from 'eslint'
import rule from './require-workbench-focus.js'

const tester = new RuleTester({
  languageOptions: {
    ecmaVersion: 2020,
    parserOptions: {
      ecmaFeatures: { jsx: true },
    },
  },
})

tester.run('viewport/require-workbench-focus', rule, {
  valid: [
    // The governed replacement
    { code: '<button className="workbench-focus" />' },
    { code: '<input className="rounded-lg px-2 workbench-focus" />' },
    // focus utilities that are not part of the outline/ring/border recipe
    { code: '<div className="focus-visible:opacity-100" />' },
    { code: '<div className="focus:bg-workbench-hover" />' },
    { code: '<div className="" />' },
  ],
  invalid: [
    {
      code: '<button className="focus:outline-none" />',
      errors: [{ messageId: 'adHocFocus', data: { cls: 'focus:outline-none' } }],
    },
    {
      code: '<button className="focus:ring-1 focus:ring-neutral-600" />',
      errors: [
        { messageId: 'adHocFocus', data: { cls: 'focus:ring-1' } },
        { messageId: 'adHocFocus', data: { cls: 'focus:ring-neutral-600' } },
      ],
    },
    {
      code: '<button className="focus:border-neutral-500" />',
      errors: [{ messageId: 'adHocFocus', data: { cls: 'focus:border-neutral-500' } }],
    },
    {
      code: '<button className="focus:ring-offset-2" />',
      errors: [{ messageId: 'adHocFocus', data: { cls: 'focus:ring-offset-2' } }],
    },
    {
      code: '<button className="focus-visible:ring-2 focus-visible:ring-cyan-500/60" />',
      errors: [
        { messageId: 'adHocFocus', data: { cls: 'focus-visible:ring-2' } },
        { messageId: 'adHocFocus', data: { cls: 'focus-visible:ring-cyan-500/60' } },
      ],
    },
    {
      code: '<button className="focus-within:ring-neutral-600" />',
      errors: [{ messageId: 'adHocFocus', data: { cls: 'focus-within:ring-neutral-600' } }],
    },
    // ad-hoc utility redundant alongside an already-present workbench-focus class
    {
      code: '<button className="workbench-focus focus:outline-none" />',
      errors: [{ messageId: 'adHocFocusRedundant', data: { cls: 'focus:outline-none' } }],
    },
    // JSXExpressionContainer with string literal
    {
      code: '<button className={"focus:outline-none"} />',
      errors: [{ messageId: 'adHocFocus' }],
    },
    // Template literal className
    {
      code: '<button className={`flex focus:ring-1`} />',
      errors: [{ messageId: 'adHocFocus' }],
    },
    // class attribute (HTML form, also covered)
    {
      code: '<button class="focus:outline-none" />',
      errors: [{ messageId: 'adHocFocus' }],
    },
  ],
})
