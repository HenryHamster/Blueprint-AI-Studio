#!/usr/bin/env node
// Minimal stdio MCP server for the Resident's one authoritative Workday reply
// action.  It deliberately does not call the backend: the Resident handler
// validates this receipt-bound staged intent, then atomically persists it with
// the operator message's handled state.

import fs from 'node:fs';
import { Buffer } from 'node:buffer';
import process from 'node:process';
import readline from 'node:readline';

const MAX_REPLY_BYTES = 65_536;
const TOOL_NAME = 'reply_to_operator';
const receiptPath = process.env.RESIDENT_REPLY_RECEIPT_PATH || '';
const stagedReplyPath = process.env.RESIDENT_STAGED_REPLY_PATH || '';

function send(message) {
  process.stdout.write(`${JSON.stringify(message)}\n`);
}

function toolError(id, text) {
  send({
    jsonrpc: '2.0',
    id,
    result: { content: [{ type: 'text', text }], isError: true },
  });
}

function readRegularJson(pathname, maxBytes, label) {
  if (!pathname) throw new Error(`${label} path is not configured`);
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
    return JSON.parse(raw);
  } finally {
    fs.closeSync(fd);
  }
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
    name: TOOL_NAME,
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

function handleToolCall(id, params) {
  if (params?.name !== TOOL_NAME) return toolError(id, `Unknown Workday tool: ${String(params?.name || '')}`);
  const args = params.arguments;
  if (!args || typeof args !== 'object' || Array.isArray(args)) {
    return toolError(id, 'reply_to_operator requires an arguments object');
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
        jsonrpc: '2.0',
        id,
        result: {
          content: [{
            type: 'text',
            text: 'No active operator turn is armed. Nothing was delivered; wait for an operator message before using reply_to_operator.',
          }],
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
      jsonrpc: '2.0',
      id,
      result: {
        content: [{ type: 'text', text: 'Workday reply accepted for delivery.' }],
        structuredContent: { accepted: true },
      },
    });
  } catch (error) {
    toolError(id, `reply_to_operator could not accept this reply: ${String(error)}`);
  }
}

function handle(request) {
  if (!request || request.jsonrpc !== '2.0' || typeof request.method !== 'string') return;
  const id = request.id;
  if (request.method === 'notifications/initialized') return;
  if (request.method === 'initialize') {
    return send({
      jsonrpc: '2.0',
      id,
      result: {
        protocolVersion: typeof request.params?.protocolVersion === 'string'
          ? request.params.protocolVersion : '2024-11-05',
        capabilities: { tools: {} },
        serverInfo: { name: 'workday', version: '1.0.0' },
      },
    });
  }
  if (request.method === 'tools/list') {
    return send({ jsonrpc: '2.0', id, result: { tools: [replyToolDefinition()] } });
  }
  if (request.method === 'tools/call') return handleToolCall(id, request.params);
  if (id !== undefined) {
    send({ jsonrpc: '2.0', id, error: { code: -32601, message: `Method not found: ${request.method}` } });
  }
}

const input = readline.createInterface({ input: process.stdin, crlfDelay: Infinity });
input.on('line', (line) => {
  try { handle(JSON.parse(line)); } catch (error) {
    // A malformed client request is not a process-fatal error: keep the tool
    // server available for the rest of the Resident turn.
    process.stderr.write(`invalid MCP request: ${String(error)}\n`);
  }
});
