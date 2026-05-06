/**
 * ESLint rule: viewport/no-fixed-width
 *
 * Flags hardcoded fixed widths >= 375px in JSX that can force horizontal
 * overflow on mobile viewports. Covers:
 *   - Tailwind arbitrary classes w-[NNNpx] and min-w-[NNNpx] (not max-w)
 *   - Inline styles width: 'NNNpx' and minWidth: 'NNNpx'
 *
 * max-w-[NNNpx] is intentionally excluded — it constrains maximum width
 * and cannot cause overflow.
 *
 * Per-line opt-out: // eslint-disable-next-line viewport/no-fixed-width — reason
 */

// Matches `w-[NNNpx]` or `min-w-[NNNpx]` preceded by start-of-string or whitespace
// (avoids matching the `w-[...]` suffix inside `max-w-[...]`)
const MIN_W_RE = /(?:^|\s)(min-w-\[(\d+)px\])/g
const W_RE = /(?:^|\s)(w-\[(\d+)px\])/g
const THRESHOLD = 375

function checkTailwindString(context, node, str) {
  MIN_W_RE.lastIndex = 0
  W_RE.lastIndex = 0

  let m
  while ((m = MIN_W_RE.exec(str)) !== null) {
    const px = parseInt(m[2], 10)
    if (px >= THRESHOLD) {
      context.report({ node, messageId: 'tailwindWidth', data: { cls: m[1].trim(), px: String(px) } })
    }
  }
  while ((m = W_RE.exec(str)) !== null) {
    const px = parseInt(m[2], 10)
    if (px >= THRESHOLD) {
      context.report({ node, messageId: 'tailwindWidth', data: { cls: m[1].trim(), px: String(px) } })
    }
  }
}

function checkInlineStyleProp(context, prop) {
  const key = prop.key.name ?? prop.key.value
  if (key !== 'width' && key !== 'minWidth') return
  const val = prop.value
  if (val.type !== 'Literal' || typeof val.value !== 'string') return
  const m = val.value.match(/^(\d+)px$/)
  if (!m) return
  const px = parseInt(m[1], 10)
  if (px >= THRESHOLD) {
    context.report({
      node: val,
      messageId: 'inlineWidth',
      data: { prop: key, value: val.value, px: String(px) },
    })
  }
}

export default {
  meta: {
    type: 'problem',
    docs: {
      description: 'Disallow hardcoded fixed widths >= 375px in JSX (mobile overflow risk)',
    },
    schema: [],
    messages: {
      tailwindWidth:
        'Tailwind class "{{cls}}" ({{px}}px) >= 375px — may cause horizontal overflow on mobile. Add eslint-disable-next-line if intentional (drawer, carousel, etc.).',
      inlineWidth:
        'Inline style {{prop}}: "{{value}}" ({{px}}px) >= 375px — may cause horizontal overflow on mobile. Add eslint-disable-next-line if intentional.',
    },
  },
  create(context) {
    return {
      JSXAttribute(node) {
        const attrName = node.name.name

        if (attrName === 'className' || attrName === 'class') {
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
        }

        if (attrName === 'style') {
          const val = node.value
          if (val?.type === 'JSXExpressionContainer' && val.expression.type === 'ObjectExpression') {
            for (const prop of val.expression.properties) {
              if (prop.type === 'Property') checkInlineStyleProp(context, prop)
            }
          }
        }
      },
    }
  },
}
