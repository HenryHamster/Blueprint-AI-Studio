// Regenerate backend/app/strategic_ops_manifest.json from the canonical
// TypeScript registry (agent/src/handlers/strategic-ops-registry.ts).
//
// The registry is the SINGLE source of truth for the strategist mutating-op
// surface; the JSON manifest is a checked-in projection of it that the Python
// backend drift-guards read. Regenerating it was previously a hand edit, which
// is exactly the drift the registry exists to prevent — this script makes the
// projection reproducible.
//
//   cd agent && npx tsx scripts/regen-strategic-ops-manifest.mjs
//
// The gate remains agent/src/handlers/strategic-ops-registry.test.ts
// ("backend/app/strategic_ops_manifest.json equals the registry projection").
import { writeFileSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

import { strategicOpsManifest } from '../src/handlers/strategic-ops-registry.ts';

const here = path.dirname(fileURLToPath(import.meta.url));
const target = path.resolve(here, '..', '..', 'backend', 'app', 'strategic_ops_manifest.json');

writeFileSync(target, JSON.stringify(strategicOpsManifest(), null, 4) + '\n', 'utf8');
console.log(`wrote ${target}`);
