// DeepSeek's documented Claude Code integration
// (api-docs.deepseek.com /quick_start/agent_integrations/claude_code) selects
// models through environment variables. Without them the CLI resolves its
// built-in claude-* aliases, which DeepSeek's compat layer silently remaps
// (claude-opus* → deepseek-v4-pro, claude-sonnet*/haiku* → deepseek-v4-flash,
// anything unrecognized → deepseek-v4-flash) — so an intended Pro session can
// silently downgrade to Flash. These are the documented plain ids: there is no
// "[1m]" long-context variant (both models are natively 1M-context, and an
// unknown id would itself trigger the silent Flash fallback). An explicit
// --model flag still overrides ANTHROPIC_MODEL, so per-task selection wins.
export const DEEPSEEK_MODEL_ENV_DEFAULTS: Readonly<Record<string, string>> = {
  ANTHROPIC_MODEL: 'deepseek-v4-pro',
  ANTHROPIC_DEFAULT_OPUS_MODEL: 'deepseek-v4-pro',
  ANTHROPIC_DEFAULT_SONNET_MODEL: 'deepseek-v4-pro',
  ANTHROPIC_DEFAULT_HAIKU_MODEL: 'deepseek-v4-flash',
  CLAUDE_CODE_SUBAGENT_MODEL: 'deepseek-v4-flash',
} as const;

/**
 * Fill the documented DeepSeek model-selection env into a CLI env under
 * construction. Scoped to the deepseek branch of buildBaseCliEnv on purpose:
 * other providers never see these keys, so a warm VM retargeted
 * deepseek→anthropic cannot leak a DeepSeek model id into an Anthropic run.
 * Precedence mirrors the rest of the env builder: process.env (account
 * materialization) > LLM_CLI_TRANSLATION.env (already merged into `env` by
 * the caller) > documented default.
 */
export function applyDeepSeekModelEnv(env: Record<string, string>): void {
  for (const [key, value] of Object.entries(DEEPSEEK_MODEL_ENV_DEFAULTS)) {
    const provided = process.env[key];
    if (provided) {
      env[key] = provided;
    } else if (!env[key]) {
      env[key] = value;
    }
  }
}

/**
 * Pin ANTHROPIC_MODEL to the explicitly requested model for a DeepSeek CLI
 * spawn (run or validation), so env-driven paths — the stream-json
 * session, subagent spawns, CLI internals that consult the env before the
 * flag — resolve the SAME model the --model flag names. Non-DeepSeek
 * providers (isDeepSeek=false) get the env back untouched.
 */
export function pinDeepSeekModel(
  env: Record<string, string>,
  isDeepSeek: boolean,
  model: string | undefined,
): Record<string, string> {
  return isDeepSeek && model ? { ...env, ANTHROPIC_MODEL: model } : env;
}
