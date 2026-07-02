import { describe, it, expect } from 'vitest';
import { readFileSync, readdirSync, statSync } from 'fs';
import path from 'path';
import { fileURLToPath } from 'url';

/**
 * Guard: full-replacement vi.mock factories must cover all runtime exports of
 * the modules they replace.
 *
 * Two enforcement tiers:
 *
 * 1. Helper factory coverage (dynamic import): The four centralized mock
 *    factories (createApiMock, createFirebaseMock, createPollerMock,
 *    createStrategicStateApiMock) are imported and their output keys are
 *    compared against the real source-module runtime exports.  A new export
 *    added to api.ts / firebase.ts / usePoller.ts / strategicStateApi.ts with
 *    no matching key in the helper factory will fail here.
 *
 * 2. Inline mock scanner (fs text scan): Every .test.{ts,tsx} file under
 *    src/ is scanned for vi.mock() factories that fully replace the firebase,
 *    api, usePoller, or strategicStateApi modules. Factories that delegate to
 *    the matching known complete helper for the mocked module tail, or that
 *    use importOriginal/importActual, are treated as safe and skipped. Current
 *    legacy api full-replacement sites are allowlisted only while the file
 *    still contains the baseline single api mock; any additional api mock in
 *    those files is scanned here.
 */

const SRC_DIR = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');

// ---------------------------------------------------------------------------
// fs utilities
// ---------------------------------------------------------------------------

function walkTestFiles(dir: string): string[] {
  const results: string[] = [];
  let entries: string[];
  try { entries = readdirSync(dir); } catch { return results; }
  for (const entry of entries) {
    const full = path.join(dir, entry);
    try {
      const stat = statSync(full);
      if (stat.isDirectory()) {
        results.push(...walkTestFiles(full));
      } else if (/\.test\.(ts|tsx)$/.test(entry)) {
        results.push(full);
      }
    } catch { /* skip unreadable entries */ }
  }
  return results;
}

// ---------------------------------------------------------------------------
// Runtime export extraction from source text
// Used as a fast cross-check fallback; dynamic import is authoritative.
// ---------------------------------------------------------------------------

function extractRuntimeExportsFromText(filePath: string): Set<string> {
  let src: string;
  try { src = readFileSync(filePath, 'utf-8'); } catch { return new Set(); }
  const names = new Set<string>();
  // Matches: export [async] function/const/let/class  (skips interface, type, abstract type)
  const re = /^export\s+(?:async\s+)?(?:function|const|let|class)\s+(\w+)/gm;
  let m: RegExpExecArray | null;
  while ((m = re.exec(src)) !== null) names.add(m[1]);
  return names;
}

// ---------------------------------------------------------------------------
// Inline mock scanner
// ---------------------------------------------------------------------------

const SAFE_FACTORY_HELPER_MODULES_BY_TAIL = new Map([
  ['api', 'test/api-mock-complete'],
  ['firebase', 'test/firebase-mock-complete'],
  ['usePoller', 'test/poller-mock-complete'],
  ['strategicStateApi', 'test/strategic-state-api-mock-complete'],
]);

// The module "tails" (final path segment) that the inline scanner guards.
const GUARDED_TAILS = new Set(['api', 'firebase', 'usePoller', 'strategicStateApi']);

// Current legacy api full-replacement mocks are allowed so the guard can block
// new inline api factories without forcing a large migration in this change.
const LEGACY_API_MOCK_FILES = new Set([
  'components/AdversarialQualityPanel.viewport.test.tsx',
  'components/AlertsBanner.viewport.test.tsx',
  'components/AlertsPopover.viewport.test.tsx',
  'components/AutonomousStatus.viewport.test.tsx',
  'components/ConfirmDialog.viewport.test.tsx',
  'components/ConsoleHome.test.tsx',
  'components/ConsoleHome.viewport.test.tsx',
  'components/DashboardHome.viewport.test.tsx',
  'components/DirectionAutoPauseWidget.test.tsx',
  'components/DirectionAutoPauseWidget.viewport.test.tsx',
  'components/DirectionPortfolioSection.test.tsx',
  'components/DirectionPortfolioSection.viewport.test.tsx',
  'components/DriftSignalsCard.test.tsx',
  'components/DriftSignalsCard.viewport.test.tsx',
  'components/EventVisibilityMenu.viewport.test.tsx',
  'components/EvergreenFillObservabilityPanel.test.tsx',
  'components/FixLoopConvergenceWidget.test.tsx',
  'components/FixLoopConvergenceWidget.viewport.test.tsx',
  'components/FleetDetailDrawer.viewport.test.tsx',
  'components/FleetRunnersPanel.viewport.test.tsx',
  'components/HeaderBar.viewport.test.tsx',
  'components/ImprovementLanesPanel.test.tsx',
  'components/ImprovementLanesPanel.viewport.test.tsx',
  'components/KanbanBoard.viewport.test.tsx',
  'components/KanbanColumn.test.tsx',
  'components/KanbanColumn.viewport.test.tsx',
  'components/KnowledgeBrowser.viewport.test.tsx',
  'components/LaneDiagPanel.test.tsx',
  'components/LaneDiagPanel.viewport.test.tsx',
  'components/LlmCostBreakdownPanel.test.tsx',
  'components/LlmCostBreakdownPanel.viewport.test.tsx',
  'components/MessagesPanel.viewport.test.tsx',
  'components/MessageTypeDropdown.viewport.test.tsx',
  'components/MetricsDashboard.test.tsx',
  'components/MetricsDashboard.viewport.test.tsx',
  'components/MuteWindowMenu.viewport.test.tsx',
  'components/NewProjectModal.viewport.test.tsx',
  'components/NewTaskModal.test.tsx',
  'components/NewTaskModal.viewport.test.tsx',
  'components/NullBranchDiagnosticsPanel.test.tsx',
  'components/NullBranchDiagnosticsPanel.viewport.test.tsx',
  'components/OrchestratorHealthBar.test.tsx',
  'components/OrchestratorHealthBar.viewport.test.tsx',
  'components/OuterReviewLoopConvergenceWidget.test.tsx',
  'components/OuterReviewLoopConvergenceWidget.viewport.test.tsx',
  'components/PRMonitorSection.viewport.test.tsx',
  'components/ProblemModelEditor.viewport.test.tsx',
  'components/ProblemModelViewer.test.tsx',
  'components/ProblemModelViewer.viewport.test.tsx',
  'components/ProjectAlertDomainsSection.test.tsx',
  'components/ProjectAlertDomainsSection.viewport.test.tsx',
  'components/ProjectAutoReviewSection.test.tsx',
  'components/ProjectManagementModal.test.tsx',
  'components/ProviderCostPanel.test.tsx',
  'components/ProviderCostPanel.viewport.test.tsx',
  'components/ProviderModelBreakdownPanel.test.tsx',
  'components/ProviderModelBreakdownPanel.viewport.test.tsx',
  'components/RecentErrorsWidget.test.tsx',
  'components/RecentErrorsWidget.viewport.test.tsx',
  'components/RemediationFunnelSection.test.tsx',
  'components/RemediationFunnelSection.viewport.test.tsx',
  'components/ResearchBrowser.test.tsx',
  'components/ResearchBrowser.viewport.test.tsx',
  'components/ReviewQualitySection.viewport.test.tsx',
  'components/RunningUpdatePanel.viewport.test.tsx',
  'components/RuntimeAccountsPanel.test.tsx',
  'components/RuntimeAccountsPanel.viewport.test.tsx',
  'components/SettingsDeploySection.test.tsx',
  'components/SettingsDeploySection.viewport.test.tsx',
  'components/SettingsOrchestratorSection.test.tsx',
  'components/SrUnhandledEdgesRetrospectivesPanel.viewport.test.tsx',
  'components/StrategicAuditPanel.test.tsx',
  'components/StrategicAuditPanel.viewport.test.tsx',
  'components/TaskCard.test.tsx',
  'components/TaskCard.viewport.test.tsx',
  'components/TaskDetailDrawer.test.tsx',
  'components/TaskDetailDrawerDiagnostics.viewport.test.tsx',
  'components/TaskDetailDrawerMeta.viewport.test.tsx',
  'components/TaskDetailDrawerSelfReview.viewport.test.tsx',
  'components/TaskFixLoopPanel.viewport.test.tsx',
  'components/VersionLifecyclePanel.viewport.test.tsx',
  'components/ViewportDebtPanel.viewport.test.tsx',
  'components/WarmReuseVsCostTrend.test.tsx',
  'components/WarmReuseVsCostTrend.viewport.test.tsx',
  'components/WarmVmPanel.test.tsx',
  'components/WarmVmPanel.viewport.test.tsx',
  'components/WorkbenchComposition.viewport.test.tsx',
  'hooks/useDeploymentState.test.ts',
  'hooks/useLogViewer.test.ts',
  'hooks/useTaskModal.test.ts',
  'hooks/useWorkbenchCommands.test.tsx',
  'test/kanban-composed.viewport.test.tsx',
  'test/messages-composed.viewport.test.tsx',
  'test/pipeline-composed.viewport.test.tsx',
]);

interface InlineMockFinding {
  file: string;
  moduleSpec: string;
  factoryKeys: Set<string>;
}

function isLegacyApiMock(filePath: string): boolean {
  const rel = path.relative(SRC_DIR, filePath).replaceAll(path.sep, '/');
  return LEGACY_API_MOCK_FILES.has(rel);
}

function countMockCalls(src: string, moduleSpec: string): number {
  const re = new RegExp(`vi\\.mock\\(\\s*['"]${moduleSpec.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')}['"]\\s*,`, 'g');
  return [...src.matchAll(re)].length;
}

function skipWhitespaceAndComments(text: string, index: number): number {
  let i = index;
  while (i < text.length) {
    if (/\s/.test(text[i] ?? '')) {
      i++;
      continue;
    }
    if (text[i] === '/' && text[i + 1] === '/') {
      i += 2;
      while (i < text.length && text[i] !== '\n') i++;
      continue;
    }
    if (text[i] === '/' && text[i + 1] === '*') {
      i += 2;
      while (i < text.length - 1 && !(text[i] === '*' && text[i + 1] === '/')) i++;
      i += 2;
      continue;
    }
    break;
  }
  return i;
}

function stripWrappingParens(text: string): string {
  let trimmed = text.trim();
  while (trimmed.startsWith('(') && trimmed.endsWith(')')) {
    let depth = 0;
    let inString = false;
    let stringChar = '';
    let inLineComment = false;
    let inBlockComment = false;
    let wrapsWholeText = true;

    for (let i = 0; i < trimmed.length; i++) {
      const ch = trimmed[i];

      if (inLineComment) {
        if (ch === '\n') inLineComment = false;
        continue;
      }
      if (inBlockComment) {
        if (ch === '*' && trimmed[i + 1] === '/') {
          inBlockComment = false;
          i++;
        }
        continue;
      }
      if (inString) {
        if (ch === '\\') {
          i++;
          continue;
        }
        if (ch === stringChar) inString = false;
        continue;
      }

      if (ch === '/' && trimmed[i + 1] === '/') {
        inLineComment = true;
        i++;
        continue;
      }
      if (ch === '/' && trimmed[i + 1] === '*') {
        inBlockComment = true;
        i++;
        continue;
      }
      if (ch === '"' || ch === "'" || ch === '`') {
        inString = true;
        stringChar = ch;
        continue;
      }
      if (ch === '(') {
        depth++;
        continue;
      }
      if (ch === ')') {
        depth--;
        if (depth === 0 && i !== trimmed.length - 1) {
          wrapsWholeText = false;
          break;
        }
      }
    }

    if (!wrapsWholeText) break;
    trimmed = trimmed.slice(1, -1).trim();
  }
  return trimmed;
}

function startsWithWord(text: string, index: number, word: string): boolean {
  if (!text.startsWith(word, index)) return false;
  const before = text[index - 1];
  const after = text[index + word.length];
  return !(/[A-Za-z0-9_$]/.test(before ?? '')) && !(/[A-Za-z0-9_$]/.test(after ?? ''));
}

function readDelimitedExpression(text: string, start: number, terminators: Set<string>): { expression: string; nextIndex: number } {
  let parenDepth = 0;
  let bracketDepth = 0;
  let braceDepth = 0;
  let inString = false;
  let stringChar = '';
  let inLineComment = false;
  let inBlockComment = false;

  for (let i = start; i < text.length; i++) {
    const ch = text[i];

    if (inLineComment) {
      if (ch === '\n') inLineComment = false;
      continue;
    }
    if (inBlockComment) {
      if (ch === '*' && text[i + 1] === '/') {
        inBlockComment = false;
        i++;
      }
      continue;
    }
    if (inString) {
      if (ch === '\\') {
        i++;
        continue;
      }
      if (ch === stringChar) inString = false;
      continue;
    }

    if (ch === '/' && text[i + 1] === '/') {
      inLineComment = true;
      i++;
      continue;
    }
    if (ch === '/' && text[i + 1] === '*') {
      inBlockComment = true;
      i++;
      continue;
    }
    if (ch === '"' || ch === "'" || ch === '`') {
      inString = true;
      stringChar = ch;
      continue;
    }

    if (ch === '(') {
      parenDepth++;
      continue;
    }
    if (ch === ')') {
      if (parenDepth > 0) {
        parenDepth--;
        continue;
      }
    }
    if (ch === '[') {
      bracketDepth++;
      continue;
    }
    if (ch === ']') {
      if (bracketDepth > 0) {
        bracketDepth--;
        continue;
      }
    }
    if (ch === '{') {
      braceDepth++;
      continue;
    }
    if (ch === '}') {
      if (braceDepth > 0) {
        braceDepth--;
        if (braceDepth === 0 && terminators.has('}')) {
          return { expression: text.slice(start, i).trim(), nextIndex: i };
        }
        continue;
      }
    }

    if (parenDepth === 0 && bracketDepth === 0 && braceDepth === 0 && terminators.has(ch)) {
      return { expression: text.slice(start, i).trim(), nextIndex: i };
    }
  }

  return { expression: text.slice(start).trim(), nextIndex: text.length };
}

function collectKnownHelperBindings(filePath: string, factoryBody: string, expectedTail: string): Set<string> {
  const helperModule = SAFE_FACTORY_HELPER_MODULES_BY_TAIL.get(expectedTail);
  if (!helperModule) return new Set();

  const bindings = new Set<string>();
  const keywordLengths = new Map([
    ['const', 5],
    ['let', 3],
    ['var', 3],
  ]);

  for (let i = 0; i < factoryBody.length; i++) {
    const ch = factoryBody[i];
    if (ch === '/' && factoryBody[i + 1] === '/') {
      while (i < factoryBody.length && factoryBody[i] !== '\n') i++;
      continue;
    }
    if (ch === '/' && factoryBody[i + 1] === '*') {
      i += 2;
      while (i < factoryBody.length - 1 && !(factoryBody[i] === '*' && factoryBody[i + 1] === '/')) i++;
      i++;
      continue;
    }
    if (ch === '"' || ch === "'" || ch === '`') {
      const stringChar = ch;
      i++;
      while (i < factoryBody.length) {
        if (factoryBody[i] === '\\') {
          i += 2;
          continue;
        }
        if (factoryBody[i] === stringChar) break;
        i++;
      }
      continue;
    }

    let keywordLength = 0;
    for (const [keyword, length] of keywordLengths) {
      if (startsWithWord(factoryBody, i, keyword)) {
        keywordLength = length;
        break;
      }
    }
    if (keywordLength === 0) continue;

    let j = skipWhitespaceAndComments(factoryBody, i + keywordLength);
    if (factoryBody[j] !== '{') continue;
    const { expression: destructuring, nextIndex: destructuringEnd } = readDelimitedExpression(
      factoryBody,
      j + 1,
      new Set(['}']),
    );
    j = skipWhitespaceAndComments(factoryBody, destructuringEnd + 1);
    if (factoryBody[j] !== '=') continue;
    j = skipWhitespaceAndComments(factoryBody, j + 1);
    const { expression: rhs } = readDelimitedExpression(factoryBody, j, new Set([';']));
    const importMatch = /^(?:await\s+)?import\s*\(\s*['"]([^'"]+)['"]\s*\)$/.exec(stripWrappingParens(rhs));
    if (!importMatch) continue;

    const importSpecifier = importMatch[1] ?? '';
    if (!importSpecifier.startsWith('.')) continue;
    const resolvedImportPath = path.relative(
      SRC_DIR,
      path.resolve(path.dirname(filePath), importSpecifier),
    ).replaceAll(path.sep, '/');
    if (resolvedImportPath !== helperModule) continue;
    for (const part of destructuring.split(',')) {
      const bindingMatch = /^\s*([A-Za-z_$][\w$]*)(?:\s*:\s*([A-Za-z_$][\w$]*))?(?:\s*=\s*[^,]+)?\s*$/.exec(part);
      if (!bindingMatch) continue;
      bindings.add(bindingMatch[2] ?? bindingMatch[1]);
    }
  }

  return bindings;
}

function isDelegatingSource(
  expression: string,
  aliases: Set<string>,
  helperBindings: Set<string>,
): boolean {
  const body = stripWrappingParens(expression);
  if (!body) return false;
  const helperCallMatch = /^(?:await\s+)?([A-Za-z_$][\w$]*)\s*\(/.exec(body);
  if (helperCallMatch) {
    return helperBindings.has(helperCallMatch[1]);
  }
  if (/^(?:await\s+)?(?:vi\.)?importOriginal(?:<[^>]+>)?\s*\(/.test(body)) return true;
  if (/^(?:await\s+)?(?:vi\.)?importActual(?:<[^>]+>)?\s*\(/.test(body)) return true;
  return /^[A-Za-z_$][\w$]*$/.test(body) && aliases.has(body);
}

function objectLiteralDelegates(body: string, aliases: Set<string>, helperBindings: Set<string>): boolean {
  let depth = 0;
  let inString = false;
  let stringChar = '';
  let inLineComment = false;
  let inBlockComment = false;

  for (let i = 0; i < body.length; i++) {
    const ch = body[i];

    if (inLineComment) {
      if (ch === '\n') inLineComment = false;
      continue;
    }
    if (inBlockComment) {
      if (ch === '*' && body[i + 1] === '/') {
        inBlockComment = false;
        i++;
      }
      continue;
    }
    if (inString) {
      if (ch === '\\') {
        i++;
        continue;
      }
      if (ch === stringChar) inString = false;
      continue;
    }

    if (ch === '/' && body[i + 1] === '/') {
      inLineComment = true;
      i++;
      continue;
    }
    if (ch === '/' && body[i + 1] === '*') {
      inBlockComment = true;
      i++;
      continue;
    }
    if (ch === '"' || ch === "'" || ch === '`') {
      inString = true;
      stringChar = ch;
      continue;
    }

    if (ch === '{') {
      depth++;
      continue;
    }
    if (ch === '}') {
      depth--;
      if (depth <= 0) break;
      continue;
    }

    if (depth !== 1 || ch !== '.' || body[i + 1] !== '.' || body[i + 2] !== '.') continue;
    const { expression, nextIndex } = readDelimitedExpression(body, i + 3, new Set([',', '}']));
    if (isDelegatingSource(expression, aliases, helperBindings)) return true;
    i = nextIndex - 1;
  }

  return false;
}

function hasSafeFactoryDelegation(filePath: string, factoryBody: string, expectedTail: string): boolean {
  const arrowIdx = factoryBody.indexOf('=>');
  if (arrowIdx < 0) return false;
  const helperBindings = collectKnownHelperBindings(filePath, factoryBody, expectedTail);

  const i = skipWhitespaceAndComments(factoryBody, arrowIdx + 2);
  if (factoryBody[i] !== '{') {
    const expression = stripWrappingParens(factoryBody.slice(i));
    return isDelegatingSource(expression, new Set(), helperBindings) || (
      expression.startsWith('{') && objectLiteralDelegates(expression, new Set(), helperBindings)
    );
  }

  const body = factoryBody.slice(i + 1);
  const aliases = new Set<string>();
  let braceDepth = 1;
  let inString = false;
  let stringChar = '';
  let inLineComment = false;
  let inBlockComment = false;

  for (let j = 0; j < body.length; j++) {
    const ch = body[j];

    if (inLineComment) {
      if (ch === '\n') inLineComment = false;
      continue;
    }
    if (inBlockComment) {
      if (ch === '*' && body[j + 1] === '/') {
        inBlockComment = false;
        j++;
      }
      continue;
    }
    if (inString) {
      if (ch === '\\') {
        j++;
        continue;
      }
      if (ch === stringChar) inString = false;
      continue;
    }

    if (ch === '/' && body[j + 1] === '/') {
      inLineComment = true;
      j++;
      continue;
    }
    if (ch === '/' && body[j + 1] === '*') {
      inBlockComment = true;
      j++;
      continue;
    }
    if (ch === '"' || ch === "'" || ch === '`') {
      inString = true;
      stringChar = ch;
      continue;
    }

    if (braceDepth === 1 && startsWithWord(body, j, 'return')) {
      const exprStart = skipWhitespaceAndComments(body, j + 'return'.length);
      const { expression, nextIndex } = readDelimitedExpression(body, exprStart, new Set([';', '}']));
      if (isDelegatingSource(expression, aliases, helperBindings)) return true;
      const unwrapped = stripWrappingParens(expression);
      if (unwrapped.startsWith('{') && objectLiteralDelegates(unwrapped, aliases, helperBindings)) return true;
      j = nextIndex - 1;
      continue;
    }

    if (braceDepth === 1 && (startsWithWord(body, j, 'const') || startsWithWord(body, j, 'let') || startsWithWord(body, j, 'var'))) {
      const { expression: statementText, nextIndex } = readDelimitedExpression(body, j, new Set([';']));
      const declMatch = /^(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*(?:await\s+)?(?:(?:vi\.)?(?:importOriginal|importActual))\b/.exec(statementText.trim());
      if (declMatch) {
        aliases.add(declMatch[1]);
      }
      j = nextIndex - 1;
      continue;
    }

    if (ch === '{') {
      braceDepth++;
      continue;
    }
    if (ch === '}') {
      braceDepth--;
      if (braceDepth <= 0) break;
      continue;
    }
  }

  return false;
}

function findReturnedObjectStart(factoryBody: string): number {
  const returnRe = /\breturn\b/g;
  let match: RegExpExecArray | null;
  while ((match = returnRe.exec(factoryBody)) !== null) {
    let i = skipWhitespaceAndComments(factoryBody, match.index + match[0].length);
    if (factoryBody[i] === '(') {
      i = skipWhitespaceAndComments(factoryBody, i + 1);
    }
    if (factoryBody[i] === '{') return i;
  }

  const arrowIdx = factoryBody.indexOf('=>');
  if (arrowIdx >= 0) {
    let i = skipWhitespaceAndComments(factoryBody, arrowIdx + 2);
    if (factoryBody[i] === '(') {
      i = skipWhitespaceAndComments(factoryBody, i + 1);
    }
    if (factoryBody[i] === '{') return i;
  }

  return -1;
}

/** Advance past a balanced bracket/paren/brace section, respecting strings. */
function findFactoryBodyEnd(text: string): number {
  let depth = 0;
  let inString = false;
  let stringChar = '';
  for (let i = 0; i < text.length; i++) {
    const ch = text[i];
    if (!inString) {
      if (ch === '(' || ch === '{' || ch === '[') { depth++; }
      else if (ch === ')' || ch === '}' || ch === ']') {
        if (depth === 0) return i; // closing paren of vi.mock(...)
        depth--;
      } else if (ch === '"' || ch === "'" || ch === '`') {
        inString = true;
        stringChar = ch;
      } else if (ch === '/' && text[i + 1] === '/') {
        // Single-line comment: skip to newline
        while (i < text.length && text[i] !== '\n') i++;
      } else if (ch === '/' && text[i + 1] === '*') {
        // Block comment: skip to */
        i += 2;
        while (i < text.length - 1 && !(text[i] === '*' && text[i + 1] === '/')) i++;
        i++;
      }
    } else {
      if (ch === '\\') { i++; continue; }
      if (ch === stringChar) inString = false;
    }
  }
  return text.length;
}

/** Extract top-level object property keys from a factory body text. */
function extractTopLevelKeys(factoryBody: string): Set<string> {
  const keys = new Set<string>();
  const objectStart = findReturnedObjectStart(factoryBody);
  if (objectStart < 0) return keys;

  let depth = 0;
  let expectingKey = false;
  let inString = false;
  let stringChar = '';
  let inLineComment = false;
  let inBlockComment = false;

  for (let i = objectStart; i < factoryBody.length; i++) {
    const ch = factoryBody[i];

    if (inLineComment) {
      if (ch === '\n') inLineComment = false;
      continue;
    }
    if (inBlockComment) {
      if (ch === '*' && factoryBody[i + 1] === '/') {
        inBlockComment = false;
        i++;
      }
      continue;
    }
    if (inString) {
      if (ch === '\\') {
        i++;
        continue;
      }
      if (ch === stringChar) inString = false;
      continue;
    }

    if (ch === '/' && factoryBody[i + 1] === '/') {
      inLineComment = true;
      i++;
      continue;
    }
    if (ch === '/' && factoryBody[i + 1] === '*') {
      inBlockComment = true;
      i++;
      continue;
    }
    if (ch === '"' || ch === "'" || ch === '`') {
      inString = true;
      stringChar = ch;
      continue;
    }

    if (ch === '{') {
      depth++;
      if (depth === 1) expectingKey = true;
      continue;
    }
    if (ch === '}') {
      depth--;
      if (depth <= 0) break;
      if (depth === 1) expectingKey = false;
      continue;
    }

    if (depth !== 1) continue;
    if (ch === ',') {
      expectingKey = true;
      continue;
    }
    if (!expectingKey) continue;
    if (!/[A-Za-z_$]/.test(ch)) continue;

    const keyStart = i;
    i++;
    while (i < factoryBody.length && /[A-Za-z0-9_$]/.test(factoryBody[i] ?? '')) i++;
    const key = factoryBody.slice(keyStart, i);
    const next = skipWhitespaceAndComments(factoryBody, i);
    if (factoryBody[next] === ':' || factoryBody[next] === ',' || factoryBody[next] === '}' || factoryBody[next] === '(') {
      keys.add(key);
      i = next - 1;
      expectingKey = factoryBody[next] === ',';
    }
  }
  return keys;
}

function findInlineMockFindings(filePath: string): InlineMockFinding[] {
  let src: string;
  try { src = readFileSync(filePath, 'utf-8'); } catch { return []; }

  const findings: InlineMockFinding[] = [];
  const mockRe = /vi\.mock\(\s*['"]([^'"]+)['"]\s*,/g;
  let m: RegExpExecArray | null;

  while ((m = mockRe.exec(src)) !== null) {
    const moduleSpec = m[1];
    const tail = moduleSpec.split('/').pop() ?? '';
    if (!GUARDED_TAILS.has(tail)) continue;
    if (tail === 'api' && isLegacyApiMock(filePath)) {
      const apiMockCount = countMockCalls(src, '../api');
      if (apiMockCount === 1) continue;
    }

    // Extract the factory argument text (after the comma)
    const afterComma = src.slice(m.index + m[0].length);
    const endIdx = findFactoryBodyEnd(afterComma);
    const factoryBody = afterComma.slice(0, endIdx);

    // Check for safe patterns
    if (hasSafeFactoryDelegation(filePath, factoryBody, tail)) continue;

    const factoryKeys = extractTopLevelKeys(factoryBody);
    findings.push({ file: filePath, moduleSpec, factoryKeys });
  }

  return findings;
}

// ---------------------------------------------------------------------------
// Test
// ---------------------------------------------------------------------------

describe('mock-factory-export-coverage', () => {
  it('all_full_replacement_mock_factories_cover_runtime_exports', async () => {
    const failures: string[] = [];

    // -----------------------------------------------------------------------
    // Tier 1: Helper factory checks via dynamic import
    // -----------------------------------------------------------------------

    // Each entry: [helperModulePath, exportedFunctionName, sourcePath, label]
    const helperChecks: Array<[string, string, string, string]> = [
      ['./api-mock-complete', 'createApiMock', '../api', 'api-mock-complete.ts'],
      ['./firebase-mock-complete', 'createFirebaseMock', '../firebase', 'firebase-mock-complete.ts'],
      ['./poller-mock-complete', 'createPollerMock', '../hooks/usePoller', 'poller-mock-complete.ts'],
      ['./strategic-state-api-mock-complete', 'createStrategicStateApiMock', '../strategicStateApi', 'strategic-state-api-mock-complete.ts'],
    ];

    // Collect source module runtime exports for use in both tier 1 and tier 2.
    // Key = module tail (last path segment), Value = Set of runtime export names.
    const runtimeExportsByTail = new Map<string, Set<string>>();

    for (const [helperPath, fnName, sourcePath, label] of helperChecks) {
      // Load the helper factory
      let factory: ((overrides?: Record<string, unknown>) => Record<string, unknown>) | undefined;
      try {
        const mod = await import(helperPath) as Record<string, unknown>;
        factory = mod[fnName] as typeof factory;
      } catch (e) {
        failures.push(`${label} :: could not import helper: ${String(e)}`);
        continue;
      }

      if (typeof factory !== 'function') {
        failures.push(`${label} :: export "${fnName}" is not a function`);
        continue;
      }

      const factoryOutput = factory();
      const factoryKeys = new Set(Object.keys(factoryOutput));

      // Load the real source module to get its runtime exports
      let sourceExports: Set<string>;
      try {
        const sourceMod = await import(sourcePath) as Record<string, unknown>;
        sourceExports = new Set(Object.keys(sourceMod));
      } catch {
        // Fall back to text-based extraction when the module can't be imported
        const srcFilePath = path.resolve(SRC_DIR, sourcePath.replace(/^\.\.\//, '') + '.ts');
        sourceExports = extractRuntimeExportsFromText(srcFilePath);
      }

      // Cache for tier 2 scanner (keyed by module tail)
      const tail = sourcePath.split('/').pop() ?? '';
      runtimeExportsByTail.set(tail, sourceExports);

      for (const exportName of sourceExports) {
        if (!factoryKeys.has(exportName)) {
          failures.push(
            `${label} :: ${fnName} missing "${exportName}". ` +
            `Add it to admin-ui/src/test/${label} so the factory stays in sync with ${sourcePath}.`,
          );
        }
      }
    }

    // -----------------------------------------------------------------------
    // Tier 2: Inline mock scanner (fs text scan)
    // -----------------------------------------------------------------------

    const testFiles = walkTestFiles(SRC_DIR);

    // Minimum sanity: the walker must find a meaningful number of test files.
    if (testFiles.length < 10) {
      failures.push(
        `Inline mock scanner: test file walker found only ${testFiles.length} files — ` +
        'walker may be broken (expected >10).',
      );
    }

    for (const file of testFiles) {
      const mockFindings = findInlineMockFindings(file);
      for (const finding of mockFindings) {
        const tail = finding.moduleSpec.split('/').pop() ?? '';
        const sourceExports = runtimeExportsByTail.get(tail);
        if (!sourceExports) continue; // Module not loaded — skip

        const missing = [...sourceExports].filter(
          name => !finding.factoryKeys.has(name),
        );
        if (missing.length > 0) {
          const rel = path.relative(SRC_DIR, finding.file);
          failures.push(
            `src/${rel} :: vi.mock("${finding.moduleSpec}") missing: [${missing.join(', ')}]. ` +
            'Either add the missing exports to the inline factory or use the complete helper ' +
            '(e.g. createFirebaseMock / createPollerMock / createStrategicStateApiMock).',
          );
        }
      }
    }

    expect(failures, `\n${failures.join('\n')}`).toHaveLength(0);
  });
});

describe('mock-factory-export-coverage helpers', () => {
  const componentTestFile = path.join(SRC_DIR, 'components/example.test.tsx');
  const rootTestFile = path.join(SRC_DIR, 'strategicStateApi.test.ts');

  it('extractTopLevelKeys ignores nested object members', () => {
    const keys = extractTopLevelKeys(`() => ({
      apiFetch: vi.fn(),
      auth: {
        currentUser: null,
        onAuthStateChanged: vi.fn(),
      },
      __pollerInternals: {
        reset: vi.fn(),
      },
    })`);
    expect(keys.has('apiFetch')).toBe(true);
    expect(keys.has('auth')).toBe(true);
    expect(keys.has('currentUser')).toBe(false);
    expect(keys.has('onAuthStateChanged')).toBe(false);
    expect(keys.has('__pollerInternals')).toBe(true);
    expect(keys.has('reset')).toBe(false);
  });

  it('extractTopLevelKeys does not treat value identifiers as object keys', () => {
    const keys = extractTopLevelKeys(`() => ({
      apiFetch: fetchAlerts,
    })`);
    expect(keys.has('apiFetch')).toBe(true);
    expect(keys.has('fetchAlerts')).toBe(false);
  });

  it('treats helper mentions without delegation as unsafe', () => {
    const body = `async () => {
      const { createApiMock } = await import('../test/api-mock-complete');
      return { apiFetch: vi.fn() };
    }`;
    expect(hasSafeFactoryDelegation(componentTestFile, body, 'api')).toBe(false);
  });

  it('ignores helper-looking bindings hidden in comments and strings', () => {
    const body = `async () => {
      // const { createApiMock } = await import('../test/api-mock-complete');
      const hint = "const { createApiMock } = await import('../test/api-mock-complete')";
      return createApiMock();
    }`;
    expect(hasSafeFactoryDelegation(componentTestFile, body, 'api')).toBe(false);
  });

  it('treats direct helper delegation as safe', () => {
    const body = `async () => {
      const { createApiMock } = await import('../test/api-mock-complete');
      return createApiMock();
    }`;
    expect(hasSafeFactoryDelegation(componentTestFile, body, 'api')).toBe(true);
  });

  it('treats matching helper imports as safe across relative path variants', () => {
    const body = `async () => {
      const { createApiMock } = await import('./test/api-mock-complete');
      return createApiMock();
    }`;
    expect(hasSafeFactoryDelegation(rootTestFile, body, 'api')).toBe(true);
  });

  it('treats helper calls from the wrong module as unsafe', () => {
    const body = `async () => {
      const { createApiMock } = await import('../test/incomplete-api-mock');
      return createApiMock();
    }`;
    expect(hasSafeFactoryDelegation(componentTestFile, body, 'api')).toBe(false);
  });

  it('treats returned original-module spreads as safe', () => {
    const body = `async (importOriginal) => {
      const actual = await importOriginal<typeof import('../api')>();
      return {
        ...actual,
        apiFetch: vi.fn(),
      };
    }`;
    expect(hasSafeFactoryDelegation(componentTestFile, body, 'api')).toBe(true);
  });

  it('does not treat a bare importOriginal call as safe when the return is incomplete', () => {
    const body = `async (importOriginal) => {
      await importOriginal<typeof import('../api')>();
      return { apiFetch: vi.fn() };
    }`;
    expect(hasSafeFactoryDelegation(componentTestFile, body, 'api')).toBe(false);
  });

  it('does not treat a complete helper for a different module as safe', () => {
    const body = `async () => {
      const { createApiMock } = await import('../test/api-mock-complete');
      return createApiMock();
    }`;
    expect(hasSafeFactoryDelegation(componentTestFile, body, 'firebase')).toBe(false);
  });
});
