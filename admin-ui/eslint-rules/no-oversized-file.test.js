import { RuleTester } from 'eslint'
import { join, dirname } from 'path'
import { fileURLToPath } from 'url'
import rule from './no-oversized-file.js'

const __dirname = dirname(fileURLToPath(import.meta.url))
// Simulate file paths under admin-ui/src (mirrors rule's SRC_ROOT resolution)
const srcRoot = join(__dirname, '..', 'src')

const tester = new RuleTester({ languageOptions: { ecmaVersion: 2020 } })

// Generate N unique valid JS statements for line-count testing.
function makeCode(n) {
  return Array.from({ length: n }, (_, i) => `const v${i} = ${i};`).join('\n')
}

// ESLint 9 RuleTester keeps internal state of test case names and throws on
// duplicates during retries. Clear the internal cache before running tests.
// Access the private _testCases property if it exists to clear registered names.
if (tester._testCases) {
  tester._testCases.clear?.()
}

tester.run('size/no-oversized-file', rule, {
  valid: [
    // Within cap: 10 lines, cap 50
    {
      code: makeCode(10),
      filename: join(srcRoot, 'components', 'SmallFile.tsx'),
      options: [{ limits: { 'components/SmallFile.tsx': 50 } }],
    },
    // Exactly at cap: 50 lines, cap 50
    {
      code: makeCode(50),
      filename: join(srcRoot, 'components', 'AtCap.tsx'),
      options: [{ limits: { 'components/AtCap.tsx': 50 } }],
    },
    // No cap entry — uncapped files are not flagged by this rule
    {
      code: makeCode(5000),
      filename: join(srcRoot, 'components', 'Uncapped.tsx'),
      options: [{ limits: {} }],
    },
    // Filename is '<input>' — skip (e.g. piped or virtual source)
    {
      code: 'const x = 1;',
      filename: '<input>',
      options: [{ limits: { '<input>': 0 } }],
    },
  ],
  invalid: [
    // Over cap by 1: 51 lines, cap 50
    {
      code: makeCode(51),
      filename: join(srcRoot, 'components', 'TooBig.tsx'),
      options: [{ limits: { 'components/TooBig.tsx': 50 } }],
      errors: [{ messageId: 'oversized' }],
    },
  ],
})
