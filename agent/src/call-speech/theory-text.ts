const GREEK: Readonly<Record<string, string>> = {
  alpha: 'α',
  beta: 'β',
  beeta: 'β',
  gamma: 'γ',
  delta: 'δ',
  epsilon: 'ε',
  varepsilon: 'ε',
  zeta: 'ζ',
  eta: 'η',
  theta: 'θ',
  iota: 'ι',
  kappa: 'κ',
  lambda: 'λ',
  mu: 'μ',
  nu: 'ν',
  xi: 'ξ',
  omicron: 'ο',
  pi: 'π',
  rho: 'ρ',
  sigma: 'σ',
  tau: 'τ',
  upsilon: 'υ',
  phi: 'φ',
  chi: 'χ',
  psi: 'ψ',
  omega: 'ω',
};

// Common English words/acronyms must never become Greek merely because a call
// happens to be theoretical. They can be suggested only in clear math context.
const AMBIGUOUS_GREEK = new Set(['eta', 'pi', 'mu', 'nu']);

const THEORY_TERMS = [
  'NeurIPS',
  'KL divergence',
  'ELBO',
  'Bayes',
  'Bayesian',
  'posterior',
  'likelihood',
  'gradient',
  'Hessian',
  'eigenvalue',
  'eigenvector',
  'softmax',
  'argmax',
  'L2 norm',
  'L1 norm',
  ...Object.keys(GREEK),
] as const;

function escaped(value: string): string {
  return value.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
}

/**
 * Narrow post-processing for captions and durable call messages. It only fixes
 * unambiguous spoken mathematical notation; the raw provider transcript should
 * remain available to the coordinator if a user needs to inspect it.
 */
export function normalizeTheoryTranscript(input: string): string {
  let output = input.replace(/\s+/g, ' ').trim();
  output = output.replace(/\b(k\s*[- ]?\s*l)\s+divergence\b/gi, 'KL divergence');
  output = output.replace(/\b(l|ell)\s*([12])\s+norm\b/gi, (_match, _ell, order: string) => `L${order} norm`);
  output = output.replace(/\b([a-zA-Z])\s+sub(?:script)?\s+([a-zA-Z0-9]+)\b/g, '$1_$2');
  output = output.replace(/\b([a-zA-Z])\s+(?:superscript|to the)\s+(two|2)\b/gi, '$1²');
  for (const [spoken, glyph] of Object.entries(GREEK)) {
    if (AMBIGUOUS_GREEK.has(spoken)) {
      const mathContext = new RegExp(`\\b${escaped(spoken)}\\b(?=\\s+(?:parameter|variable|term|distribution|equals|is))`, 'gi');
      output = output.replace(mathContext, glyph);
      continue;
    }
    output = output.replace(new RegExp(`\\b${escaped(spoken)}\\b`, 'gi'), glyph);
  }
  return output;
}

export function theoryCorrectionCandidate(rawTranscript: string):
  | { text: string; reason: 'theory_notation'; requiresConfirmation: true }
  | undefined {
  const text = normalizeTheoryTranscript(rawTranscript);
  return text !== rawTranscript ? { text, reason: 'theory_notation', requiresConfirmation: true } : undefined;
}

/** The coordinator calls this only after the user accepts the displayed suggestion. */
export function applyConfirmedTheoryCorrection(rawTranscript: string, acceptedText: string): string {
  const candidate = theoryCorrectionCandidate(rawTranscript);
  if (!candidate || candidate.text !== acceptedText) {
    throw new Error('Rejected theory correction does not match the current transcript candidate.');
  }
  return candidate.text;
}

/** Flux accepts at most 100 plain keyterms.  Keep a deterministic, bounded list. */
export function buildTheoryKeyterms(extra: readonly string[] = []): string[] {
  const candidates = [...THEORY_TERMS, ...extra];
  const seen = new Set<string>();
  const result: string[] = [];
  for (const candidate of candidates) {
    const term = candidate.trim().replace(/\s+/g, ' ');
    if (!term || term.length > 96 || seen.has(term.toLocaleLowerCase())) continue;
    seen.add(term.toLocaleLowerCase());
    result.push(term);
    if (result.length === 100) break;
  }
  return result;
}

export function splitSpeakablePhrases(text: string, maxCharacters = 240): string[] {
  const compact = text.replace(/\s+/g, ' ').trim();
  if (!compact) return [];
  if (!Number.isInteger(maxCharacters) || maxCharacters < 32 || maxCharacters > 1_000) {
    throw new Error('maxCharacters must be an integer between 32 and 1000.');
  }
  const sentences = compact.match(/[^.!?;:]+[.!?;:]*(?:\s+|$)/g) ?? [compact];
  const phrases: string[] = [];
  for (const sentence of sentences) {
    let remainder = sentence.trim();
    while (remainder.length > maxCharacters) {
      const boundary = remainder.lastIndexOf(' ', maxCharacters);
      const cut = boundary >= Math.floor(maxCharacters / 2) ? boundary : maxCharacters;
      phrases.push(remainder.slice(0, cut).trim());
      remainder = remainder.slice(cut).trim();
    }
    if (remainder) phrases.push(remainder);
  }
  return phrases;
}
