// Fast is deliberately unprivileged. This is the pre-existing narrow safety
// fence, extracted without broadening its English policy: it rejects direct
// first-person claims of managed-project execution and nothing about Workday
// status prose.
const FAST_DIRECT_EXECUTION_CLAIM = /\b(?:i(?:'ll| will|'m going to| am going to)|let me)\s+(?:(?:now|directly|quickly|just|immediately|first|actually|personally|then|go)\s+|go\s+ahead\s+and\s+)*(?:edit|delete|remove|modify|patch|rewrite|commit|push|merge|deploy|revert|reset|rename|fix|change|update|configure|implement|make|apply|open|run|handle|work|cherry[-\s]?pick|fast[-\s]?forward)(?:\s+and\s+(?:edit|delete|remove|modify|patch|rewrite|commit|push|merge|deploy|revert|reset|rename|fix|change|update|configure|implement|make|apply|open|run|handle|work|cherry[-\s]?pick|fast[-\s]?forward))*\s+/i;
const FAST_FUTURE_PROGRESSIVE_EXECUTION_CLAIM = /\b(?:i(?:'ll| will|'m going to| am going to)|let me)\s+(?:(?:now|directly|quickly|just|immediately|first|actually|personally|then)\s+|go\s+ahead\s+and\s+)*(?:be\s+|take\s+care\s+of\s+)(?:editing|deleting|removing|modifying|patching|rewriting|committing|pushing|merging|deploying|reverting|resetting|renaming|fixing|changing|updating|configuring|implementing|making|applying|opening|running|handling|working\s+on|cherry[-\s]?picking|fast[-\s]?forwarding)\s+/i;
const FAST_ACTIVE_EXECUTION_CLAIM = /\bi(?:'m| am)\s+(?:(?:already|now|directly|quickly|just|immediately|first|actually|personally|then)\s+|going\s+ahead\s+and\s+)*(?:editing|deleting|removing|modifying|patching|rewriting|committing|pushing|merging|deploying|reverting|resetting|renaming|fixing|changing|updating|configuring|implementing|making|applying|opening|running|handling|working|cherry[-\s]?picking|fast[-\s]?forwarding)(?:\s+and\s+(?:editing|deleting|removing|modifying|patching|rewriting|committing|pushing|merging|deploying|reverting|resetting|renaming|fixing|changing|updating|configuring|implementing|making|applying|opening|running|handling|working|cherry[-\s]?picking|fast[-\s]?forwarding))*\s+/i;
const FAST_COMPLETED_EXECUTION_CLAIM = /\bi(?:'ve| have)\s+(?:(?:already|now|directly|quickly|just|immediately|actually|personally|then)\s+)*(?:edited|deleted|removed|modified|patched|rewritten|committed|pushed|merged|deployed|reverted|reset|renamed|fixed|changed|updated|configured|implemented|made|applied|opened|run|handled|worked|cherry[-\s]?picked|fast[-\s]?forwarded)(?:\s+and\s+(?:edited|deleted|removed|modified|patched|rewritten|committed|pushed|merged|deployed|reverted|reset|renamed|fixed|changed|updated|configured|implemented|made|applied|opened|run|handled|worked|cherry[-\s]?picked|fast[-\s]?forwarded))*\s+/i;
const FAST_SIMPLE_PAST_EXECUTION_CLAIM = /\bi\s+(?:(?:just|already|quickly|immediately|actually|personally|then)\s+)*(?:edited|deleted|removed|modified|patched|rewritten|committed|pushed|merged|deployed|reverted|reset|renamed|fixed|changed|updated|configured|implemented|made|applied|opened|ran|handled|worked|cherry[-\s]?picked|fast[-\s]?forwarded)(?:\s+and\s+(?:edited|deleted|removed|modified|patched|rewritten|committed|pushed|merged|deployed|reverted|reset|renamed|fixed|changed|updated|configured|implemented|made|applied|opened|ran|handled|worked|cherry[-\s]?picked|fast[-\s]?forwarded))*\s+/i;

// Repository/deployment verbs are intrinsically managed execution. A bare
// pronoun is therefore enough to make the claim concrete ("I'll deploy it"),
// unlike ambiguous verbs such as "update" or "rewrite", whose pronoun may
// refer to an explanation or recommendation.
const FAST_DIRECT_INTRINSIC_PRONOUN_CLAIM = /\b(?:i(?:'ll| will|'m going to| am going to)|let me)\s+(?:(?:now|directly|quickly|just|immediately|first|actually|personally|then)\s+|go\s+ahead\s+and\s+)*(?:commit|push|merge|deploy|revert|cherry[-\s]?pick|fast[-\s]?forward)(?:\s+and\s+(?:commit|push|merge|deploy|revert|cherry[-\s]?pick|fast[-\s]?forward))*\s+(?:it|this|that)\b/i;
const FAST_ACTIVE_INTRINSIC_PRONOUN_CLAIM = /\bi(?:'m| am)\s+(?:(?:already|now|directly|quickly|just|immediately|first|actually|personally|then)\s+|going\s+ahead\s+and\s+)*(?:committing|pushing|merging|deploying|reverting|cherry[-\s]?picking|fast[-\s]?forwarding)(?:\s+and\s+(?:committing|pushing|merging|deploying|reverting|cherry[-\s]?picking|fast[-\s]?forwarding))*\s+(?:it|this|that)\b/i;
const FAST_COMPLETED_INTRINSIC_PRONOUN_CLAIM = /\bi(?:'ve| have)\s+(?:(?:already|now|directly|quickly|just|immediately|actually|personally|then)\s+)*(?:committed|pushed|merged|deployed|reverted|cherry[-\s]?picked|fast[-\s]?forwarded)(?:\s+and\s+(?:committed|pushed|merged|deployed|reverted|cherry[-\s]?picked|fast[-\s]?forwarded))*\s+(?:it|this|that)\b/i;
const FAST_SIMPLE_PAST_INTRINSIC_PRONOUN_CLAIM = /\bi\s+(?:(?:just|already|quickly|immediately|actually|personally|then)\s+)*(?:committed|pushed|merged|deployed|reverted|cherry[-\s]?picked|fast[-\s]?forwarded)(?:\s+and\s+(?:committed|pushed|merged|deployed|reverted|cherry[-\s]?picked|fast[-\s]?forwarded))*\s+(?:it|this|that)\b/i;
const FAST_OBJECTLESS_EXECUTION_END = '(?=\\s*(?:(?:now|shortly|soon|already|just|immediately|today|tonight)\\b\\s*)?(?:[.!?,;:]|$))';
const FAST_DIRECT_OBJECTLESS_INTRINSIC_CLAIM = new RegExp(`\\b(?:i(?:'ll| will|'m going to| am going to)|let me)\\s+(?:(?:now|directly|quickly|just|immediately|first|actually|personally|then|go)\\s+|go\\s+ahead\\s+and\\s+)*(?:commit|push|merge|deploy|revert|cherry[-\\s]?pick|fast[-\\s]?forward)(?:\\s+and\\s+(?:commit|push|merge|deploy|revert|cherry[-\\s]?pick|fast[-\\s]?forward))*${FAST_OBJECTLESS_EXECUTION_END}`, 'i');
const FAST_ACTIVE_OBJECTLESS_INTRINSIC_CLAIM = new RegExp(`\\bi(?:'m| am)\\s+(?:(?:already|now|directly|quickly|just|immediately|first|actually|personally|then)\\s+|going\\s+ahead\\s+and\\s+)*(?:committing|pushing|merging|deploying|reverting|cherry[-\\s]?picking|fast[-\\s]?forwarding)(?:\\s+and\\s+(?:committing|pushing|merging|deploying|reverting|cherry[-\\s]?picking|fast[-\\s]?forwarding))*${FAST_OBJECTLESS_EXECUTION_END}`, 'i');
const FAST_COMPLETED_OBJECTLESS_INTRINSIC_CLAIM = new RegExp(`\\bi(?:'ve| have)\\s+(?:(?:already|now|directly|quickly|just|immediately|actually|personally|then)\\s+)*(?:committed|pushed|merged|deployed|reverted|cherry[-\\s]?picked|fast[-\\s]?forwarded)(?:\\s+and\\s+(?:committed|pushed|merged|deployed|reverted|cherry[-\\s]?picked|fast[-\\s]?forwarded))*${FAST_OBJECTLESS_EXECUTION_END}`, 'i');
const FAST_SIMPLE_PAST_OBJECTLESS_INTRINSIC_CLAIM = new RegExp(`\\bi\\s+(?:(?:just|already|quickly|immediately|actually|personally|then)\\s+)*(?:committed|pushed|merged|deployed|reverted|cherry[-\\s]?picked|fast[-\\s]?forwarded)(?:\\s+and\\s+(?:committed|pushed|merged|deployed|reverted|cherry[-\\s]?picked|fast[-\\s]?forwarded))*${FAST_OBJECTLESS_EXECUTION_END}`, 'i');

// Managed execution needs a concrete object or destination. This catches direct,
// prepositional, pronoun-bound, and compound claims while leaving strategist
// prose such as "update you about the code" and "reset expectations around the
// deployment" alone.
const FAST_MANAGED_TARGET = '(?:(?:the|a|an|this|that|your|our)\\s+)?(?:(?:current|existing|requested|production|project|service|database|repository|repo|deployment|default|main)\\s+){0,2}(?:code|codebase|configuration|config|setting|knob|entr(?:y|ies)|file|branch|commit|change|changes|patch|pull request|pr|deployment|service|database|schema|migration|repository|repo|production|main|default\\s+branch|[a-z0-9_.-]+\\.(?:ts|tsx|js|jsx|mjs|cjs|py|json|ya?ml|toml|sql|sh|ps1|md))\\b(?!\\s+(?:recommendation|recommendations|plan|plans|example|examples|sample|samples|snippet|snippets|discussion|analysis|explanation|summary|options?|status|expectations?|proposal|direction)\\b)';
const FAST_MANAGED_EXECUTION_COMPLEMENT = new RegExp(
  `^(?:${FAST_MANAGED_TARGET}|(?:(?:it|this|that)\\s+)?(?:to|into|onto|from|in|on|against|through)\\s+${FAST_MANAGED_TARGET})`,
  'i',
);

export function claimsFastManagedExecution(content: string): boolean {
  const normalized = content.replace(/\u2019/g, "'");
  if ([
    FAST_DIRECT_INTRINSIC_PRONOUN_CLAIM,
    FAST_ACTIVE_INTRINSIC_PRONOUN_CLAIM,
    FAST_COMPLETED_INTRINSIC_PRONOUN_CLAIM,
    FAST_SIMPLE_PAST_INTRINSIC_PRONOUN_CLAIM,
    FAST_DIRECT_OBJECTLESS_INTRINSIC_CLAIM,
    FAST_ACTIVE_OBJECTLESS_INTRINSIC_CLAIM,
    FAST_COMPLETED_OBJECTLESS_INTRINSIC_CLAIM,
    FAST_SIMPLE_PAST_OBJECTLESS_INTRINSIC_CLAIM,
  ].some(pattern => pattern.test(normalized))) {
    return true;
  }
  for (const pattern of [
    FAST_DIRECT_EXECUTION_CLAIM,
    FAST_FUTURE_PROGRESSIVE_EXECUTION_CLAIM,
    FAST_ACTIVE_EXECUTION_CLAIM,
    FAST_COMPLETED_EXECUTION_CLAIM,
    FAST_SIMPLE_PAST_EXECUTION_CLAIM,
  ]) {
    const scanner = new RegExp(pattern.source, `${pattern.flags}g`);
    for (let match = scanner.exec(normalized); match; match = scanner.exec(normalized)) {
      if (FAST_MANAGED_EXECUTION_COMPLEMENT.test(
        normalized.slice(match.index + match[0].length),
      )) {
        return true;
      }
    }
  }
  return false;
}
