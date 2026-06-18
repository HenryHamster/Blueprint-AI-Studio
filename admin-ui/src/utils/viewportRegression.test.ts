import { describe, it, expect } from 'vitest';
import { parseViewportSubkey } from './viewportRegression';

describe('parseViewportSubkey', () => {
  it('parses legacy format: Component@Width', () => {
    const result = parseViewportSubkey('MessagesPanel@375');
    expect(result).toEqual({
      component: 'MessagesPanel',
      width: '375',
    });
  });

  it('parses legacy format: Component only', () => {
    const result = parseViewportSubkey('MessagesPanel');
    expect(result).toEqual({
      component: 'MessagesPanel',
    });
  });

  it('parses new format: Component:assertion_type@Width', () => {
    const result = parseViewportSubkey('MessagesPanel:overflow@375');
    expect(result).toEqual({
      component: 'MessagesPanel',
      assertionType: 'overflow',
      width: '375',
    });
  });

  it('parses new format: Component:assertion_type (no width)', () => {
    const result = parseViewportSubkey('MessagesPanel:overflow');
    expect(result).toEqual({
      component: 'MessagesPanel',
      assertionType: 'overflow',
    });
  });

  it('handles multi-word component names', () => {
    const result = parseViewportSubkey('NewTaskModal:touch_target@768');
    expect(result).toEqual({
      component: 'NewTaskModal',
      assertionType: 'touch_target',
      width: '768',
    });
  });
});
