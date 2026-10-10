# Operator workspace

The existing Blueprint dashboard, built with React 19, TypeScript, Vite, and Tailwind CSS. It uses authenticated backend APIs.

## Run from the repository root

```bash
npm ci
npm run dev                     # Dashboard on port 3001
npm run demo                    # Same dashboard on port 3002
```

Configure the backend and authentication using [CONTRIBUTING.md](../CONTRIBUTING.md). Both commands load `index.html`, `src/main.tsx`, and `src/App.tsx` through the existing Vite configuration.

## Verify

```bash
npm run typecheck --workspace admin-ui
npm run lint --workspace admin-ui
npm run test:run --workspace admin-ui
```

Unit tests use happy-dom. Browser viewport tests are a separate suite; see [TESTING_GUIDE.md](../docs/TESTING_GUIDE.md). On agent VMs, use type checking; the production bundle runs in CI.

## Source map

| Path | Responsibility |
| --- | --- |
| `src/main.tsx`, `src/App.tsx` | Application and workspace routing |
| `src/components/` | Task, project, conversation, fleet, settings, and release surfaces |
| `src/hooks/` | State, polling, WebSocket, and navigation hooks |
| `src/api/` | Authenticated request paths; `src/api.ts` provides the compatibility facade |
| `src/types/`, `src/utils/` | Contracts and shared helpers |
| `src/test/` | Unit and viewport test support |

For the component boundaries and backend connections, read the [architecture guide](../docs/ARCHITECTURE.md).
