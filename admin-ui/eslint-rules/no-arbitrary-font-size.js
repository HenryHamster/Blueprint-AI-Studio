/**
 * ESLint rule: viewport/no-arbitrary-font-size
 *
 * Flags arbitrary pixel font-size Tailwind classes (text-[Npx]) in JSX
 * className attributes. The codebase has 1976 existing occurrences that
 * are being migrated to @theme design tokens — this rule prevents new ones
 * from being added while the migration is in flight.
 *
 * Covers:
 *   - Literal className: className="text-[14px]"
 *   - JSXExpressionContainer string: className={"text-[14px]"}
 *   - Template literal: className={`flex text-[14px]`}
 *
 * Intentionally excluded:
 *   - Non-px units: text-[1rem], text-[calc(...)], text-[var(...)]
 *   - Non-text arbitrary classes: max-w-[16px], w-[16px]
 *
 * Per-line opt-out: // eslint-disable-next-line viewport/no-arbitrary-font-size — reason
 */

const ARBITRARY_FONT_SIZE_RE = /(?:^|\s)(text-\[(\d+)px\])/g

function checkTailwindString(context, node, str) {
  ARBITRARY_FONT_SIZE_RE.lastIndex = 0
  let m
  while ((m = ARBITRARY_FONT_SIZE_RE.exec(str)) !== null) {
    const cls = m[1].trim()
    const px = parseInt(m[2], 10)
    context.report({ node, messageId: 'arbitraryFontSize', data: { cls, px: String(px) } })
  }
}

export default {
  meta: {
    type: 'suggestion',
    docs: {
      description:
        'Disallow arbitrary pixel font-size Tailwind classes (text-[Npx]) — use @theme design tokens instead',
    },
    schema: [],
    messages: {
      arbitraryFontSize:
        'Tailwind class "{{cls}}" uses an arbitrary {{px}}px font size — use a @theme design token (e.g. text-label-sm) instead. Add eslint-disable-next-line if migration is deferred.',
    },
  },
  create(context) {
    return {
      JSXAttribute(node) {
        const attrName = node.name.name
        if (attrName !== 'className' && attrName !== 'class') return

        const val = node.value
        if (val?.type === 'Literal' && typeof val.value === 'string') {
          checkTailwindString(context, val, val.value)
        } else if (val?.type === 'JSXExpressionContainer') {
          const expr = val.expression
          if (expr.type === 'Literal' && typeof expr.value === 'string') {
            checkTailwindString(context, expr, expr.value)
          } else if (expr.type === 'TemplateLiteral') {
            for (const quasi of expr.quasis) {
              const str = quasi.value.cooked ?? quasi.value.raw
              checkTailwindString(context, quasi, str)
            }
          }
        }
      },
    }
  },
}
