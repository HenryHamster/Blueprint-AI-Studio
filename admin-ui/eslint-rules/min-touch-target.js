/**
 * ESLint rule: viewport/min-touch-target
 *
 * Flags interactive elements (<button>, <a>, onClick handlers) with touch targets
 * below the iOS 44px minimum touch target. Detects elements with:
 *   - Small padding classes only (p-0, p-0.5, p-1, p-1.5, p-2)
 *   - No explicit min-h-[44px], min-h-11, h-11, h-12, h-14, h-16, h-20, or larger
 *
 * Per-line opt-out: // eslint-disable-next-line viewport/min-touch-target — reason
 */

const SMALL_PADDING_RE = /(?:^|\s)(p(?:[lrxy])?-(?:0|0\.5|1|1\.5|2))(?![\w-])/
const EXPLICIT_HEIGHT_RE = /(?:^|\s)(?:min-h-(?:\[44px\]|11|[0-9]{2,}|full|screen)|h-(?:11|12|14|16|20|[0-9]{2,}|full|screen))(?![\w-])/

function getClassNameString(node) {
  const classNameAttr = node.attributes?.find(
    (attr) => attr.type === 'JSXAttribute' && (attr.name.name === 'className' || attr.name.name === 'class')
  )

  if (!classNameAttr || !classNameAttr.value) return null

  const val = classNameAttr.value

  if (val.type === 'Literal' && typeof val.value === 'string') {
    return val.value
  } else if (val.type === 'JSXExpressionContainer') {
    const expr = val.expression
    if (expr.type === 'Literal' && typeof expr.value === 'string') {
      return expr.value
    } else if (expr.type === 'TemplateLiteral') {
      let result = ''
      for (const quasi of expr.quasis) {
        result += quasi.value.cooked ?? quasi.value.raw
      }
      return result
    }
  }

  return null
}

function hasSmallPaddingOnly(classNameStr) {
  if (!classNameStr) return false

  const classes = classNameStr.split(/\s+/)
  let hasPadding = false

  for (const cls of classes) {
    // Check if this is a padding class
    if (cls.match(/^p(?:[lrxy])?-/)) {
      hasPadding = true
      // If it's a large padding class, return false
      if (!cls.match(/^p(?:[lrxy])?-(?:0|0\.5|1|1\.5|2)$/)) {
        return false
      }
    }
  }

  return hasPadding
}

function hasExplicitHeight(classNameStr) {
  if (!classNameStr) return false
  return EXPLICIT_HEIGHT_RE.test(classNameStr)
}

function hasViewportAllowAttr(node) {
  return node.attributes?.some(
    (attr) => attr.type === 'JSXAttribute' && attr.name.name === 'data-viewport-allow-small-target'
  )
}

function checkInteractiveElement(context, node) {
  const classNameStr = getClassNameString(node)

  // Skip if no className
  if (!classNameStr) return

  // Skip if has explicit large height
  if (hasExplicitHeight(classNameStr)) return

  // Skip if explicitly opted out of touch-target requirement (dense operator UI)
  if (hasViewportAllowAttr(node)) return

  // Flag if has only small padding
  if (hasSmallPaddingOnly(classNameStr)) {
    context.report({
      node,
      messageId: 'minTouchTarget',
      data: { element: node.name.name || 'element' },
    })
  }
}

export default {
  meta: {
    type: 'problem',
    docs: {
      description: 'Disallow interactive elements with touch targets below 44px (iOS guideline)',
    },
    schema: [],
    messages: {
      minTouchTarget:
        '<{{element}}> with small padding classes may have a touch target below 44px. Add min-h-[44px] or h-11+, or add eslint-disable-next-line if the element is inside a dense toolbar.',
    },
  },
  create(context) {
    return {
      JSXOpeningElement(node) {
        // Check for button or anchor elements
        if (node.name.name === 'button' || node.name.name === 'a') {
          checkInteractiveElement(context, node)
          return
        }

        // Check for onClick handlers
        const hasOnClick = node.attributes?.some(
          (attr) => attr.type === 'JSXAttribute' && attr.name.name === 'onClick'
        )

        if (hasOnClick) {
          checkInteractiveElement(context, node)
        }
      },
    }
  },
}
