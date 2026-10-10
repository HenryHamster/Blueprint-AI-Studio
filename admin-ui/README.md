# Operator workspace and portfolio demo

React 19, TypeScript, and Vite. The operational dashboard uses Tailwind CSS and authenticated backend APIs; the standalone portfolio demo uses its own stylesheet, sample data, and HTML entry point.

## Run from the repository root

```bash
npm ci
npm run demo                    # Credential-free demo, port 3002
npm run dev                     # Operational dashboard, port 3001
```

The dashboard needs the backend and authentication configuration. See [CONTRIBUTING.md](../CONTRIBUTING.md). The demo needs neither; see the [demo guide](../docs/portfolio/DEMO.md).

## Verify

```bash
npm run typecheck --workspace admin-ui
npm run lint --workspace admin-ui
npm run test:run --workspace admin-ui
npm run demo:test
npm run demo:build

# Install Chromium once before the browser check
npx playwright install chromium
npm run demo:check
```

Unit tests use happy-dom. Browser viewport tests are a separate suite; see [TESTING_GUIDE.md](../docs/TESTING_GUIDE.md). On agent VMs, use type checking for the full dashboard; its production bundle runs in CI. The small demo bundle is built and checked locally.

## Source map

| Path | Responsibility |
| --- | --- |
| `src/main.tsx`, `src/App.tsx` | Operational application and workspace routing |
| `src/components/` | Task, project, conversation, fleet, settings, and release surfaces |
| `src/hooks/` | State, polling, WebSocket, and navigation hooks |
| `src/api/` | Authenticated request paths; `src/api.ts` provides the compatibility facade |
| `src/types/`, `src/utils/` | Contracts and shared helpers |
| `src/test/` | Unit and viewport test support |
| `src/demo/`, `vite.demo.config.ts` | Independent portfolio presentation and static build |
| `scripts/check-demo.mjs` | Browser checks of the built demo |

For a short source tour, read the [architecture guide](../docs/portfolio/ARCHITECTURE.md). For detailed component reference, read [FRONTEND_COMPONENTS.md](../docs/FRONTEND_COMPONENTS.md).
