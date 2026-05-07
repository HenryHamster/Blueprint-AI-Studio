# Contributing to admin-ui

## Viewport regression check

`admin-ui` runs an opt-in viewport-regression suite under
`npm run test:viewport`. It launches headless Chromium via Vitest browser
mode + Playwright and asserts that the targeted component renders without
horizontal overflow at narrow widths (375px primary target).

On Blueprint agent VMs, `./check` bootstraps the Playwright Chromium
binary automatically before the viewport suite runs. The baked VM image
also includes the Chromium runtime libraries, so a fresh machine can run
the gate without manual browser setup.

The framework lives in `src/test/viewport.ts`; tests opt in by using the
filename suffix `*.viewport.test.{ts,tsx}` so they don't run under the
default jsdom suite (`npm run test:run`).

### Adding a viewport check for a new page

Five lines of test code per page. Example:

```tsx
import { expectNoOverflowAt } from '../test/viewport';
import { createRoot } from 'react-dom/client';
import { act } from 'react';
import { MyComponent } from './MyComponent';

test('MyComponent renders without horizontal overflow at 375px', async () => {
  await expectNoOverflowAt(375, async () => {
    const host = document.createElement('div');
    document.body.appendChild(host);
    const root = createRoot(host);
    await act(async () => {
      root.render(<MyComponent {...realisticProps} />);
    });
  });
});
```

Save it as `MyComponent.viewport.test.tsx` next to the component. Run
`npm run test:viewport` locally if you already have Chromium installed;
`./check` will do the bootstrap for you on agent VMs. CI will run it on
PRs that touch `admin-ui/**` once the workflow snippet below is applied.

### Opting an element out

If a node intentionally extends past the viewport edge (e.g. a tooltip
that's positioned off-screen and only revealed via JS), add the
`data-viewport-allow-overflow` attribute. The helper skips that node and
its descendants. Use sparingly — opting out of the check defeats its
purpose for that subtree.

### Required CI step (must be applied out-of-band)

CLAUDE.md forbids agents from editing `.github/workflows/`, so this snippet
must be added to `ci.yml`'s existing `frontend` job by a human maintainer
through the workflow-management channel:

```yaml
      - name: Install Playwright Chromium
        if: steps.changes.outputs.admin-ui == 'true'
        working-directory: admin-ui
        run: npx playwright install --with-deps chromium

      - name: Run viewport regression tests
        if: steps.changes.outputs.admin-ui == 'true'
        working-directory: admin-ui
        run: npm run test:viewport
```

The step is gated on the same `admin-ui/**` paths-filter the existing
`frontend` job uses so the ~150MB Chromium download only happens on PRs
that actually touch the admin UI.

A `./messages` note has been filed flagging that this snippet must be
applied out-of-band.

### Running locally without system Chromium libs

If `npm run test:viewport` fails on your machine with a missing `libatk` /
`libxcomposite` / similar shared library error, your host doesn't have the
Chromium runtime deps installed. Two options:

1. **Install the deps** — on Debian/Ubuntu, `npx playwright install --with-deps chromium`
   (requires sudo).
2. **Run inside the Playwright container** — use the official image which
   ships everything pre-installed:

   ```sh
   docker run --rm -v "$PWD/..:/work" -w /work/admin-ui \
     mcr.microsoft.com/playwright:v1.59.1-jammy \
     npm run test:viewport
   ```

   Bind-mount the **repo root** (one level above `admin-ui/`) because
   workspace `node_modules` are hoisted there.

CI uses option 1 (the workflow snippet above runs `npx playwright install
--with-deps chromium` before the test step).

### Why a separate runner?

The default `npm run test:run` is jsdom-based, fast, and runs in CI on
every push. jsdom does not implement layout, so it cannot detect
overflow. Browser mode is slower and pulls in Chromium, so it stays in
its own runner — opt in per page rather than wholesale.
