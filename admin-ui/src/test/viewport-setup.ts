// Browser-mode setup file for the viewport regression suite.
//
// Imports `index.css` (which `@import "tailwindcss"` pulls in) so that
// Tailwind utility classes actually apply at test time. Without this,
// the @tailwindcss/vite plugin in vitest.viewport.config.ts is a no-op
// — utility classes like `break-all`, `min-w-0`, `truncate`,
// `overflow-x-auto`, the entire `w-*` family etc. become inert, and the
// gate produces both false positives (production-correct components
// that fail the test) and false negatives (real overflow that Tailwind
// would expose passes).
//
// Sets the React act() flag so `act()` warnings don't pollute test logs when
// the canary tests render via `react-dom/client.createRoot`. We deliberately
// don't import `@testing-library/jest-dom` matchers here — browser mode
// asserts on real layout, not DOM-shape matchers, so they'd be unused weight.
//
// Cast through `Record` to avoid editing the global type definitions for a
// single setup file; `IS_REACT_ACT_ENVIRONMENT` is the documented opt-in
// flag from React's testing API.
import '../index.css';

(globalThis as unknown as Record<string, boolean>).IS_REACT_ACT_ENVIRONMENT = true;
