export interface ClaimBoundWorkMonitorOptions<T> {
  work: Promise<T>;
  renew: () => Promise<boolean>;
  renewEveryMs: number;
  externalFence?: Promise<unknown>;
  shouldFenceExternal?: () => boolean;
  onFence?: () => void;
}

export interface ClaimBoundWorkMonitorResult<T> {
  fenced: boolean;
  result: PromiseSettledResult<T>;
}

type ClaimBoundWorkEvent<T> =
  | { kind: 'settled'; result: PromiseSettledResult<T> }
  | { kind: 'renew' }
  | { kind: 'external' };

/** Keep exact claimed work owned and caught until it settles after the first fence. */
export async function monitorClaimBoundWork<T>(
  options: ClaimBoundWorkMonitorOptions<T>,
): Promise<ClaimBoundWorkMonitorResult<T>> {
  const settled = options.work.then(
    value => ({ status: 'fulfilled' as const, value }),
    (reason: unknown) => ({ status: 'rejected' as const, reason }),
  );
  const settledEvent = settled.then(result => ({ kind: 'settled' as const, result }));
  const externalEvent = options.externalFence?.then(() => ({ kind: 'external' as const }));
  let externalPending = externalEvent !== undefined;
  let fenced = false;
  let fenceNotified = false;
  let nextRenewAt = Date.now() + options.renewEveryMs;
  for (;;) {
    let renewTimer: ReturnType<typeof setTimeout> | undefined;
    const renewEvent = new Promise<{ kind: 'renew' }>(resolve => {
      renewTimer = setTimeout(
        () => { resolve({ kind: 'renew' }); },
        Math.max(0, nextRenewAt - Date.now()),
      );
    });
    const events: Array<Promise<ClaimBoundWorkEvent<T>>> = [
      settledEvent,
      renewEvent,
    ];
    if (externalPending && externalEvent !== undefined) events.push(externalEvent);
    const event = await Promise.race(events);
    if (renewTimer !== undefined) clearTimeout(renewTimer);
    if (event.kind === 'settled') return { fenced, result: event.result };
    if (event.kind === 'external') {
      externalPending = false;
      if (options.shouldFenceExternal?.() ?? true) fenced = true;
    } else {
      try {
        if (!await options.renew()) fenced = true;
      } catch {
        fenced = true;
      }
      nextRenewAt = Date.now() + options.renewEveryMs;
    }
    if (fenced && !fenceNotified) {
      fenceNotified = true;
      try { options.onFence?.(); } catch { /* settlement remains mandatory */ }
    }
  }
}
