#!/usr/bin/env node
// viewport-live-capture.js — headless Chromium capture of the live admin-UI SPA.
// Usage: node viewport-live-capture.js <url> <outDir> <widths>
//   widths: space-separated string, e.g. "375 1440"
// Exits 0 on success, 1 on any error.

const { chromium } = require('playwright');
const path = require('path');
const fs = require('fs');

async function main() {
  const [,, url, outDir, widthsArg] = process.argv;
  if (!url || !outDir || !widthsArg) {
    console.error('Usage: node viewport-live-capture.js <url> <outDir> <widths>');
    process.exit(1);
  }

  const widths = widthsArg.split(/\s+/).map(Number).filter(Boolean);
  if (widths.length === 0) {
    console.error('viewport-live-capture: no valid widths provided');
    process.exit(1);
  }

  fs.mkdirSync(outDir, { recursive: true });

  const browser = await chromium.launch({ headless: true });
  try {
    for (const width of widths) {
      console.log(`viewport-live-capture: capturing width=${width}`);
      const context = await browser.newContext({ viewport: { width, height: 900 } });
      const page = await context.newPage();

      await page.goto(url, { waitUntil: 'networkidle', timeout: 30000 });

      // Wait for a reliable rendered-state selector.
      try {
        await page.waitForSelector('[data-testid="orchestrator-health-bar"]', { timeout: 15000 });
      } catch (_) {
        // fallback: wait for any task list content
        await page.waitForSelector('text=queued', { timeout: 15000 });
      }

      const outPath = path.join(outDir, `live-${width}px.png`);
      await page.screenshot({ path: outPath, fullPage: true });
      console.log(`viewport-live-capture: wrote ${outPath}`);
      await context.close();
    }
  } finally {
    await browser.close();
  }
}

main().catch(err => {
  const msg = err && err.message ? err.message : String(err);
  if (/libatk|libxcomposite|libgbm|libnss3|libasound|cannot open shared object|executable doesn|host system is missing|please install|playwright install|missing browser|missing chromium/i.test(msg)) {
    console.error('VIEWPORT_GATE_INFRA_UNAVAILABLE: ' + msg);
    process.exit(75);
  }
  console.error('viewport-live-capture error:', msg);
  process.exit(1);
});
