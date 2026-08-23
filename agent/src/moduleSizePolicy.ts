/** Shared agent-side module-size comparison contract.
 *
 * The backend counterpart lives in app.size_limit_policy.is_breach. Keep the
 * null and strict-greater-than semantics identical across the two runtimes.
 */
export function isModuleSizeBreach(
  count: number | null | undefined,
  cap: number | null | undefined,
): boolean {
  return count != null && cap != null && count > cap;
}
