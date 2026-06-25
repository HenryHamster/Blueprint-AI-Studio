import { RuleTester } from 'eslint'
import rule from './no-disabled-react-hooks.js'

const tester = new RuleTester({
  languageOptions: { ecmaVersion: 2020 },
})

tester.run('hooksGuard/no-disabled-react-hooks', rule, {
  valid: [
    // Non-react-hooks rule set to 'off' is fine
    { code: "const rules = { 'no-console': 'off' }" },
    // react-hooks rule set to 'error'
    { code: "const rules = { 'react-hooks/exhaustive-deps': 'error' }" },
    // react-hooks rule set to 'warn'
    { code: "const rules = { 'react-hooks/exhaustive-deps': 'warn' }" },
    // react-hooks rule severity 1 (warn)
    { code: "const rules = { 'react-hooks/exhaustive-deps': 1 }" },
    // react-hooks rule severity 2 (error)
    { code: "const rules = { 'react-hooks/exhaustive-deps': 2 }" },
  ],
  invalid: [
    {
      code: "const rules = { 'react-hooks/exhaustive-deps': 'off' }",
      errors: [{ messageId: 'noDisable' }],
    },
    {
      code: "const rules = { 'react-hooks/exhaustive-deps': 0 }",
      errors: [{ messageId: 'noDisable' }],
    },
    {
      code: "const rules = { 'react-hooks/purity': ['off'] }",
      errors: [{ messageId: 'noDisable' }],
    },
    {
      code: "const rules = { 'react-hooks/purity': ['off', 'warn'] }",
      errors: [{ messageId: 'noDisable' }],
    },
    {
      code: "const rules = { 'react-hooks/refs': 'off', 'react-hooks/immutability': 'off' }",
      errors: [{ messageId: 'noDisable' }, { messageId: 'noDisable' }],
    },
  ],
})
