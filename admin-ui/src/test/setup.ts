import '@testing-library/jest-dom/vitest';
import { configure } from '@testing-library/dom';

// The self-hosted CI machine runs multiple isolated shards concurrently. Under
// legitimate CPU pressure, React/Suspense updates can exceed Testing Library's
// 1s default even though the component settles correctly. Keep retries disabled
// and give asynchronous UI assertions a bounded, realistic scheduling window.
configure({ asyncUtilTimeout: 5_000 });

// jsdom does not implement Element.scrollTo — components that auto-scroll
// otherwise throw "el.scrollTo is not a function" inside requestAnimationFrame
// callbacks during tests. Stub to a no-op for the test environment.
if (typeof Element !== 'undefined' && !Element.prototype.scrollTo) {
  Element.prototype.scrollTo = function () {};
}

// happy-dom (unlike jsdom) does not implement the window dialog methods
// confirm/alert/prompt. Several tests `vi.spyOn(window, 'confirm')`, which
// requires the method to already exist as a function before it can be wrapped.
// Define no-op stubs when missing; under jsdom these guards are no-ops since
// jsdom already provides them.
if (typeof window !== 'undefined') {
  if (typeof window.confirm !== 'function') {
    window.confirm = () => false;
  }
  if (typeof window.alert !== 'function') {
    window.alert = () => {};
  }
  if (typeof window.prompt !== 'function') {
    window.prompt = () => null;
  }
}
