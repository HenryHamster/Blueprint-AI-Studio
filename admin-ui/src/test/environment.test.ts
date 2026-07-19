describe('admin UI unit-test environment', () => {
  test('keeps iframe navigation out of the Happy DOM unit suite', () => {
    const happyDOM = (
      window as typeof window & {
        happyDOM?: {
          settings?: {
            disableIframePageLoading?: boolean;
          };
        };
      }
    ).happyDOM;

    expect(happyDOM?.settings?.disableIframePageLoading).toBe(true);
  });
});
