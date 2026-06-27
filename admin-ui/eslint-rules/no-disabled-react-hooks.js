export default {
  meta: {
    type: 'problem',
    docs: {
      description: 'Disallow wholesale-disabling of react-hooks/* rules in ESLint config',
    },
    schema: [],
    messages: {
      noDisable:
        'react-hooks/{{rule}} must not be set to "off" — re-enable or fix the violation instead.',
    },
  },
  create(context) {
    function isOff(node) {
      if (node.type === 'Literal') return node.value === 'off' || node.value === 0
      if (node.type === 'ArrayExpression' && node.elements.length > 0) {
        const first = node.elements[0]
        return first?.type === 'Literal' && (first.value === 'off' || first.value === 0)
      }
      return false
    }

    return {
      Property(node) {
        const key = node.key
        let ruleName
        if (key.type === 'Literal' && typeof key.value === 'string') {
          ruleName = key.value
        } else if (key.type === 'Identifier') {
          ruleName = key.name
        }
        if (ruleName?.startsWith('react-hooks/') && isOff(node.value)) {
          context.report({
            node,
            messageId: 'noDisable',
            data: { rule: ruleName.slice('react-hooks/'.length) },
          })
        }
      },
    }
  },
}
