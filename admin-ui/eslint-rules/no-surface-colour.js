/**
 * ESLint rule: viewport/no-surface-colour
 *
 * Forbids raw Tailwind neutral-palette background utilities (bg-neutral-NNN,
 * with or without a hover:/focus:/dark:/etc. variant prefix and an optional
 * /opacity modifier) and hand-copied hex literals that duplicate a declared
 * --color-workbench-* token (src/index.css). Surfaces must go through the
 * workbench-* background tokens so a palette change is a one-edit change.
 *
 * A bare `bg-neutral-N` maps by shade to the workbench token at the matching
 * depth; a `hover:bg-neutral-N` always maps to `hover:bg-workbench-hover`
 * regardless of shade, matching the single declared hover-surface token.
 *
 * Only hex literals that exactly match a known --color-workbench-* value are
 * flagged — arbitrary hex that doesn't duplicate a token is a distinct
 * colour and out of scope for this rule (design judgement, not enforcement).
 *
 * Covers:
 *   - Literal className: className="bg-neutral-800"
 *   - JSXExpressionContainer string: className={"bg-neutral-800"}
 *   - Template literal: className={`flex bg-neutral-800`}
 *
 * Per-line opt-out: // eslint-disable-next-line viewport/no-surface-colour — reason
 */

const SHADE_MAP = {
  '200': 'workbench-hover',
  '300': 'workbench-hover',
  '400': 'workbench-control',
  '500': 'workbench-control',
  '600': 'workbench-card',
  '700': 'workbench-layer',
  '800': 'workbench-panel',
  '900': 'workbench-base',
  '950': 'workbench-shell',
}

const HEX_MAP = {
  '10151c': 'workbench-layer',
  '0d1218': 'workbench-card',
  '151c25': 'workbench-hover',
  '080d13': 'workbench-rail',
  '05080d': 'workbench-control',
}

const BG_NEUTRAL_RE = /(?<![\w-])((?:[a-zA-Z][\w-]*:)*)bg-neutral-(\d+)((?:\/\d+)?)(?![\w-])/g
const BG_HEX_RE = /(?<![\w-])((?:[a-zA-Z][\w-]*:)*)bg-\[#([0-9a-fA-F]{6})\](?![\w-])/g

function suggestForNeutral(prefix, shade) {
  if (prefix === 'hover:') return 'hover:bg-workbench-hover'
  const token = SHADE_MAP[shade]
  return token ? `${prefix}bg-${token}` : null
}

function checkTailwindString(context, node, str) {
  BG_NEUTRAL_RE.lastIndex = 0
  let m
  while ((m = BG_NEUTRAL_RE.exec(str)) !== null) {
    const suggestion = suggestForNeutral(m[1], m[2])
    context.report({
      node,
      messageId: 'noNeutralBackground',
      data: { cls: m[0], suggestion: suggestion || '(no workbench token for this shade)' },
    })
  }

  BG_HEX_RE.lastIndex = 0
  while ((m = BG_HEX_RE.exec(str)) !== null) {
    const token = HEX_MAP[m[2].toLowerCase()]
    if (!token) continue
    context.report({
      node,
      messageId: 'noHexDuplicate',
      data: { cls: m[0], suggestion: `${m[1]}bg-${token}` },
    })
  }
}

export default {
  meta: {
    type: 'suggestion',
    docs: {
      description:
        'Disallow raw bg-neutral-* utilities and hand-copied token hex literals — use --color-workbench-* tokens instead',
    },
    schema: [],
    messages: {
      noNeutralBackground:
        'Tailwind class "{{cls}}" uses the raw neutral palette — use "{{suggestion}}" instead so surface colour is governed by --color-workbench-* tokens.',
      noHexDuplicate:
        'Tailwind class "{{cls}}" hand-copies a --color-workbench-* hex value — use "{{suggestion}}" instead so the token stays the single source of truth.',
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
