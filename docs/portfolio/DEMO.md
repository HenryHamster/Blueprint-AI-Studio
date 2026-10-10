# Run the existing application UI

`npm run demo` launches Blueprint's existing operator workspace on port 3002. It uses the same HTML entry point, React components, styles, authentication, API client, and Vite configuration as `npm run dev`.

## Start the frontend

Requires Node.js 22+ and npm. From the repository root:

```bash
npm ci
npm run demo
```

Open http://127.0.0.1:3002. The server binds to loopback and fails if the port is already occupied. When working over SSH, forward port 3002 to your computer.

For normal frontend development, `npm run dev` starts the same UI on port 3001.

## Backend and authentication

The dashboard expects an API at `http://127.0.0.1:8080`; Vite proxies `/api` there. Set `VITE_DEV_PROXY_TARGET` to change the development proxy.

Configure Firebase web authentication or the existing trusted-local-development token path as described in [CONTRIBUTING.md](../../CONTRIBUTING.md). Starting the frontend alone does not start the database, API, workers, or execution capacity.

A full workflow also needs the relevant GitHub and model-provider configuration. The [runner runbook](../RUNNER_RUNBOOK.md) covers execution capacity. These requirements are part of the current application.

## Take a look

With a configured backend, explore the existing conversations, project workspace, pipeline, and runtime views. The [recorded operator walkthrough](../walkthroughs/v26-operator-walkthrough.md) also includes screenshots of the application and observations from an earlier session.

## Verify

```bash
npm run typecheck --workspace admin-ui
npm run lint --workspace admin-ui
npm run test:run --workspace admin-ui
```

See the [verification record](VERIFICATION.md) for executed checks.
