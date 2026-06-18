/** Parse viewport regression subkey into component name, assertion type, and width.
 *
 * Handles both legacy (Component@Width) and new (Component:assertion_type@Width) formats.
 * The assertion_type is NOT part of the baseline filename — baselines are stored as
 * Component-Widthpx.png regardless of which assertion tripped.
 */
export function parseViewportSubkey(subkey: string): { component: string; assertionType?: string; width?: string } {
  const colonIdx = subkey.indexOf(':');
  if (colonIdx === -1) {
    // Legacy format: Component@Width or just Component
    const atIdx = subkey.indexOf('@');
    return atIdx === -1 ? { component: subkey } : { component: subkey.slice(0, atIdx), width: subkey.slice(atIdx + 1) };
  }
  // New format: Component:assertion_type@Width or Component:assertion_type
  const component = subkey.slice(0, colonIdx);
  const rest = subkey.slice(colonIdx + 1);
  const atIdx = rest.indexOf('@');
  return atIdx === -1
    ? { component, assertionType: rest }
    : { component, assertionType: rest.slice(0, atIdx), width: rest.slice(atIdx + 1) };
}
