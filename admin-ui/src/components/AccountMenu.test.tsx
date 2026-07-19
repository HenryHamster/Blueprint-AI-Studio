import { afterEach, describe, expect, test, vi } from 'vitest';
import { createRoot, type Root } from 'react-dom/client';
import { act } from 'react';
import { AccountMenu } from './AccountMenu';
import type { StoredAccount } from '../accountManager';

const accounts: StoredAccount[] = [
  { id: 'a', email: 'alice@example.com', displayName: 'Alice', uid: 'uid-a' },
  { id: 'b', email: 'bob@example.com', displayName: 'Bob', uid: 'uid-b' },
];

let container: HTMLDivElement;
let root: Root;

afterEach(() => {
  act(() => root.unmount());
  container.remove();
  vi.clearAllMocks();
});

function render(ui: React.ReactElement) {
  container = document.createElement('div');
  document.body.appendChild(container);
  root = createRoot(container);
  act(() => root.render(ui));
  return container;
}

function baseProps(overrides: Record<string, unknown> = {}) {
  return {
    accounts,
    activeAccountId: 'a',
    currentEmail: 'alice@example.com',
    onAddAccount: vi.fn(),
    onSwitchAccount: vi.fn(),
    onSignOut: vi.fn(),
    onSignOutAll: vi.fn(),
    ...overrides,
  };
}

function click(el: Element | null) {
  act(() => {
    el?.dispatchEvent(new MouseEvent('click', { bubbles: true }));
  });
}

describe('AccountMenu — mobile variant', () => {
  test('lists other accounts to switch to, excluding the active one', () => {
    const el = render(<AccountMenu variant="mobile" {...baseProps()} />);
    expect(el.textContent).toContain('alice@example.com'); // active, shown in header
    // Bob is switchable; Alice must not appear as a switch target row.
    const switchButtons = [...el.querySelectorAll('button')].filter(b => b.textContent?.includes('bob@example.com'));
    expect(switchButtons.length).toBe(1);
    const aliceSwitchRow = [...el.querySelectorAll('button')].filter(
      b => b.textContent === 'alice@example.com',
    );
    expect(aliceSwitchRow.length).toBe(0);
  });

  test('switching an account fires onSwitchAccount with that id and closes via onAfterAction', () => {
    const onSwitchAccount = vi.fn();
    const onAfterAction = vi.fn();
    const el = render(
      <AccountMenu variant="mobile" {...baseProps({ onSwitchAccount, onAfterAction })} />,
    );
    const bobButton = [...el.querySelectorAll('button')].find(b => b.textContent?.includes('bob@example.com'));
    click(bobButton ?? null);
    expect(onSwitchAccount).toHaveBeenCalledWith('b');
    expect(onAfterAction).toHaveBeenCalledTimes(1);
  });

  test('Add account and Sign out fire their callbacks', () => {
    const onAddAccount = vi.fn();
    const onSignOut = vi.fn();
    const el = render(<AccountMenu variant="mobile" {...baseProps({ onAddAccount, onSignOut })} />);
    click([...el.querySelectorAll('button')].find(b => b.textContent === 'Add account') ?? null);
    expect(onAddAccount).toHaveBeenCalledTimes(1);
    click([...el.querySelectorAll('button')].find(b => b.textContent === 'Sign out') ?? null);
    expect(onSignOut).toHaveBeenCalledTimes(1);
  });

  test('Sign out all only appears with more than one account', () => {
    const single = render(
      <AccountMenu variant="mobile" {...baseProps({ accounts: [accounts[0]] })} />,
    );
    expect(single.textContent).not.toContain('Sign out all');
    act(() => root.unmount());
    container.remove();

    const multi = render(<AccountMenu variant="mobile" {...baseProps()} />);
    expect(multi.textContent).toContain('Sign out all accounts');
  });
});

describe('AccountMenu — desktop variant', () => {
  test('dropdown is closed until the trigger is clicked, then reveals accounts', () => {
    const el = render(<AccountMenu variant="desktop" {...baseProps()} />);
    expect(el.querySelector('[role="menu"]')).toBeNull();
    const trigger = el.querySelector('[data-testid="account-menu-desktop"] button');
    click(trigger);
    expect(el.querySelector('[role="menu"]')).not.toBeNull();
    expect(el.textContent).toContain('bob@example.com');
    expect(el.textContent).toContain('Add account');
  });

  test('selecting a switch target fires onSwitchAccount and closes the menu', () => {
    const onSwitchAccount = vi.fn();
    const el = render(<AccountMenu variant="desktop" {...baseProps({ onSwitchAccount })} />);
    click(el.querySelector('[data-testid="account-menu-desktop"] button'));
    const bob = [...el.querySelectorAll('[role="menuitem"]')].find(b => b.textContent?.includes('bob@example.com'));
    click(bob ?? null);
    expect(onSwitchAccount).toHaveBeenCalledWith('b');
    expect(el.querySelector('[role="menu"]')).toBeNull();
  });
});
