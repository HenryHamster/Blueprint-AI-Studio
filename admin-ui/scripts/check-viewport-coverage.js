#!/usr/bin/env node
import fs from 'fs';
import path from 'path';

const configPath = path.join(process.cwd(), 'src/test/viewport-required.json');
const config = JSON.parse(fs.readFileSync(configPath, 'utf-8'));
const componentsDir = path.join(process.cwd(), 'src/components');

let missing = [];
for (const component of config.components) {
  const testFile = path.join(componentsDir, `${component}.viewport.test.tsx`);
  if (!fs.existsSync(testFile)) {
    missing.push(component);
  }
}

if (missing.length > 0) {
  console.error('[viewport] Missing viewport tests for required components:');
  for (const component of missing) {
    console.error(`  ${component} — no ${component}.viewport.test.tsx found`);
  }
  process.exit(1);
}

console.log(`[viewport] All ${config.components.length} required viewport tests present`);
process.exit(0);
