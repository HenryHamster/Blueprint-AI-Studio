import type { SpeechProviderName } from './types.js';

type RequiredSpeechEnvironment =
  | 'CALL_DEEPGRAM_API_KEY'
  | 'CALL_DEEPGRAM_STT_MODEL'
  | 'CALL_CARTESIA_API_KEY'
  | 'CALL_CARTESIA_TTS_MODEL'
  | 'CALL_CARTESIA_VOICE_ID';

const CALL_SPEECH_ENVIRONMENT_KEYS = new Set([
  'CALL_DEEPGRAM_API_KEY',
  'CALL_DEEPGRAM_STT_MODEL',
  'CALL_DEEPGRAM_STT_ENDPOINT',
  'CALL_DEEPGRAM_SAMPLE_RATE_HZ',
  'CALL_DEEPGRAM_EOT_THRESHOLD',
  'CALL_DEEPGRAM_EAGER_EOT_THRESHOLD',
  'CALL_DEEPGRAM_EOT_TIMEOUT_MS',
  'CALL_CARTESIA_API_KEY',
  'CALL_CARTESIA_TTS_MODEL',
  'CALL_CARTESIA_VOICE_ID',
  'CALL_CARTESIA_TTS_ENDPOINT',
  'CALL_CARTESIA_API_VERSION',
  'CALL_CARTESIA_SAMPLE_RATE_HZ',
  'CALL_SPEECH_CONNECT_TIMEOUT_MS',
]);

export class CallSpeechConfigurationError extends Error {
  constructor(message: string) {
    super(message);
    this.name = 'CallSpeechConfigurationError';
  }
}

export interface CallSpeechConfig {
  readonly deepgram: {
    apiKey: string;
    model: string;
    endpoint: string;
    sampleRateHz: number;
    eotThreshold: number;
    eagerEotThreshold: number;
    eotTimeoutMs: number;
    connectTimeoutMs: number;
  };
  readonly cartesia: {
    apiKey: string;
    model: string;
    voiceId: string;
    endpoint: string;
    apiVersion: string;
    sampleRateHz: number;
    connectTimeoutMs: number;
  };
}

function required(env: NodeJS.ProcessEnv, key: RequiredSpeechEnvironment): string {
  const value = env[key]?.trim();
  if (!value) {
    throw new CallSpeechConfigurationError(`Call speech is disabled: missing ${key}.`);
  }
  return value;
}

function parseBoundedInt(
  env: NodeJS.ProcessEnv,
  key: string,
  fallback: number,
  min: number,
  max: number,
): number {
  const raw = env[key]?.trim();
  if (!raw) return fallback;
  const value = Number(raw);
  if (!Number.isInteger(value) || value < min || value > max) {
    throw new CallSpeechConfigurationError(`${key} must be an integer between ${min} and ${max}.`);
  }
  return value;
}

function parseBoundedFloat(
  env: NodeJS.ProcessEnv,
  key: string,
  fallback: number,
  min: number,
  max: number,
): number {
  const raw = env[key]?.trim();
  if (!raw) return fallback;
  const value = Number(raw);
  if (!Number.isFinite(value) || value < min || value > max) {
    throw new CallSpeechConfigurationError(`${key} must be a number between ${min} and ${max}.`);
  }
  return value;
}

function safeWebsocketEndpoint(value: string, key: string): string {
  let url: URL;
  try {
    url = new URL(value);
  } catch {
    throw new CallSpeechConfigurationError(`${key} must be a valid wss:// endpoint.`);
  }
  if (url.protocol !== 'wss:') {
    throw new CallSpeechConfigurationError(`${key} must use wss://; speech credentials are never sent over cleartext WebSocket.`);
  }
  return url.toString();
}

/**
 * This parser is deliberately strict: a voice call cannot silently fall back
 * to a browser key, an unconfigured model, or a generic public service.
 */
export function loadCallSpeechConfig(env: NodeJS.ProcessEnv = process.env): CallSpeechConfig {
  const eotThreshold = parseBoundedFloat(env, 'CALL_DEEPGRAM_EOT_THRESHOLD', 0.7, 0.5, 0.9);
  const eagerEotThreshold = parseBoundedFloat(
    env,
    'CALL_DEEPGRAM_EAGER_EOT_THRESHOLD',
    0.5,
    0.3,
    eotThreshold,
  );
  return {
    deepgram: {
      apiKey: required(env, 'CALL_DEEPGRAM_API_KEY'),
      model: required(env, 'CALL_DEEPGRAM_STT_MODEL'),
      endpoint: safeWebsocketEndpoint(
        env.CALL_DEEPGRAM_STT_ENDPOINT ?? 'wss://api.deepgram.com/v2/listen',
        'CALL_DEEPGRAM_STT_ENDPOINT',
      ),
      sampleRateHz: parseBoundedInt(env, 'CALL_DEEPGRAM_SAMPLE_RATE_HZ', 16000, 8000, 48000),
      eotThreshold,
      eagerEotThreshold,
      eotTimeoutMs: parseBoundedInt(env, 'CALL_DEEPGRAM_EOT_TIMEOUT_MS', 900, 500, 60_000),
      connectTimeoutMs: parseBoundedInt(env, 'CALL_SPEECH_CONNECT_TIMEOUT_MS', 8_000, 500, 30_000),
    },
    cartesia: {
      apiKey: required(env, 'CALL_CARTESIA_API_KEY'),
      model: required(env, 'CALL_CARTESIA_TTS_MODEL'),
      voiceId: required(env, 'CALL_CARTESIA_VOICE_ID'),
      endpoint: safeWebsocketEndpoint(
        env.CALL_CARTESIA_TTS_ENDPOINT ?? 'wss://api.cartesia.ai/tts/websocket',
        'CALL_CARTESIA_TTS_ENDPOINT',
      ),
      apiVersion: env.CALL_CARTESIA_API_VERSION?.trim() || '2026-03-01',
      sampleRateHz: parseBoundedInt(env, 'CALL_CARTESIA_SAMPLE_RATE_HZ', 16000, 8000, 48000),
      connectTimeoutMs: parseBoundedInt(env, 'CALL_SPEECH_CONNECT_TIMEOUT_MS', 8_000, 500, 30_000),
    },
  };
}

/** Parse the backend's finite, call-scoped envelope without merging it into
 * process.env or any model subprocess environment. */
export function parseCallSpeechCredentialEnvelope(value: unknown): NodeJS.ProcessEnv {
  if (!value || typeof value !== 'object' || Array.isArray(value)) {
    throw new CallSpeechConfigurationError('Call speech credential response is invalid.');
  }
  const speech = (value as Record<string, unknown>).speech;
  if (!speech || typeof speech !== 'object' || Array.isArray(speech)) {
    throw new CallSpeechConfigurationError('Call speech credential response is invalid.');
  }
  const result: NodeJS.ProcessEnv = {};
  for (const [key, raw] of Object.entries(speech as Record<string, unknown>)) {
    if (!CALL_SPEECH_ENVIRONMENT_KEYS.has(key) || typeof raw !== 'string') {
      throw new CallSpeechConfigurationError('Call speech credential response contains an unsupported field.');
    }
    result[key] = raw;
  }
  loadCallSpeechConfig(result);
  return result;
}

/** Only the deterministic Call Coordinator may inherit these variables. */
export function coordinatorSpeechEnvironment(env: NodeJS.ProcessEnv = process.env): Record<string, string> {
  // Load first so an incomplete secret set cannot be forwarded accidentally.
  loadCallSpeechConfig(env);
  const result: Record<string, string> = {};
  for (const [key, value] of Object.entries(env)) {
    if (value !== undefined && (key.startsWith('CALL_DEEPGRAM_') || key.startsWith('CALL_CARTESIA_'))) {
      result[key] = value;
    }
  }
  return result;
}

/** Explicit deny-list helper for the Fast Resident child-process environment. */
export function withoutCallSpeechSecrets(env: NodeJS.ProcessEnv = process.env): Record<string, string> {
  return Object.fromEntries(
    Object.entries(env).filter(([key, value]) => value !== undefined && !key.startsWith('CALL_DEEPGRAM_') && !key.startsWith('CALL_CARTESIA_')),
  ) as Record<string, string>;
}

export function redactProviderError(provider: SpeechProviderName, error: unknown): string {
  const raw = error instanceof Error ? error.message : String(error);
  // Provider error bodies and URLs can contain headers/query credentials.  Keep
  // a bounded, single-line diagnostic that is safe for task logs.
  const normalized = raw
    .replace(/(authorization)\s*[:=]\s*(?:(?:bearer|token)\s+)?[^\s,;]+/gi, '$1=[redacted]')
    .replace(/(api[_ -]?key|token)\s*[:=]\s*[^\s,;]+/gi, '$1=[redacted]')
    .replace(/([?&](?:api[_-]?key|token|access_token)=)[^&\s]+/gi, '$1[redacted]')
    .replace(/[\r\n]+/g, ' ')
    .slice(0, 240);
  return `${provider}: ${normalized || 'unknown provider error'}`;
}
