# Interactive demo

The portfolio demo is a standalone React application with sample tasks, logs, patch, and fleet data. It presents the system's core workflow without running a model, contacting GitHub, or starting the backend.

## Run

Requires Node.js 22+ and npm. From the repository root:

```bash
npm ci
npm run demo
```

Open http://127.0.0.1:3002. The dev server binds to loopback and fails if the port is already occupied. For a custom port, invoke the workspace directly:

```bash
npm run demo --workspace admin-ui -- --port 3003
```

## Two-minute walkthrough

1. Inspect the running activity-pagination task and its lifecycle.
2. Choose **Interrupt worker**. Execution stops and the log shows recovery becoming available.
3. Choose **Retry task**. The attempt number increments and playback stops at **Needs review**.
4. Open **Changes** to inspect the illustrative patch, then choose **Approve sample**. Task completion does not claim a deployment.
5. Choose **New sample task**, enter a title and goal, and queue it. Use **Next step** or **Replay** to explore its lifecycle.
6. Visit **Fleet** and **Architecture** for the execution boundaries and a map to the source.

Search filters the task list. Arrow keys, Home, and End navigate the detail tabs. Refresh resets the in-memory sample state. A new prompt stays in this browser session.

## Static artifact

```bash
npm run demo:build
npm run demo:preview
```

The output is `admin-ui/dist-demo/`. Its assets use relative paths, so it can be served from a static host or a subdirectory. Serve it over HTTP; opening `index.html` with `file://` is not a supported browser module setup.

The build uses `vite.demo.config.ts`, a separate HTML entry point, `envDir: false`, and no API proxy. It does not load `.env` files or the operational dashboard. Only explicitly public sample content belongs in the demo.

## Verify

```bash
npm run demo:test
npm run typecheck --workspace admin-ui
npm run lint --workspace admin-ui

# Install the browser once, then check the built artifact
npx playwright install chromium
npm run demo:check
```

The browser check starts and removes its own loopback preview server on port 3102. It tests submission, interruption/retry, review approval, keyboard tabs, reload reset, and all three views at widths 375, 768, and 1440. It also fails on browser errors, asset failures, external requests, and `/api` requests.

For an already-installed Chromium executable, set `BLUEPRINT_DEMO_BROWSER_PATH`. Set `BLUEPRINT_DEMO_PORT` if the check's port is occupied. To capture screenshots for an intentional presentation update:

```bash
npm run demo:build
node admin-ui/scripts/check-demo.mjs --screenshots
```

Screenshots are written to `.scratch/portfolio/browser/`. Curated copies used by the README live in `docs/portfolio/images/`; generated test attachments remain ignored.

## Source

| File | Responsibility |
| --- | --- |
| [`DemoApp.tsx`](../../admin-ui/src/demo/DemoApp.tsx) | Workspace navigation and in-memory task/playback state |
| [`TaskDetail.tsx`](../../admin-ui/src/demo/TaskDetail.tsx) | Task phases, simulated output, patch, and review controls |
| [`NewTaskForm.tsx`](../../admin-ui/src/demo/NewTaskForm.tsx) | Local sample prompt submission |
| [`SystemViews.tsx`](../../admin-ui/src/demo/SystemViews.tsx) | Fleet and architecture tours |
| [`model.ts`](../../admin-ui/src/demo/model.ts) | Fixtures, bounded transitions, and illustrative logs |
| [`check-demo.mjs`](../../admin-ui/scripts/check-demo.mjs) | Browser verification of the built artifact |

The production dashboard remains at `admin-ui/src/main.tsx` and runs with `npm run dev` on port 3001. Its setup and authentication requirements are different; see [CONTRIBUTING.md](../../CONTRIBUTING.md).
