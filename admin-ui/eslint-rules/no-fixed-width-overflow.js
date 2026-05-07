/**
 * ESLint rule: viewport/no-fixed-width
 *
 * Flags hardcoded fixed widths >= 375px in JSX that can force horizontal
 * overflow on mobile viewports. Covers:
 *   - Tailwind arbitrary classes w-[NNNpx] and min-w-[NNNpx] (not max-w)
 *   - Standard Tailwind width utilities like w-96 and w-screen
 *   - Arbitrary spacing classes that can force minimum width
 *   - Inline styles width/minWidth and optional spacing literals
 *
 * max-w-[NNNpx] is intentionally excluded — it constrains maximum width
 * and cannot cause overflow.
 *
 * Per-line opt-out: // eslint-disable-next-line viewport/no-fixed-width — reason
 */

// Matches fixed width / spacing classes preceded by start-of-string or whitespace.
const MIN_W_RE = /(?:^|\s)(min-w-\[(\d+)px\])/g
const W_BRACKET_RE = /(?:^|\s)(w-\[(\d+)px\])/g
const WIDTH_SCALE_RE = /(?:^|\s)(w-(\d+))(?![\w-])/g
const SCREEN_RE = /(?:^|\s)(w-screen)(?![\w-])/g
const SPACING_RE = /(?:^|\s)((?:p[lrxy]?|m[lrxy]?|gap(?:-x)?)-\[(\d+)px\])/g
const THRESHOLD = 375

// Per-pattern px conversion. Bracket forms (`w-[400px]`, `min-w-[400px]`,
// `px-[400px]`) capture the literal pixel value in m[2]. The bare scale
// form (`w-96`) captures the Tailwind scale unit, which is 4px per unit.
// `w-screen` is treated as exactly THRESHOLD so it always trips the gate.
function pxFor(re, m) {
  if (re === SCREEN_RE) return THRESHOLD
  if (re === WIDTH_SCALE_RE) return parseInt(m[2], 10) * 4
  return parseInt(m[2], 10)
}

function checkTailwindString(context, node, str) {
  for (const re of [MIN_W_RE, W_BRACKET_RE, WIDTH_SCALE_RE, SCREEN_RE, SPACING_RE]) {
    re.lastIndex = 0
    let m
    while ((m = re.exec(str)) !== null) {
      const cls = m[1].trim()
      const px = pxFor(re, m)
      if (px >= THRESHOLD) context.report({ node, messageId: 'tailwindWidth', data: { cls, px: String(px) } })
    }
  }
}

function checkInlineStyleProp(context, prop) {
  const key = prop.key.name ?? prop.key.value
  if (!['width', 'minWidth', 'padding', 'paddingLeft', 'paddingRight', 'marginLeft', 'marginRight', 'gap'].includes(key)) return
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
