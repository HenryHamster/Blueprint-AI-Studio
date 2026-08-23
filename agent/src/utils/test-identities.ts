export interface TestDeclaration {
  identifier: string | null;
  isDeclaration: boolean;
}

const PYTHON_TEST_DECLARATION_RE = /^(?:async\s+)?def\s+(test_\w+)\s*\(/;
// Keep these quote alternatives in lockstep with backend/testing/
// identity_extraction.py. A backreference-based delimiter matcher looks
// equivalent, but makes the cross-language contract needlessly implicit and
// is easy to diverge when multiline labels are changed.
const TS_DIRECT_TEST_DECLARATION_RE = /^\s*(?:it|test)\s*\(\s*(?:'([^']+)'|"([^"]+)"|`([^`]+)`)/gm;
const TS_EACH_TEST_DECLARATION_RE = /^\s*(?:it|test)\.each\b[\s\S]*?\)\s*\(\s*(?:'([^']+)'|"([^"]+)"|`([^`]+)`)/gm;
const TS_EACH_START_RE = /^\s*(?:test|it)\.each\b/gm;

function firstTypeScriptLabel(match: RegExpMatchArray): string | null {
  return match.slice(1).find((group): group is string => typeof group === 'string') ?? null;
}

/**
 * Identify TypeScript test declarations in source or diff lines.
 *
 * Direct and parameterized Vitest/Jest declarations support all JS quote
 * styles. The parameterized matcher uses the same multiline declaration shape
 * as the Python identity extractor used by Workday and viewport audits.
 */
export function extractTypeScriptTestDeclarations(source: string): TestDeclaration[] {
  const normalized = source.replace(/^[+-]\s*/gm, '');
  const declarations: Array<TestDeclaration & { offset: number }> = [];

  for (const match of normalized.matchAll(TS_DIRECT_TEST_DECLARATION_RE)) {
    declarations.push({ identifier: firstTypeScriptLabel(match), isDeclaration: true, offset: match.index });
  }

  const parameterizedStarts = [...normalized.matchAll(TS_EACH_START_RE)];
  for (const [index, match] of parameterizedStarts.entries()) {
    // Bound label extraction to this declaration. Without this slice, a
    // malformed or unlabeled .each can borrow the label of a later .each
    // because the canonical matcher is intentionally allowed to span lines.
    const start = match.index;
    const end = parameterizedStarts[index + 1]?.index ?? normalized.length;
    const declaration = normalized.slice(start, end).matchAll(TS_EACH_TEST_DECLARATION_RE).next().value as RegExpMatchArray | undefined;
    declarations.push({
      identifier: declaration ? firstTypeScriptLabel(declaration) : null,
      isDeclaration: true,
      offset: start,
    });
  }

  return declarations
    .sort((left, right) => left.offset - right.offset)
    .map(({ identifier, isDeclaration }) => ({ identifier, isDeclaration }));
}

/** Identify one Python or Vitest/Jest declaration from a complete source chunk. */
export function extractTestDeclaration(source: string): TestDeclaration {
  const python = extractPythonTestDeclaration(source);
  if (python.isDeclaration) return python;

  return extractTypeScriptTestDeclarations(source)[0] ?? { identifier: null, isDeclaration: false };
}

/** Identify a Python test declaration in a single source or diff line. */
export function extractPythonTestDeclaration(line: string): TestDeclaration {
  const source = line.replace(/^[+-]\s*/, '');
  const python = source.match(PYTHON_TEST_DECLARATION_RE);
  return python
    ? { identifier: python[1], isDeclaration: true }
    : { identifier: null, isDeclaration: false };
}
