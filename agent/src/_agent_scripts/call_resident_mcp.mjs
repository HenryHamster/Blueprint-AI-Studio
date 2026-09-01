#!/usr/bin/env node
/* global Buffer, process */
// Strictly local MCP bridge for the text-only Call Coordinator.  It never
// calls Blueprint or the network: it validates and stages a typed intent for
// the parent process to fence against the current call lease.

import crypto from 'node:crypto';
import fs from 'node:fs';
import path from 'node:path';
import readline from 'node:readline';

const role = process.env.CALL_RESIDENT_ROLE || '';
const receiptPath = process.env.CALL_TURN_RECEIPT_PATH || '';
const stageDir = process.env.CALL_LOCAL_STAGE_DIR || '';
const privateTurnMarkerPath = process.env.BLUEPRINT_RESIDENT_PRIVATE_TURN_MARKER_PATH || '';
const MAX_BYTES = 65_536;
const MAX_PACKET_TEXT = 24_000;
const CALL_PACKET_REQUIRED = [
  'packet_id', 'kind', 'spoken_text',
];
const CALL_PACKET_AUTHORITY_FIELDS = [
  'version', 'project_id', 'call_session_id', 'call_generation', 'request_id',
  'originating_user_turn_ids', 'input_revision', 'context_sequence',
];
const CALL_PACKET_PROPERTIES = {
  packet_id: { type: 'string', minLength: 1, maxLength: 256 },
  kind: { type: 'string', enum: ['progress', 'final', 'blocked'] },
  spoken_text: { type: 'string', minLength: 1, maxLength: MAX_PACKET_TEXT },
  exact_text: { type: 'string', minLength: 1, maxLength: MAX_PACKET_TEXT },
  display_blocks: {
    type: 'array', maxItems: 8,
    items: {
      type: 'object',
      properties: {
        type: { type: 'string', enum: ['latex', 'code', 'table', 'artifact_ref'] },
        content: { type: 'string', minLength: 1, maxLength: 16_000 },
      },
      required: ['type', 'content'],
      additionalProperties: false,
    },
  },
  evidence_refs: {
    type: 'array', maxItems: 32,
    items: { type: 'string', minLength: 1, maxLength: 512 },
  },
  supersedes_packet_id: { type: 'string', minLength: 1, maxLength: 256 },
};

function send(message) { process.stdout.write(`${JSON.stringify(message)}\n`); }
function error(id, text) { send({ jsonrpc: '2.0', id, result: { content: [{ type: 'text', text }], isError: true } }); }
function text(id, value) { send({ jsonrpc: '2.0', id, result: { content: [{ type: 'text', text: value }] } }); }
function object(value) { return value && typeof value === 'object' && !Array.isArray(value); }
function nonEmpty(value, label, max = MAX_BYTES) {
  if (typeof value !== 'string' || !value.trim() || Buffer.byteLength(value, 'utf8') > max) throw new Error(`invalid ${label}`);
  return value;
}
function regularJson(filename, label) {
  if (!filename) throw new Error(`${label} is not configured`);
  const before = fs.lstatSync(filename);
  if (!before.isFile() || before.size > MAX_BYTES) throw new Error(`invalid ${label}`);
  const fd = fs.openSync(filename, fs.constants.O_RDONLY | (fs.constants.O_NOFOLLOW || 0));
  try {
    const stat = fs.fstatSync(fd);
    if (!stat.isFile() || stat.dev !== before.dev || stat.ino !== before.ino) throw new Error(`${label} changed identity`);
    return JSON.parse(fs.readFileSync(fd, 'utf8'));
  } finally { fs.closeSync(fd); }
}
function boundedStringArray(value, label, maxItems, maxStringBytes) {
  if (!Array.isArray(value) || value.length > maxItems) throw new Error(`invalid ${label}`);
  return value.map((entry) => nonEmpty(entry, label, maxStringBytes));
}
function validateCallPacket(value) {
  const marker = regularJson(privateTurnMarkerPath, 'private Workday turn marker');
  if (!object(marker) || marker.kind !== 'active-call') {
    throw new Error('emit_call_packet requires an active private call turn');
  }
  if (!object(value) || !['progress', 'final', 'blocked'].includes(value.kind)) {
    throw new Error('invalid call packet');
  }
  if (CALL_PACKET_AUTHORITY_FIELDS.some((field) => Object.hasOwn(value, field))) {
    throw new Error('call packet authority is Coordinator-owned');
  }
  const authority = {
    project_id: nonEmpty(marker.project_id, 'project_id', 256),
    call_session_id: nonEmpty(marker.call_session_id, 'call_session_id', 256),
    call_generation: marker.call_generation,
    request_id: nonEmpty(marker.request_id, 'request_id', 256),
    originating_user_turn_ids: boundedStringArray(
      marker.originating_user_turn_ids, 'originating_user_turn_ids', 32, 256,
    ),
    input_revision: marker.input_revision,
    context_sequence: marker.context_sequence,
  };
  if (!Number.isSafeInteger(authority.call_generation) || authority.call_generation < 1
    || !Number.isSafeInteger(authority.input_revision) || authority.input_revision < 0
    || !Number.isSafeInteger(authority.context_sequence) || authority.context_sequence < 0
    || authority.originating_user_turn_ids.length === 0) {
    throw new Error('private Workday turn marker has invalid call authority');
  }
  const packet = {
    version: 1,
    packet_id: nonEmpty(value.packet_id, 'packet_id', 256),
    ...authority,
    kind: value.kind,
    spoken_text: nonEmpty(value.spoken_text, 'spoken_text', MAX_PACKET_TEXT),
    ...(value.exact_text === undefined
      ? {} : { exact_text: nonEmpty(value.exact_text, 'exact_text', MAX_PACKET_TEXT) }),
    ...(value.display_blocks === undefined ? {} : { display_blocks: value.display_blocks }),
    ...(value.evidence_refs === undefined ? {} : {
      evidence_refs: boundedStringArray(value.evidence_refs, 'evidence_refs', 32, 512),
    }),
    ...(value.supersedes_packet_id === undefined ? {} : {
      supersedes_packet_id: nonEmpty(value.supersedes_packet_id, 'supersedes_packet_id', 256),
    }),
  };
  if (packet.display_blocks !== undefined) {
    if (!Array.isArray(packet.display_blocks) || packet.display_blocks.length > 8) {
      throw new Error('invalid display_blocks');
    }
    packet.display_blocks = packet.display_blocks.map((entry) => {
      if (!object(entry) || !['latex', 'code', 'table', 'artifact_ref'].includes(entry.type)) {
        throw new Error('invalid display_blocks');
      }
      return { type: entry.type, content: nonEmpty(entry.content, 'display block content', 16_000) };
    });
  }
  if (packet.evidence_refs?.some((ref) => /^[a-z][a-z0-9+.-]*:\/\//i.test(ref))) {
    throw new Error('invalid evidence_refs');
  }
  if (Buffer.byteLength(JSON.stringify(packet), 'utf8') > MAX_BYTES) throw new Error('call packet exceeds staging limit');
  return packet;
}
function activeReceipt() {
  const receipt = regularJson(receiptPath, 'foreground turn receipt');
  const callReceipt = role === 'fast'
    && typeof receipt.call_id === 'string'
    && Number.isSafeInteger(receipt.call_generation)
    && Number.isSafeInteger(receipt.input_revision)
    && Number.isSafeInteger(receipt.context_sequence)
    && Array.isArray(receipt.originating_user_turn_ids)
    && receipt.originating_user_turn_ids.length > 0
    && receipt.originating_user_turn_ids.every((id) => typeof id === 'string' && id)
    && typeof receipt.idempotency_key === 'string';
  const typedReceipt = role === 'typed'
    && typeof receipt.claim_id === 'string'
    && Array.isArray(receipt.message_ids)
    && receipt.message_ids.length > 0
    && receipt.message_ids.every((id) => typeof id === 'string' && id)
    && typeof receipt.idempotency_key === 'string';
  if (!object(receipt) || (!callReceipt && !typedReceipt)) {
    throw new Error('invalid foreground turn receipt');
  }
  return receipt;
}
function stage(kind, value, identity) {
  if (!stageDir) throw new Error('local stage directory is not configured');
  fs.mkdirSync(stageDir, { recursive: true, mode: 0o700 });
  fs.chmodSync(stageDir, 0o700);
  const digest = crypto.createHash('sha256').update(`${kind}:${identity}`).digest('hex');
  const target = path.join(stageDir, `${kind}-${digest}.json`);
  const temporary = path.join(stageDir, `.${digest}.${process.pid}.${Date.now()}.tmp`);
  try {
    fs.writeFileSync(temporary, JSON.stringify(value), { encoding: 'utf8', mode: 0o600, flag: 'wx' });
    fs.linkSync(temporary, target); // first write wins; duplicate calls fail closed
  } finally { try { fs.unlinkSync(temporary); } catch { /* already absent */ } }
}
function definitions() {
  if (role === 'typed') return [
    { name: 'complete_turn', description: 'Complete the active public typed turn using its receipt.', inputSchema: { type: 'object', properties: { spoken_text: { type: 'string' }, durable: { type: 'boolean' } }, required: ['spoken_text'], additionalProperties: false } },
    { name: 'request_workday', description: 'Request durable private Workday reasoning while Fast still completes this turn.', inputSchema: { type: 'object', properties: { request_id: { type: 'string' }, prompt: { type: 'string' } }, required: ['request_id', 'prompt'], additionalProperties: false } },
  ];
  if (role === 'fast') return [
    { name: 'complete_turn', description: 'Complete the active public call turn using its receipt.', inputSchema: { type: 'object', properties: { spoken_text: { type: 'string' }, exact_text: { type: 'string' }, display_blocks: { type: 'array' }, durable: { type: 'boolean' } }, required: ['spoken_text'], additionalProperties: false } },
    { name: 'request_workday', description: 'Request private Workday reasoning. This does not speak or grant authority.', inputSchema: { type: 'object', properties: { request_id: { type: 'string' }, prompt: { type: 'string' } }, required: ['request_id', 'prompt'], additionalProperties: false } },
    { name: 'show_display_blocks', description: 'Render exact opaque display blocks through the Coordinator.', inputSchema: { type: 'object', properties: { display_blocks: { type: 'array' } }, required: ['display_blocks'], additionalProperties: false } },
  ];
  if (role === 'workday') return [{
    name: 'emit_call_packet',
    description: 'Emit one private call work packet using the exact active-call identity supplied by the Coordinator. Never guess an identity. This tool never replies directly to the operator.',
    inputSchema: {
      type: 'object',
      properties: {
        packet: {
          type: 'object', properties: CALL_PACKET_PROPERTIES,
          required: CALL_PACKET_REQUIRED, additionalProperties: false,
        },
      },
      required: ['packet'], additionalProperties: false,
    },
  }];
  return [];
}
function handleCall(id, params) {
  const name = params?.name;
  const args = params?.arguments;
  if (!definitions().some((tool) => tool.name === name) || !object(args)) return error(id, 'unknown or invalid local call tool');
  try {
    if (name === 'complete_turn') {
      const receipt = activeReceipt();
      const completion = role === 'typed'
        ? { receipt, content: nonEmpty(args.spoken_text, 'content'), durable: args.durable !== false }
        : { receipt, spoken_text: nonEmpty(args.spoken_text, 'spoken_text'), exact_text: args.exact_text === undefined ? undefined : nonEmpty(args.exact_text, 'exact_text'), display_blocks: args.display_blocks, durable: args.durable !== false };
      stage('complete', completion, receipt.idempotency_key);
      return text(id, role === 'typed'
        ? 'Conversation completion accepted for Coordinator validation.'
        : 'Call completion accepted for Coordinator validation.');
    }
    if (name === 'request_workday') {
      const receipt = activeReceipt();
      const request = role === 'typed'
        ? { delivery_key: nonEmpty(args.request_id, 'delivery_key', 256), prompt: nonEmpty(args.prompt, 'prompt'), receipt }
        : { request_id: nonEmpty(args.request_id, 'request_id', 256), prompt: nonEmpty(args.prompt, 'prompt'), receipt };
      // The model chooses the descriptive request id, but the immutable turn
      // receipt owns authority. First write wins for that exact input so one
      // turn cannot fan out duplicate private Workday jobs under fresh ids.
      stage('request', request, receipt.idempotency_key);
      return text(id, role === 'typed'
        ? 'Workday consultation request accepted for Coordinator validation.'
        : 'Private Workday request accepted for Coordinator validation.');
    }
    if (name === 'show_display_blocks') {
      if (!Array.isArray(args.display_blocks) || args.display_blocks.length > 8) throw new Error('invalid display_blocks');
      const receipt = activeReceipt();
      stage('display', { receipt, display_blocks: args.display_blocks }, `${receipt.idempotency_key}:${JSON.stringify(args.display_blocks)}`);
      return text(id, 'Display blocks accepted for Coordinator validation.');
    }
    const packet = validateCallPacket(args.packet);
    stage('packet', packet, packet.packet_id);
    return text(id, 'Private call packet accepted for Coordinator validation.');
  } catch (cause) { return error(id, `local call tool rejected input: ${String(cause)}`); }
}
function handle(request) {
  if (!object(request) || request.jsonrpc !== '2.0' || typeof request.method !== 'string') return;
  if (request.method === 'notifications/initialized') return;
  if (request.method === 'initialize') return send({ jsonrpc: '2.0', id: request.id, result: { protocolVersion: request.params?.protocolVersion || '2024-11-05', capabilities: { tools: {} }, serverInfo: { name: 'call-local', version: '1.0.0' } } });
  if (request.method === 'tools/list') return send({ jsonrpc: '2.0', id: request.id, result: { tools: definitions() } });
  if (request.method === 'tools/call') return handleCall(request.id, request.params);
  if (request.id !== undefined) send({ jsonrpc: '2.0', id: request.id, error: { code: -32601, message: 'Method not found' } });
}
readline.createInterface({ input: process.stdin, crlfDelay: Infinity }).on('line', (line) => { try { handle(JSON.parse(line)); } catch { /* remain available */ } });
