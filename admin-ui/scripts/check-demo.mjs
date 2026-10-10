import assert from 'node:assert/strict';
import { spawn } from 'node:child_process';
import { mkdir, readFile } from 'node:fs/promises';
import { createServer } from 'node:http';
import { extname, join, relative } from 'node:path';
import { fileURLToPath } from 'node:url';
import { setTimeout as delay } from 'node:timers/promises';
import { chromium } from 'playwright';

// Exercises the built artifact, so relative asset paths and auth separation are
// covered as well as React interactions. Install Chromium once before running.
const uiRoot = fileURLToPath(new URL('../', import.meta.url));
const output = fileURLToPath(
  new URL('../../.scratch/portfolio/browser/', import.meta.url),
);
const screenshots = process.argv.includes('--screenshots');
const port = Number(process.env.BLUEPRINT_DEMO_PORT ?? 3102);
assert(
  Number.isInteger(port) && port > 1024 && port < 65536,
  'Invalid demo port',
);
const origin = `http://127.0.0.1:${port}`;
const vite = fileURLToPath(
  new URL('../../node_modules/vite/bin/vite.js', import.meta.url),
);
const server = spawn(
  process.execPath,
  [vite, 'preview', '--config', 'vite.demo.config.ts', '--port', String(port)],
  { cwd: uiRoot, stdio: ['ignore', 'pipe', 'pipe'] },
);
let serverOutput = '';
server.stdout.on('data', (chunk) => {
  serverOutput += chunk;
});
server.stderr.on('data', (chunk) => {
  serverOutput += chunk;
});
let browser;
let subpathServer;

try {
  let ready = false;
  for (let attempt = 0; attempt < 100; attempt++) {
    if (server.exitCode !== null)
      throw new Error(`Demo server exited: ${serverOutput}`);
    try {
      if ((await fetch(origin, { signal: AbortSignal.timeout(500) })).ok) {
        ready = true;
        break;
      }
    } catch {
      /* the server is still starting */
    }
    await delay(100);
  }
  assert(ready, 'Demo server did not become ready');
  browser = await chromium.launch({
    headless: true,
    executablePath: process.env.BLUEPRINT_DEMO_BROWSER_PATH || undefined,
  });
  const page = await browser.newPage({
    viewport: { width: 1440, height: 1120 },
    deviceScaleFactor: 1,
  });
  const errors = [];
  const forbiddenRequests = [];
  page.on('pageerror', (error) => errors.push(error.message));
  page.on('request', (request) => {
    const url = new URL(request.url());
    if (url.origin !== origin || url.pathname.startsWith('/api'))
      forbiddenRequests.push(url.pathname);
  });
  page.on('response', (response) => {
    if (response.status() >= 400)
      errors.push(
        `HTTP ${response.status()} ${new URL(response.url()).pathname}`,
      );
  });
  await page.goto(origin, { waitUntil: 'networkidle' });
  await page
    .getByRole('heading', { name: 'From intent to implementation.' })
    .waitFor();
  await page
    .getByText('Sample data. Execution and checks are simulated.')
    .waitFor();
  await mkdir(output, { recursive: true });
  if (screenshots)
    await page.screenshot({ path: `${output}/pipeline.png`, fullPage: true });

  // Execution must stop at review, and a failed worker cannot advance.
  await page.getByRole('button', { name: 'Interrupt worker' }).click();
  await page.getByText('Worker interrupted. Retry to recover.').waitFor();
  assert.equal(
    await page.getByRole('button', { name: 'Next step' }).count(),
    0,
  );
  await page.getByRole('button', { name: 'Retry task' }).click();
  await page.getByText('Attempt 2', { exact: true }).waitFor();
  await page.getByRole('button', { name: 'Approve sample' }).waitFor();
  await delay(1600);
  assert(
    await page.getByRole('button', { name: 'Approve sample' }).isVisible(),
  );
  await page.getByRole('tab', { name: /Changes/ }).click();
  await page.getByText('Illustrative review artifact').waitFor();
  await page.getByRole('button', { name: 'Approve sample' }).click();
  await page.getByText('Task complete. No deployment performed.').waitFor();

  await page.getByRole('button', { name: 'New sample task' }).click();
  await page
    .getByRole('textbox', { name: 'Task title' })
    .fill('Add a health endpoint');
  await page
    .getByRole('textbox', { name: 'Goal' })
    .fill('Return readiness and test failure cases.');
  await page.getByRole('button', { name: 'Queue sample task' }).click();
  await page.getByRole('heading', { name: 'Add a health endpoint' }).waitFor();
  const detail = page.getByRole('article', { name: 'Selected task' });
  assert(await detail.getByText('Queued', { exact: true }).isVisible());
  await page.getByRole('tab', { name: 'Overview' }).focus();
  await page.keyboard.press('ArrowRight');
  assert.equal(
    await page.evaluate(() => document.activeElement?.textContent),
    'Execution log',
  );
  await page.keyboard.press('End');
  assert.equal(
    await page
      .getByRole('tab', { name: /Changes/ })
      .getAttribute('aria-selected'),
    'true',
  );

  // Check every view at narrow, tablet, and desktop sizes.
  for (const width of [375, 768, 1440]) {
    await page.setViewportSize({ width, height: 1000 });
    for (const view of ['Pipeline', 'Fleet', 'Architecture']) {
      await page.getByRole('button', { name: view, exact: true }).click();
      assert.equal(
        await page.evaluate(
          () => document.documentElement.scrollWidth > innerWidth,
        ),
        false,
        `${view} overflows at ${width}px`,
      );
      if (screenshots && width === 1440 && view === 'Architecture')
        await page.screenshot({
          path: `${output}/architecture.png`,
          fullPage: true,
        });
    }
  }
  await page.reload({ waitUntil: 'networkidle' });
  await page.setViewportSize({ width: 390, height: 844 });
  if (screenshots)
    await page.screenshot({ path: `${output}/mobile.png`, fullPage: true });
  assert.equal(
    await page.getByRole('heading', { name: 'Add a health endpoint' }).count(),
    0,
    'Refresh should reset the demo',
  );
  assert.deepEqual(
    forbiddenRequests,
    [],
    'Demo must not contact external services or the backend',
  );
  assert.deepEqual(errors, [], 'Browser errors or failed asset requests');
  // Serve the unchanged build under a directory, as a static host would.
  const dist = fileURLToPath(new URL('../dist-demo/', import.meta.url));
  subpathServer = createServer(async (request, response) => {
    const pathname = new URL(request.url, 'http://localhost').pathname;
    const file = join(
      dist,
      decodeURIComponent(pathname.slice('/blueprint/'.length)) || 'index.html',
    );
    if (
      !pathname.startsWith('/blueprint/') ||
      relative(dist, file).startsWith('..')
    ) {
      response.writeHead(404).end();
      return;
    }
    try {
      const types = {
        '.html': 'text/html',
        '.js': 'text/javascript',
        '.css': 'text/css',
        '.svg': 'image/svg+xml',
      };
      const data = await readFile(file);
      response.writeHead(200, {
        'Content-Type': types[extname(file)] || 'application/octet-stream',
      });
      response.end(data);
    } catch {
      response.writeHead(404).end();
    }
  });
  await new Promise((resolve, reject) => {
    subpathServer.once('error', reject);
    subpathServer.listen(0, '127.0.0.1', resolve);
  });
  const subpathOrigin = `http://127.0.0.1:${subpathServer.address().port}`;
  const subpage = await browser.newPage();
  const subpathErrors = [];
  subpage.on('pageerror', (error) => subpathErrors.push(error.message));
  subpage.on('response', (response) => {
    if (response.status() >= 400)
      subpathErrors.push(`HTTP ${response.status()}`);
  });
  await subpage.goto(`${subpathOrigin}/blueprint/`, {
    waitUntil: 'networkidle',
  });
  await subpage
    .getByRole('heading', { name: 'From intent to implementation.' })
    .waitFor();
  assert.deepEqual(subpathErrors, [], 'Static subdirectory hosting failed');
  await subpage.close();
  console.log(
    `Demo browser checks passed (${browser.version()}): submission, recovery, review, keyboard tabs, 3 viewport sizes, static subdirectory hosting, and no external/API requests.`,
  );
  if (screenshots) console.log(`Screenshots saved to ${output}`);
} finally {
  await browser?.close();
  if (subpathServer)
    await new Promise((resolve) => subpathServer.close(resolve));
  server.kill('SIGTERM');
  await Promise.race([
    new Promise((resolve) => server.once('exit', resolve)),
    delay(3000),
  ]);
  if (server.exitCode === null && server.signalCode === null)
    server.kill('SIGKILL');
}
