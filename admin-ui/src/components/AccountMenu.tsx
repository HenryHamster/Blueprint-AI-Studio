import { useEffect, useRef, useState } from 'react';
import { ChevronDown, LogOut, UserPlus, Users } from 'lucide-react';
import type { StoredAccount } from '../accountManager';

export interface AccountMenuProps {
  /** All signed-in accounts (active one included). */
  accounts: StoredAccount[];
  /** id of the currently active account, or null when none. */
  activeAccountId: string | null;
  /** Live email of the active account (from the current auth user), used for the trigger label. */
  currentEmail: string;
  /** Begin Google sign-in for an additional account. */
  onAddAccount: () => void;
  /** Switch the active account to `id`. */
  onSwitchAccount: (id: string) => void;
  /** Sign out the active account only. */
  onSignOut: () => void;
  /** Sign out every account and return to the login screen. */
  onSignOutAll: () => void;
  /** 'desktop' = compact dropdown; 'mobile' = flat list for the overflow menu. */
  variant?: 'desktop' | 'mobile';
  /** Mobile only: called after any action so the parent overflow menu can close. */
  onAfterAction?: () => void;
}

function accountLabel(account: StoredAccount): string {
  return account.email || account.displayName || account.id;
}

function accountInitial(email: string): string {
  const trimmed = email.trim();
  return trimmed ? trimmed.charAt(0).toUpperCase() : '?';
}

/**
 * Account switcher: current account, "Add account", the list of signed-in
 * accounts to switch between, and sign-out (current) / sign-out-all. Presented
 * as a compact dropdown on desktop and a flat list inside the mobile overflow
 * menu. Purely presentational — all identity/token work happens in
 * accountManager via the callbacks.
 */
export function AccountMenu(props: AccountMenuProps) {
  const {
    accounts,
    activeAccountId,
    currentEmail,
    onAddAccount,
    onSwitchAccount,
    onSignOut,
    onSignOutAll,
    variant = 'desktop',
    onAfterAction,
  } = props;

  const activeAccount = accounts.find((a) => a.id === activeAccountId) ?? null;
  const activeEmail = currentEmail || (activeAccount ? accountLabel(activeAccount) : '');
  const otherAccounts = accounts.filter((a) => a.id !== activeAccountId);
  const hasMultiple = accounts.length > 1;

  function afterAction() {
    onAfterAction?.();
  }

  if (variant === 'mobile') {
    return (
      <div className="border-t border-workbench-border/70 pt-1" data-testid="account-menu-mobile">
        <div className="px-3 py-2 text-ui-sm text-neutral-500">
          Signed in as
          <div className="truncate text-sm text-neutral-200">{activeEmail || '—'}</div>
        </div>
        {otherAccounts.map((account) => (
          <button
            key={account.id}
            onClick={() => { onSwitchAccount(account.id); afterAction(); }}
            className="flex min-h-[44px] w-full items-center gap-3 rounded px-3 py-2.5 text-left text-sm text-neutral-300 transition-colors hover:bg-workbench-hover"
          >
            <span className="grid h-6 w-6 shrink-0 place-items-center rounded-full bg-workbench-layer text-ui-sm font-semibold text-neutral-100">
              {accountInitial(accountLabel(account))}
            </span>
            <span className="min-w-0 flex-1 truncate">{accountLabel(account)}</span>
          </button>
        ))}
        <button
          onClick={() => { onAddAccount(); afterAction(); }}
          className="flex min-h-[44px] w-full items-center gap-3 rounded px-3 py-2.5 text-left text-sm text-neutral-300 transition-colors hover:bg-workbench-hover"
        >
          <UserPlus className="h-4 w-4 shrink-0" />
          <span>Add account</span>
        </button>
        <button
          onClick={() => { onSignOut(); afterAction(); }}
          className="flex min-h-[44px] w-full items-center gap-3 rounded px-3 py-2.5 text-left text-sm text-neutral-400 transition-colors hover:bg-red-950/20 hover:text-red-300"
        >
          <LogOut className="h-4 w-4 shrink-0" />
          <span>Sign out</span>
        </button>
        {hasMultiple && (
          <button
            onClick={() => { onSignOutAll(); afterAction(); }}
            className="flex min-h-[44px] w-full items-center gap-3 rounded px-3 py-2.5 text-left text-sm text-neutral-500 transition-colors hover:bg-red-950/20 hover:text-red-300"
          >
            <LogOut className="h-4 w-4 shrink-0" />
            <span>Sign out all accounts</span>
          </button>
        )}
      </div>
    );
  }

  return <DesktopAccountMenu {...props} activeEmail={activeEmail} otherAccounts={otherAccounts} hasMultiple={hasMultiple} />;
}

interface DesktopProps extends AccountMenuProps {
  activeEmail: string;
  otherAccounts: StoredAccount[];
  hasMultiple: boolean;
}

function DesktopAccountMenu(props: DesktopProps) {
  const {
    activeEmail,
    otherAccounts,
    hasMultiple,
    onAddAccount,
    onSwitchAccount,
    onSignOut,
    onSignOutAll,
  } = props;
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!open) return;
    function handleClickOutside(e: MouseEvent) {
      if (ref.current && !ref.current.contains(e.target as Node)) setOpen(false);
    }
    document.addEventListener('mousedown', handleClickOutside);
    return () => document.removeEventListener('mousedown', handleClickOutside);
  }, [open]);

  function runAndClose(action: () => void) {
    setOpen(false);
    action();
  }

  return (
    <div className="relative hidden md:block" ref={ref} data-testid="account-menu-desktop">
      {/* eslint-disable-next-line viewport/min-touch-target -- desktop-only dropdown (hidden md:block); never visible at 375px mobile width */}
      <button
        onClick={() => setOpen((prev) => !prev)}
        title={activeEmail ? `Account: ${activeEmail}` : 'Account'}
        aria-haspopup="menu"
        aria-expanded={open}
        className="flex h-7 items-center gap-1 rounded border border-workbench-border/80 bg-workbench-control px-1.5 text-neutral-400 transition-colors hover:bg-workbench-layer hover:text-neutral-100"
      >
        <span className="grid h-4 w-4 shrink-0 place-items-center rounded-full bg-workbench-layer text-ui-sm font-semibold text-neutral-100">
          {accountInitial(activeEmail)}
        </span>
        <ChevronDown className="h-3 w-3 shrink-0" />
      </button>
      {open && (
        <div
          role="menu"
          className="absolute right-0 top-full z-50 mt-2 w-64 overflow-hidden rounded-md border border-workbench-border bg-workbench-panel py-1 shadow-2xl"
        >
          <div className="flex items-center gap-2 px-3 py-2">
            <Users className="h-3.5 w-3.5 shrink-0 text-neutral-500" />
            <div className="min-w-0">
              <div className="text-ui-sm text-neutral-500">Signed in as</div>
              <div className="truncate text-sm text-neutral-200" title={activeEmail}>{activeEmail || '—'}</div>
            </div>
          </div>
          <div className="my-1 border-t border-workbench-border/70" />
          {otherAccounts.length > 0 && (
            <div className="px-1">
              {otherAccounts.map((account) => (
                // eslint-disable-next-line viewport/min-touch-target -- desktop-only dropdown (hidden md:block); never visible at 375px mobile width
                <button
                  key={account.id}
                  role="menuitem"
                  onClick={() => runAndClose(() => onSwitchAccount(account.id))}
                  className="flex w-full items-center gap-2 rounded px-2 py-1.5 text-left text-sm text-neutral-300 transition-colors hover:bg-workbench-hover"
                >
                  <span className="grid h-5 w-5 shrink-0 place-items-center rounded-full bg-workbench-layer text-ui-sm font-semibold text-neutral-100">
                    {accountInitial(accountLabel(account))}
                  </span>
                  <span className="min-w-0 flex-1 truncate">{accountLabel(account)}</span>
                </button>
              ))}
              <div className="my-1 border-t border-workbench-border/70" />
            </div>
          )}
          <div className="px-1">
            {/* eslint-disable-next-line viewport/min-touch-target -- desktop-only dropdown (hidden md:block); never visible at 375px mobile width */}
            <button
              role="menuitem"
              onClick={() => runAndClose(onAddAccount)}
              className="flex w-full items-center gap-2 rounded px-2 py-1.5 text-left text-sm text-neutral-300 transition-colors hover:bg-workbench-hover"
            >
              <UserPlus className="h-4 w-4 shrink-0" />
              <span>Add account</span>
            </button>
            {/* eslint-disable-next-line viewport/min-touch-target -- desktop-only dropdown (hidden md:block); never visible at 375px mobile width */}
            <button
              role="menuitem"
              onClick={() => runAndClose(onSignOut)}
              className="flex w-full items-center gap-2 rounded px-2 py-1.5 text-left text-sm text-neutral-400 transition-colors hover:bg-red-950/20 hover:text-red-300"
            >
              <LogOut className="h-4 w-4 shrink-0" />
              <span>Sign out</span>
            </button>
            {hasMultiple && (
              // eslint-disable-next-line viewport/min-touch-target -- desktop-only dropdown (hidden md:block); never visible at 375px mobile width
              <button
                role="menuitem"
                onClick={() => runAndClose(onSignOutAll)}
                className="flex w-full items-center gap-2 rounded px-2 py-1.5 text-left text-sm text-neutral-500 transition-colors hover:bg-red-950/20 hover:text-red-300"
              >
                <LogOut className="h-4 w-4 shrink-0" />
                <span>Sign out all accounts</span>
              </button>
            )}
          </div>
        </div>
      )}
    </div>
  );
}
