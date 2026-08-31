#!/usr/bin/env node
// Closed stdio MCP surface for the shell-free Codex Resident. Replies are
// staged for the handler's terminal commit. Strategic reads and mutations run
// only fixed generated helpers with validated arguments and a cleared env.

import { spawn } from 'node:child_process';
import { Buffer } from 'node:buffer';
import { createHash } from 'node:crypto';
import fs from 'node:fs';
import path from 'node:path';
import process from 'node:process';
import readline from 'node:readline';
import { clearTimeout, setTimeout } from 'node:timers';

const MAX_REPLY_BYTES = 65_536;
const MAX_STRATEGIC_INPUT_BYTES = 100 * 1024;
const MAX_HELPER_OUTPUT_BYTES = 512 * 1024;
// The MCP helper gets two transport attempts (<=70s worst case below), leaving
// a deterministic margin inside the Resident's minimum 120s stall deadline.
const HELPER_TIMEOUT_MS = 90_000;
const REPLY_TOOL = 'reply_to_operator';
const STRATEGIC_TOOL = 'strategic_operation';
const PROJECT_READ_TOOL = 'project_read';
const receiptPath = process.env.RESIDENT_REPLY_RECEIPT_PATH || '';
const stagedReplyPath = process.env.RESIDENT_STAGED_REPLY_PATH || '';
const strategicHelperPath = process.env.RESIDENT_STRATEGIC_HELPER_PATH || '';
const strategicOpsPath = process.env.RESIDENT_STRATEGIC_OPS_PATH || '';
const projectReadHelperPath = process.env.RESIDENT_PROJECT_READ_HELPER_PATH || '';
const projectReadsPath = process.env.RESIDENT_PROJECT_READS_PATH || '';
let cachedStrategicOps;
let cachedProjectReads;
const activeHelperChildren = new Set();

function signalActiveHelperGroups(signal) {
  for (const child of activeHelperChildren) {
    if (!child.pid) continue;
    try {
      process.kill(-child.pid, signal);
    } catch {
      try { child.kill(signal); } catch { /* close/error owns final cleanup */ }
    }
  }
}

for (const signal of ['SIGINT', 'SIGTERM']) {
  process.on(signal, () => {
    signalActiveHelperGroups('SIGTERM');
    signalActiveHelperGroups('SIGKILL');
    setTimeout(() => process.exit(signal === 'SIGINT' ? 130 : 143), 250).unref();
  });
}

function send(message) {
  process.stdout.write(`${JSON.stringify(message)}\n`);
}

function toolError(id, text, structuredContent) {
  send({
    jsonrpc: '2.0',
    id,
    result: {
      content: [{ type: 'text', text }],
      isError: true,
      ...(structuredContent ? { structuredContent } : {}),
    },
  });
}

function readRegular(pathname, maxBytes, label) {
  if (!pathname || !path.isAbsolute(pathname)) throw new Error(`${label} path is not configured`);
  const before = fs.lstatSync(pathname);
  if (!before.isFile()) throw new Error(`${label} is not a regular file`);
  const noFollow = fs.constants.O_NOFOLLOW || 0;
  const fd = fs.openSync(pathname, fs.constants.O_RDONLY | noFollow);
  try {
    const opened = fs.fstatSync(fd);
    if (!opened.isFile() || opened.dev !== before.dev || opened.ino !== before.ino) {
      throw new Error(`${label} changed identity during open`);
    }
    if (opened.size > maxBytes) throw new Error(`${label} exceeds its size limit`);
    const raw = fs.readFileSync(fd, 'utf8');
    const after = fs.fstatSync(fd);
    if (after.size !== opened.size) throw new Error(`${label} changed during read`);
    return raw;
  } finally {
    fs.closeSync(fd);
  }
}

function readRegularJson(pathname, maxBytes, label) {
  return JSON.parse(readRegular(pathname, maxBytes, label));
}

function strategicOps() {
  if (cachedStrategicOps !== undefined) return cachedStrategicOps;
  if (!strategicHelperPath && !strategicOpsPath) return null;
  const parsed = readRegularJson(strategicOpsPath, 256 * 1024, 'strategic operation contract');
  if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) {
    throw new Error('strategic operation contract is invalid');
  }
  cachedStrategicOps = parsed;
  return cachedStrategicOps;
}

function projectReads() {
  if (cachedProjectReads !== undefined) return cachedProjectReads;
  if (!projectReadHelperPath && !projectReadsPath) return null;
  const parsed = readRegularJson(projectReadsPath, 32 * 1024, 'project read contract');
  if (!Array.isArray(parsed) || parsed.some((item) => typeof item !== 'string' || !item)) {
    throw new Error('project read contract is invalid');
  }
  cachedProjectReads = new Set(parsed);
  return cachedProjectReads;
}

function activeReceipt() {
  if (!receiptPath || !fs.existsSync(receiptPath)) return null;
  const receipt = readRegularJson(receiptPath, 16_384, 'resident reply receipt');
  if (
    !receipt || typeof receipt !== 'object' || Array.isArray(receipt)
    || typeof receipt.idempotency_key !== 'string' || !receipt.idempotency_key
    || !Array.isArray(receipt.covered_message_ids) || receipt.covered_message_ids.length === 0
    || receipt.covered_message_ids.some((id) => typeof id !== 'string' || !id)
  ) {
    throw new Error('resident reply receipt is invalid');
  }
  return receipt;
}

function stageReply(reply) {
  const tempPath = `${stagedReplyPath}.tmp.${process.pid}.${Date.now()}`;
  try {
    fs.writeFileSync(tempPath, JSON.stringify(reply), { encoding: 'utf8', flag: 'wx', mode: 0o600 });
    fs.linkSync(tempPath, stagedReplyPath);
  } finally {
    try { fs.unlinkSync(tempPath); } catch { /* best-effort temporary cleanup */ }
  }
}

function replyToolDefinition() {
  return {
    name: REPLY_TOOL,
    description: 'Send the complete response to the active Workday operator message. This is the only action that delivers a Resident chat reply.',
    inputSchema: {
      type: 'object',
      properties: {
        content: { type: 'string', description: 'Complete user-facing reply for the operator.' },
        message_type: {
          type: 'string',
          enum: ['NOTE', 'QUESTION', 'FEEDBACK', 'BUG_REPORT', 'BLOCKER'],
          description: 'Optional Workday message type. Defaults to NOTE.',
        },
      },
      required: ['content'],
      additionalProperties: false,
    },
  };
}

function strategicToolDefinition(ops) {
  const contracts = Object.fromEntries(Object.entries(ops).map(([operation, contract]) => [
    operation,
    {
      required: Array.isArray(contract.required) ? contract.required : [],
      fields: Array.isArray(contract.fields) ? contract.fields : [],
      requirements: Array.isArray(contract.requirements) ? contract.requirements : [],
      example: contract.example && typeof contract.example === 'object' && !Array.isArray(contract.example)
        ? contract.example
        : {},
    },
  ]));
  return {
    name: STRATEGIC_TOOL,
    description: 'Apply one exact Blueprint strategic operation. Use a stable idempotency_key and retry only byte-identical ambiguous calls.',
    inputSchema: {
      type: 'object',
      properties: {
        operation: { type: 'string', enum: Object.keys(ops) },
        data: {
          type: 'object',
          description: `Canonical JSON discovery contracts by operation. required lists unconditional required top-level fields; fields lists MCP-supported top-level fields; requirements lists conditional or nested rules; example is a valid data object. contract-json=${JSON.stringify(contracts)}`,
        },
        idempotency_key: { type: 'string', minLength: 1, maxLength: 128 },
      },
      required: ['operation', 'data', 'idempotency_key'],
      additionalProperties: false,
    },
  };
}

function projectReadToolDefinition(reads) {
  return {
    name: PROJECT_READ_TOOL,
    description: 'Read one bounded canonical Blueprint project resource for preconditions and post-mutation verification.',
    inputSchema: {
      type: 'object',
      properties: {
        resource: { type: 'string', enum: [...reads] },
        id: { type: 'string', minLength: 1, maxLength: 200 },
      },
      required: ['resource'],
      additionalProperties: false,
    },
  };
}

function toolDefinitions() {
  const tools = [replyToolDefinition()];
  const ops = strategicOps();
  const reads = projectReads();
  if (ops) tools.push(strategicToolDefinition(ops));
  if (reads) tools.push(projectReadToolDefinition(reads));
  return tools;
}

function handleReply(id, args) {
  if (!args || typeof args !== 'object' || Array.isArray(args)) {
    return toolError(id, 'reply_to_operator requires an arguments object');
  }
  if (Object.keys(args).some((key) => !['content', 'message_type'].includes(key))) {
    return toolError(id, 'reply_to_operator received an unknown argument');
  }
  const content = typeof args.content === 'string' ? args.content.trim() : '';
  if (!content) return toolError(id, 'reply_to_operator requires non-empty content');
  if (Buffer.byteLength(content, 'utf8') > MAX_REPLY_BYTES) {
    return toolError(id, 'reply_to_operator content exceeds 64KiB');
  }
  const type = args.message_type === undefined ? 'NOTE' : args.message_type;
  if (!['NOTE', 'QUESTION', 'FEEDBACK', 'BUG_REPORT', 'BLOCKER'].includes(type)) {
    return toolError(id, 'reply_to_operator message_type is invalid');
  }
  try {
    const receipt = activeReceipt();
    if (receipt === null) {
      return send({
        jsonrpc: '2.0', id,
        result: {
          content: [{ type: 'text', text: 'No active operator turn is armed. Nothing was delivered; wait for an operator message before using reply_to_operator.' }],
          structuredContent: { accepted: false },
        },
      });
    }
    stageReply({
      type,
      content,
      idempotency_key: receipt.idempotency_key,
      covered_message_ids: receipt.covered_message_ids,
    });
    send({
      jsonrpc: '2.0', id,
      result: {
        content: [{ type: 'text', text: 'Workday reply accepted for delivery.' }],
        structuredContent: { accepted: true },
      },
    });
  } catch (error) {
    toolError(id, `reply_to_operator could not accept this reply: ${String(error)}`);
  }
}

function canonicalJson(value) {
  if (Array.isArray(value)) return `[${value.map(canonicalJson).join(',')}]`;
  if (value && typeof value === 'object') {
    return `{${Object.keys(value).sort().map((key) => `${JSON.stringify(key)}:${canonicalJson(value[key])}`).join(',')}}`;
  }
  return JSON.stringify(value);
}

function privateTurnContext() {
  const markerPath = process.env.BLUEPRINT_RESIDENT_PRIVATE_TURN_MARKER_PATH || '';
  if (!markerPath || !fs.existsSync(markerPath)) {
    return { marker: 'no-private-marker', receiptBound: false };
  }
  const marker = readRegular(markerPath, 4_096, 'private Workday turn marker');
  const parsed = JSON.parse(marker);
  const incarnation = process.env.BLUEPRINT_RESIDENT_INCARNATION_ID || '';
  if (
    !parsed || typeof parsed !== 'object' || Array.isArray(parsed)
    || !['active-call', 'typed-consultation', 'typed-autonomous'].includes(parsed.kind)
    || typeof parsed.incarnation_id !== 'string' || !incarnation
    || parsed.incarnation_id !== incarnation
  ) {
    throw new Error('private Workday turn marker is invalid');
  }
  const receiptBound = parsed.kind === 'typed-consultation';
  if (
    receiptBound
    && (typeof parsed.packet_id !== 'string' || !parsed.packet_id
      || typeof parsed.workday_claim_id !== 'string' || !parsed.workday_claim_id)
  ) {
    throw new Error('private Workday consultation marker is incomplete');
  }
  return { marker, receiptBound };
}

function operationIdFor(idempotencyKey, marker) {
  const digest = createHash('sha256')
    .update('blueprint-resident-strategic-operation-v1\0')
    .update(process.env.BLUEPRINT_RESIDENT_INCARNATION_ID || '')
    .update('\0').update(marker).update('\0').update(idempotencyKey)
    .digest();
  digest[6] = (digest[6] & 0x0f) | 0x50;
  digest[8] = (digest[8] & 0x3f) | 0x80;
  const hex = digest.subarray(0, 16).toString('hex');
  return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`;
}

function helperEnv(operationId) {
  const scratch = process.env.TMPDIR || '';
  if (!path.isAbsolute(scratch) || !fs.lstatSync(scratch).isDirectory()) {
    throw new Error('Resident helper scratch is not a configured directory');
  }
  const env = {
    PATH: process.env.RESIDENT_STRATEGIC_PATH || '/usr/local/bin:/usr/bin:/bin',
    LANG: process.env.RESIDENT_STRATEGIC_LANG || 'C.UTF-8',
    TERM: process.env.RESIDENT_STRATEGIC_TERM || 'dumb',
    TMPDIR: scratch,
    ORCHESTRATOR_URL: process.env.ORCHESTRATOR_URL || '',
    AGENT_TOKEN_FILE: process.env.AGENT_TOKEN_FILE || '',
    BLUEPRINT_RESIDENT_INCARNATION_ID: process.env.BLUEPRINT_RESIDENT_INCARNATION_ID || '',
    BLUEPRINT_RESIDENT_REPLY_KEY_PATH: process.env.BLUEPRINT_RESIDENT_REPLY_KEY_PATH || '',
    BLUEPRINT_RESIDENT_REPLY_RECEIPT_PATH: process.env.BLUEPRINT_RESIDENT_REPLY_RECEIPT_PATH || '',
    BLUEPRINT_RESIDENT_STAGED_REPLY_PATH: process.env.BLUEPRINT_RESIDENT_STAGED_REPLY_PATH || '',
    BLUEPRINT_RESIDENT_PRIVATE_TURN_MARKER_PATH: process.env.BLUEPRINT_RESIDENT_PRIVATE_TURN_MARKER_PATH || '',
    BLUEPRINT_STRATEGIC_MAX_ATTEMPTS: '2',
  };
  if (operationId) env.BLUEPRINT_STRATEGIC_OPERATION_ID = operationId;
  return env;
}

function runHelper(executable, args, operationId) {
  return new Promise((resolve) => {
    if (!path.isAbsolute(executable)) return resolve({ ok: false, detail: 'helper path is not absolute' });
    try {
      const stat = fs.lstatSync(executable);
      if (!stat.isFile()) return resolve({ ok: false, detail: 'helper is not a regular file' });
    } catch {
      return resolve({ ok: false, detail: 'helper is unavailable' });
    }
    const child = spawn(executable, args, {
      cwd: process.env.TMPDIR || '/',
      env: helperEnv(operationId),
      detached: true,
      shell: false,
      stdio: ['ignore', 'pipe', 'pipe'],
    });
    activeHelperChildren.add(child);
    let stdout = Buffer.alloc(0);
    let stderr = Buffer.alloc(0);
    let overflow = false;
    let timedOut = false;
    let settled = false;
    const signalGroup = (signal) => {
      if (!child.pid) return;
      try {
        process.kill(-child.pid, signal);
      } catch {
        try { child.kill(signal); } catch { /* close/error settles the result */ }
      }
    };
    const stopGroup = () => {
      // Once the result is ambiguous, descendants must not keep mutating during
      // a grace window. Signal graceful stop, then kill the whole group now.
      signalGroup('SIGTERM');
      signalGroup('SIGKILL');
    };
    const append = (current, chunk) => {
      const next = Buffer.concat([current, chunk]);
      if (next.length > MAX_HELPER_OUTPUT_BYTES) {
        overflow = true;
        stopGroup();
        return next.subarray(0, MAX_HELPER_OUTPUT_BYTES);
      }
      return next;
    };
    child.stdout.on('data', (chunk) => { stdout = append(stdout, chunk); });
    child.stderr.on('data', (chunk) => { stderr = append(stderr, chunk); });
    const timer = setTimeout(() => {
      if (!settled) {
        timedOut = true;
        stopGroup();
      }
    }, HELPER_TIMEOUT_MS);
    child.on('error', (error) => {
      if (settled) return;
      settled = true;
      activeHelperChildren.delete(child);
      clearTimeout(timer);
      resolve({ ok: false, detail: `helper launch failed: ${String(error)}` });
    });
    child.on('close', (code, signal) => {
      if (settled) return;
      settled = true;
      activeHelperChildren.delete(child);
      clearTimeout(timer);
      const detail = stderr.toString('utf8').slice(-4_096).trim();
      resolve({
        ok: code === 0 && signal === null && !overflow && !timedOut,
        code,
        signal,
        overflow,
        stdout: stdout.toString('utf8'),
        detail,
      });
    });
  });
}

async function handleStrategic(id, args) {
  const ops = strategicOps();
  if (!ops) return toolError(id, 'strategic_operation is not configured');
  if (!args || typeof args !== 'object' || Array.isArray(args)) {
    return toolError(id, 'strategic_operation requires an arguments object');
  }
  if (Object.keys(args).some((key) => !['operation', 'data', 'idempotency_key'].includes(key))) {
    return toolError(id, 'strategic_operation received an unknown argument');
  }
  const operation = typeof args.operation === 'string' ? args.operation : '';
  const contract = ops[operation];
  if (!contract || typeof contract !== 'object' || Array.isArray(contract)) {
    return toolError(id, 'strategic_operation operation is not allowed');
  }
  const idempotencyKey = typeof args.idempotency_key === 'string' ? args.idempotency_key : '';
  if (!/^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$/.test(idempotencyKey)) {
    return toolError(id, 'strategic_operation idempotency_key is invalid');
  }
  const data = args.data;
  if (!data || typeof data !== 'object' || Array.isArray(data)) {
    return toolError(id, 'strategic_operation data must be an object');
  }
  const keys = Object.keys(data);
  if (Array.isArray(contract.required) && contract.required.some((key) => !keys.includes(key))) {
    return toolError(id, 'strategic_operation data is missing a required field');
  }
  if (Array.isArray(contract.fields) && contract.fields.length > 0 && keys.some((key) => !contract.fields.includes(key))) {
    return toolError(id, 'strategic_operation data contains an unknown field');
  }
  const payload = JSON.stringify(data);
  if (Buffer.byteLength(payload, 'utf8') > MAX_STRATEGIC_INPUT_BYTES) {
    return toolError(id, 'strategic_operation data exceeds 100KiB');
  }
  const privateTurn = privateTurnContext();
  const operationId = operationIdFor(idempotencyKey, privateTurn.marker);
  const fingerprint = createHash('sha256').update(canonicalJson({ op: contract.backend_op, data })).digest('hex');
  const completed = await runHelper(strategicHelperPath, [operation, '--json', payload], operationId);
  if (!completed.ok) {
    return toolError(id, `strategic_operation ended ambiguously: ${completed.detail || 'helper did not complete cleanly'}`, {
      outcome: 'ambiguous', applied: false, operation, idempotency_key: idempotencyKey,
      operation_id: operationId, operation_fingerprint: fingerprint,
    });
  }
  let response;
  try {
    response = JSON.parse(completed.stdout.trim());
  } catch {
    return toolError(id, 'strategic_operation returned an invalid response', {
      outcome: 'ambiguous', applied: false, operation, idempotency_key: idempotencyKey,
      operation_id: operationId, operation_fingerprint: fingerprint,
    });
  }
  const raw = response && typeof response === 'object' && !Array.isArray(response) ? response : {};
  const refused = raw.applied === false || raw.status === 'refused' || raw.http_status === 409;
  const replayed = raw.idempotent_replay === true || raw.effect === 'replayed';
  const authoritativeDeferred = raw.status === 'deferred'
    || raw.effect === 'deferred' || raw.http_status === 202;
  const nonApplying = ['blocked', 'noop', 'preview', 'already_resolved', 'skipped'].includes(raw.status);
  const explicitlyApplied = raw.applied === true || raw.status === 'ok';
  const outcome = refused ? 'refused'
    : replayed ? 'replayed'
      : authoritativeDeferred || nonApplying ? 'deferred'
        : explicitlyApplied ? 'applied' : 'invalid';
  const applied = outcome === 'applied';
  const commitAcknowledged = outcome === 'applied' && applied;
  const receiptConfirmed = privateTurn.receiptBound
    && (outcome === 'applied' || outcome === 'replayed' || authoritativeDeferred);
  const structuredContent = {
    outcome, applied, commit_acknowledged: commitAcknowledged,
    receipt_confirmed: receiptConfirmed, operation,
    idempotency_key: idempotencyKey, operation_id: operationId,
    operation_fingerprint: fingerprint, response,
  };
  if (outcome === 'invalid') {
    return toolError(id, 'strategic_operation returned no authoritative outcome', structuredContent);
  }
  send({
    jsonrpc: '2.0', id,
    result: {
      content: [{ type: 'text', text: JSON.stringify(structuredContent) }],
      structuredContent,
    },
  });
}

async function handleProjectRead(id, args) {
  const reads = projectReads();
  if (!reads) return toolError(id, 'project_read is not configured');
  if (!args || typeof args !== 'object' || Array.isArray(args)) {
    return toolError(id, 'project_read requires an arguments object');
  }
  if (Object.keys(args).some((key) => !['resource', 'id'].includes(key))) {
    return toolError(id, 'project_read received an unknown argument');
  }
  const resource = typeof args.resource === 'string' ? args.resource : '';
  if (!reads.has(resource)) return toolError(id, 'project_read resource is not allowed');
  const resourceId = args.id === undefined ? '' : args.id;
  if (typeof resourceId !== 'string' || (resourceId && !/^[A-Za-z0-9][A-Za-z0-9._:-]{0,199}$/.test(resourceId))) {
    return toolError(id, 'project_read id is invalid');
  }
  const completed = await runHelper(
    projectReadHelperPath,
    resourceId ? [resource, resourceId] : [resource],
    null,
  );
  if (!completed.ok) {
    return toolError(id, `project_read failed: ${completed.detail || 'helper did not complete cleanly'}`);
  }
  const text = completed.stdout.trim();
  send({
    jsonrpc: '2.0', id,
    result: {
      content: [{ type: 'text', text }],
      structuredContent: { resource, ...(resourceId ? { id: resourceId } : {}), bytes: Buffer.byteLength(text, 'utf8') },
    },
  });
}

async function handleToolCall(id, params) {
  if (params?.name === REPLY_TOOL) return handleReply(id, params.arguments);
  if (params?.name === STRATEGIC_TOOL) return handleStrategic(id, params.arguments);
  if (params?.name === PROJECT_READ_TOOL) return handleProjectRead(id, params.arguments);
  return toolError(id, `Unknown Workday tool: ${String(params?.name || '')}`);
}

async function handle(request) {
  if (!request || request.jsonrpc !== '2.0' || typeof request.method !== 'string') return;
  const id = request.id;
  if (request.method === 'notifications/initialized') return;
  if (request.method === 'initialize') {
    return send({
      jsonrpc: '2.0', id,
      result: {
        protocolVersion: typeof request.params?.protocolVersion === 'string'
          ? request.params.protocolVersion : '2024-11-05',
        capabilities: { tools: {} },
        serverInfo: { name: 'workday', version: '2.0.0' },
      },
    });
  }
  if (request.method === 'tools/list') {
    return send({ jsonrpc: '2.0', id, result: { tools: toolDefinitions() } });
  }
  if (request.method === 'tools/call') return handleToolCall(id, request.params);
  if (id !== undefined) {
    send({ jsonrpc: '2.0', id, error: { code: -32601, message: `Method not found: ${request.method}` } });
  }
}

const input = readline.createInterface({ input: process.stdin, crlfDelay: Infinity });
let requestQueue = Promise.resolve();
input.on('line', (line) => {
  requestQueue = requestQueue.then(async () => {
    let request;
    try {
      request = JSON.parse(line);
      await handle(request);
    } catch (error) {
      const detail = String(error).slice(0, 1_024);
      if (request && request.id !== undefined) {
        toolError(request.id, `Workday tool failed closed: ${detail}`);
      } else {
        process.stderr.write(`invalid MCP request: ${detail}\n`);
      }
    }
  });
});
