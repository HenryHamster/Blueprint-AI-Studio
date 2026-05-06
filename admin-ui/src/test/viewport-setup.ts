// Browser-mode setup file for the viewport regression suite.
//
// Sets the React act() flag so `act()` warnings don't pollute test logs when
// the canary tests render via `react-dom/client.createRoot`. We deliberately
// don't import `@testing-library/jest-dom` matchers here — browser mode
// asserts on real layout, not DOM-shape matchers, so they'd be unused weight.
//
// Cast through `Record` to avoid editing the global type definitions for a
// single setup file; `IS_REACT_ACT_ENVIRONMENT` is the documented opt-in
// flag from React's testing API.
(globalThis as unknown as Record<string, boolean>).IS_REACT_ACT_ENVIRONMENT = true;
