import { RuleTester } from 'eslint'
import rule from './no-disabled-react-hooks.js'
import { readFileSync } from 'fs'
import { fileURLToPath } from 'url'
import { dirname, join } from 'path'

const tester = new RuleTester({
  languageOptions: { ecmaVersion: 2020 },
})

const __filename = fileURLToPath(import.meta.url)
const __dirname = dirname(__filename)

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

describe('react-hooks compiler rules explicitly locked at error', () => {
  const requiredRules = [
    'react-hooks/rules-of-hooks',
    'react-hooks/set-state-in-effect',
    'react-hooks/refs',
    'react-hooks/purity',
    'react-hooks/preserve-manual-memoization',
    'react-hooks/immutability',
  ]

  it('verifies all compiler rules are explicitly set to error in eslint config', () => {
    const configPath = join(__dirname, '..', 'eslint.config.js')
    const configContent = readFileSync(configPath, 'utf8')

    for (const ruleName of requiredRules) {
      const rulePattern = new RegExp(`['"]${ruleName}['"]\\s*:\\s*['"]error['"]`)
      if (!rulePattern.test(configContent)) {
        throw new Error(
          `React-hooks compiler rule "${ruleName}" must be explicitly set to 'error' in eslint.config.js`
        )
      }
    }
  })
})
