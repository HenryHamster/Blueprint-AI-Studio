/**
 * ESLint rule: viewport/require-workbench-focus
 *
 * Forbids ad-hoc focus outline/ring Tailwind utilities (focus:outline-none,
 * focus:ring-*, focus:ring-offset-*, focus:ring-{color}-{shade},
 * focus:border-{color}-{shade}, and the focus-visible:/focus-within:
 * equivalents) in favour of the single .workbench-focus component class
 * (src/index.css), which is the one declared focus treatment.
 *
 * Not covered on purpose: focus:/focus-visible: utilities that aren't part
 * of the outline/ring/border recipe (e.g. focus-visible:opacity-100) — those
 * are a different kind of effect, not a competing focus-ring declaration.
 *
 * Covers:
 *   - Literal className: className="focus:outline-none focus:ring-1"
 *   - JSXExpressionContainer string: className={"focus:ring-neutral-600"}
 *   - Template literal: className={`flex focus:outline-none`}
 *
 * Per-line opt-out: // eslint-disable-next-line viewport/require-workbench-focus — reason
 */

const FOCUS_TOKEN_RE =
  /^(focus|focus-visible|focus-within):(outline-none|ring(?:-\d+)?|ring-offset-\d+|ring-[a-z]+-\d+(?:\/\d+)?|border-[a-z]+-\d+(?:\/\d+)?)$/

function checkTailwindString(context, node, str) {
  if (!str.includes('focus:') && !str.includes('focus-visible:') && !str.includes('focus-within:')) return
  const hasWorkbenchFocus = str.split(/\s+/).includes('workbench-focus')
  for (const tok of str.split(/\s+/)) {
    if (!tok || !FOCUS_TOKEN_RE.test(tok)) continue
    context.report({
      node,
      messageId: hasWorkbenchFocus ? 'adHocFocusRedundant' : 'adHocFocus',
      data: { cls: tok },
    })
  }
}

export default {
  meta: {
    type: 'suggestion',
    docs: {
      description:
        'Disallow ad-hoc focus outline/ring Tailwind utilities — use the .workbench-focus component class instead',
    },
    schema: [],
    messages: {
      adHocFocus:
        'Tailwind class "{{cls}}" is an ad-hoc focus declaration — add the "workbench-focus" class instead so focus styling is governed by the .workbench-focus token.',
      adHocFocusRedundant:
        'Tailwind class "{{cls}}" is an ad-hoc focus declaration redundant with the "workbench-focus" class already present — remove it.',
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
