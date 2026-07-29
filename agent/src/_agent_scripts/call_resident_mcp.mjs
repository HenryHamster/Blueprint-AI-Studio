#!/usr/bin/env node
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
const MAX_BYTES = 65_536;
const FAST_TOOLS = ['complete_turn', 'request_workday', 'show_display_blocks'];
const WORKDAY_TOOLS = ['emit_call_packet'];

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
function activeReceipt() {
  const receipt = regularJson(receiptPath, 'call turn receipt');
  if (!object(receipt) || typeof receipt.call_id !== 'string' || !Number.isSafeInteger(receipt.call_generation)
    || !Number.isSafeInteger(receipt.input_revision) || typeof receipt.idempotency_key !== 'string') {
    throw new Error('invalid call turn receipt');
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
  } finally { try { fs.unlinkSync(temporary); } catch {} }
}
function definitions() {
  if (role === 'fast') return [
    { name: 'complete_turn', description: 'Complete the active public call turn using its receipt.', inputSchema: { type: 'object', properties: { spoken_text: { type: 'string' }, exact_text: { type: 'string' }, display_blocks: { type: 'array' }, durable: { type: 'boolean' } }, required: ['spoken_text'], additionalProperties: false } },
    { name: 'request_workday', description: 'Request private Workday reasoning. This does not speak or grant authority.', inputSchema: { type: 'object', properties: { request_id: { type: 'string' }, prompt: { type: 'string' } }, required: ['request_id', 'prompt'], additionalProperties: false } },
    { name: 'show_display_blocks', description: 'Render exact opaque display blocks through the Coordinator.', inputSchema: { type: 'object', properties: { display_blocks: { type: 'array' } }, required: ['display_blocks'], additionalProperties: false } },
  ];
  if (role === 'workday') return [{ name: 'emit_call_packet', description: 'Emit one private, schema-validated call work packet. It never replies directly to the operator.', inputSchema: { type: 'object', properties: { packet: { type: 'object' } }, required: ['packet'], additionalProperties: false } }];
  return [];
}
function handleCall(id, params) {
  const name = params?.name;
  const args = params?.arguments;
  if (!definitions().some((tool) => tool.name === name) || !object(args)) return error(id, 'unknown or invalid local call tool');
  try {
    if (name === 'complete_turn') {
      const receipt = activeReceipt();
      const completion = { receipt, spoken_text: nonEmpty(args.spoken_text, 'spoken_text'), exact_text: args.exact_text === undefined ? undefined : nonEmpty(args.exact_text, 'exact_text'), display_blocks: args.display_blocks, durable: args.durable !== false };
      stage('complete', completion, receipt.idempotency_key);
      return text(id, 'Call completion accepted for Coordinator validation.');
    }
    if (name === 'request_workday') {
      const request = { request_id: nonEmpty(args.request_id, 'request_id', 256), prompt: nonEmpty(args.prompt, 'prompt'), receipt: activeReceipt() };
      stage('request', request, request.request_id);
      return text(id, 'Private Workday request accepted for Coordinator validation.');
    }
    if (name === 'show_display_blocks') {
      if (!Array.isArray(args.display_blocks) || args.display_blocks.length > 8) throw new Error('invalid display_blocks');
      const receipt = activeReceipt();
      stage('display', { receipt, display_blocks: args.display_blocks }, `${receipt.idempotency_key}:${JSON.stringify(args.display_blocks)}`);
      return text(id, 'Display blocks accepted for Coordinator validation.');
    }
    const packet = args.packet;
    if (!object(packet) || packet.version !== 1 || !['progress', 'final', 'blocked'].includes(packet.kind)
      || typeof packet.packet_id !== 'string' || !packet.packet_id || typeof packet.project_id !== 'string'
      || typeof packet.call_session_id !== 'string' || !Number.isSafeInteger(packet.call_generation)
      || !Number.isSafeInteger(packet.input_revision) || typeof packet.request_id !== 'string'
      || typeof packet.spoken_text !== 'string' || !packet.spoken_text.trim()) throw new Error('invalid call packet');
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
