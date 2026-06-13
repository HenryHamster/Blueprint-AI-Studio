# Blueprint Admin UI

The admin UI is a React + TypeScript application built with Vite. It provides a dashboard for monitoring Blueprint agents, reviewing autonomous research findings, configuring settings, and viewing analytics in real time.

## Architecture

- **Framework:** React 19 + TypeScript
- **Build tool:** Vite (with HMR for development)
- **Styling:** Tailwind CSS
- **Testing:** Vitest + Playwright (viewport regression tests)
- **Deployment:** Built artifact deployed with the frontend hosting flow described in [../Deploy.md](../Deploy.md)

## Development Setup

```bash
# Install dependencies (from root of repo)
npm install

# Start dev server (runs on port 3001)
npm run dev

# Build for production
npm run build

# Run tests
npm test                   # jsdom unit tests
npm run test:viewport      # Chromium viewport regression tests
```

See [../DEVELOPMENT_GUIDE.md](../docs/DEVELOPMENT_GUIDE.md#react) for comprehensive development guidance.

## Key Features

- **Real-time task monitoring** — View agent logs and task status via WebSocket streaming
- **Autonomous research dashboard** — Track research findings, strategic directions, and autonomous cycles
- **Project management** — Configure projects, manage GitHub integration, set approval gates
- **Analytics** — Visualize metrics, cost analysis, and system health
- **Settings** — Manage team access, LLM providers, warm VM pool configuration, and more

## File Organization

- `src/components/` — React components organized by feature (Task, Settings, Analytics, etc.)
- `src/hooks/` — Custom React hooks (WebSocket, state management, polling)
- `src/utils/` — Utility functions (API calls, formatting, time handling)
- `src/types/` — TypeScript type definitions
- `src/test/` — Testing utilities (viewport test framework)
- `public/` — Static assets

## Component Documentation

See [../docs/FRONTEND_COMPONENTS.md](../docs/FRONTEND_COMPONENTS.md) for detailed component reference including:
- Component props and usage patterns
- Data hooks (`useAppState`, `usePoller`, `useWebSocket`)
- Styling conventions (Tailwind CSS)
- Testing patterns

## API Integration

API calls are in `src/api.ts` and handle:
- Firebase authentication
- Request deduplication (in-flight request collapsing)
- Automatic retries for transient Cloud Run errors (502/503)
- WebSocket authentication via signed tickets

See [../docs/API_REFERENCE.md](../docs/API_REFERENCE.md) for complete endpoint reference.

## Viewport Regression Tests

The admin-ui runs an opt-in viewport regression suite to ensure pages render without horizontal overflow at mobile widths (375px target).

```bash
npm run test:viewport      # Run all viewport tests
npm run test:viewport my-component  # Run specific component
```

Tests use the `*.viewport.test.tsx` naming convention and are skipped by the default jsdom suite. See [../docs/TESTING_GUIDE.md#viewport-tests-playwright-browser-based](../docs/TESTING_GUIDE.md#viewport-tests-playwright-browser-based) for details on adding a test for a new component.

For headless Chromium setup help, see [../docs/TESTING_GUIDE.md#viewport-tests-playwright-browser-based](../docs/TESTING_GUIDE.md#viewport-tests-playwright-browser-based).

## Styling

All styling uses Tailwind CSS. No CSS modules or styled-components.

- Tailwind config: `tailwind.config.js` (at repo root)
- Common colors, spacing, and responsive utilities documented in component source code
- Prefer utility classes over custom CSS

## Integration with Backend

The admin-ui communicates with the FastAPI backend (`backend/app/routers/admin_*.py`) for:
- Project and task data
- WebSocket log streaming
- Settings management
- Analytics data
- GitHub integration

All backend routes require authentication (Firebase token or API key).

## Contributing

1. Read [CONTRIBUTING.md](./CONTRIBUTING.md) for viewport testing setup
2. Follow component patterns in `src/components/`
3. Update `src/types/` when adding new data structures
4. Run `npm test` and `npm run test:viewport` before committing
5. See [../CLAUDE.md](../CLAUDE.md) for coding standards

## Troubleshooting

**Module import errors?**
- Run `npm install` from repo root to ensure hoisted node_modules are available

**Viewport tests fail with missing Chromium?**
- See [../docs/TESTING_GUIDE.md#viewport-tests-playwright-browser-based](../docs/TESTING_GUIDE.md#viewport-tests-playwright-browser-based)

**WebSocket disconnects?**
- Check backend is running on correct port (default 8080)
- Verify `VITE_API_URL` environment variable if not using localhost

See [../DEBUG_GUIDE.md](../docs/DEBUG_GUIDE.md) for more troubleshooting guidance.
