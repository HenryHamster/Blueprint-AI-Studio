export async function preserveScrollAnchorAfterPrepend(
  element: Pick<HTMLDivElement, 'scrollHeight' | 'scrollTop'>,
  loadOlder: () => void | Promise<void>,
  schedule: (callback: () => void) => void = callback => { window.requestAnimationFrame(callback); },
): Promise<void> {
  const previousHeight = element.scrollHeight;
  const previousTop = element.scrollTop;
  await loadOlder();
  schedule(() => {
    element.scrollTop = previousTop + Math.max(0, element.scrollHeight - previousHeight);
  });
}
