/**
 * ESLint rule: viewport/canonical-radius
 *
 * Forbids Tailwind border-radius utilities outside the declared canonical
 * set: rounded, rounded-md, rounded-lg, rounded-full (bare or with a
 * directional prefix, e.g. rounded-r-md, rounded-tl-lg). Eliminated
 * variants (rounded-sm, rounded-xl, rounded-2xl, and a bare directional
 * rounded-r/-l/-t/-b with no size suffix) collapse onto the nearest
 * canonical tier.
 *
 * Covers:
 *   - Literal className: className="rounded-xl"
 *   - JSXExpressionContainer string: className={"rounded-xl"}
 *   - Template literal: className={`flex rounded-xl`}
 *
 * Per-line opt-out: // eslint-disable-next-line viewport/canonical-radius — reason
 */

const CANONICAL_SIZES = new Set(['md', 'lg', 'full'])
const RADIUS_RE = /(?<![\w-])(rounded(?:-[trbl][trbl]?)?)(?:-([a-z0-9]+))?(?![\w-])/g

const SUGGEST = {
  sm: 'rounded',
  xl: 'rounded-lg',
  '2xl': 'rounded-lg',
}

function checkTailwindString(context, node, str) {
  RADIUS_RE.lastIndex = 0
  let m
  while ((m = RADIUS_RE.exec(str)) !== null) {
    const [full, base, size] = m
    const isBareRounded = base === 'rounded' && size == null
    const isCanonicalSize = size != null && CANONICAL_SIZES.has(size)
    if (isBareRounded || isCanonicalSize) continue
    const suggestion = size && SUGGEST[size] ? SUGGEST[size] : 'rounded / rounded-md / rounded-lg / rounded-full'
    context.report({
      node,
      messageId: 'nonCanonicalRadius',
      data: { cls: full, suggestion },
    })
  }
}

export default {
  meta: {
    type: 'suggestion',
    docs: {
      description:
        'Disallow non-canonical Tailwind radius utilities — use rounded / rounded-md / rounded-lg / rounded-full',
    },
    schema: [],
    messages: {
      nonCanonicalRadius:
        'Tailwind class "{{cls}}" is not part of the canonical radius set — use "{{suggestion}}" instead.',
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
