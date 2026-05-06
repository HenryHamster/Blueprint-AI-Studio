import '@testing-library/jest-dom/vitest';

// jsdom does not implement Element.scrollTo — components that auto-scroll
// otherwise throw "el.scrollTo is not a function" inside requestAnimationFrame
// callbacks during tests. Stub to a no-op for the test environment.
if (!Element.prototype.scrollTo) {
  Element.prototype.scrollTo = function () {};
}
