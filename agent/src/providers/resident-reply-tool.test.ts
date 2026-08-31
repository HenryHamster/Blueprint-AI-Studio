import { describe, expect, it } from 'vitest';
import { execFileSync } from 'child_process';
import fs from 'fs';
import os from 'os';
import path from 'path';
import {
  provisionResidentReplyTool, RESIDENT_REPLY_TOOL_NAME,
} from './resident-reply-tool.js';

function runMcp(scriptPath: string, env: NodeJS.ProcessEnv, requests: unknown[]): unknown[] {
  const output = execFileSync(process.execPath, [scriptPath], {
    input: requests.map(request => JSON.stringify(request)).join('\n') + '\n',
    encoding: 'utf8',
    env,
  });
  return output.trim().split('\n').filter(Boolean).map((line): unknown => JSON.parse(line) as unknown);
}

describe('Resident Workday reply MCP tool', () => {
  it('does not fail or stage a reply when no operator turn is armed', () => {
    const workspace = fs.mkdtempSync(path.join(os.tmpdir(), 'resident-reply-mcp-unarmed-'));
    const receiptPath = path.join(workspace, '.resident-reply-receipt.json');
    const stagedReplyPath = path.join(workspace, '.resident-reply-staged-message.json');
    try {
      const tool = provisionResidentReplyTool(workspace, { receiptPath, stagedReplyPath });
      const config = JSON.parse(fs.readFileSync(tool.configPath, 'utf8')) as {
        mcpServers: { workday: { args: string[]; env: NodeJS.ProcessEnv } };
      };
      const responses = runMcp(config.mcpServers.workday.args[0], {
        ...process.env,
        ...config.mcpServers.workday.env,
      }, [{
        jsonrpc: '2.0', id: 1, method: 'tools/call',
        params: { name: 'reply_to_operator', arguments: { content: 'not deliverable' } },
      }]) as Array<Record<string, unknown>>;

      const result = responses[0].result as {
        isError?: boolean;
        content: Array<{ text: string }>;
        structuredContent: { accepted: boolean };
      };
      expect(result.isError).not.toBe(true);
      expect(result.content[0].text).toContain('No active operator turn is armed');
      expect(result.structuredContent.accepted).toBe(false);
      expect(fs.existsSync(stagedReplyPath)).toBe(false);
    } finally {
      fs.rmSync(workspace, { recursive: true, force: true });
    }
  });

  it('exposes only the native reply action and stages an authenticated receipt-bound reply', () => {
    const workspace = fs.mkdtempSync(path.join(os.tmpdir(), 'resident-reply-mcp-'));
    const receiptPath = path.join(workspace, '.resident-reply-receipt.json');
    const stagedReplyPath = path.join(workspace, '.resident-reply-staged-message.json');
    const receipt = { idempotency_key: 'resident-reply-key', covered_message_ids: ['message-1', 'message-2'] };
    fs.writeFileSync(receiptPath, JSON.stringify(receipt), { mode: 0o600 });
    try {
      const tool = provisionResidentReplyTool(workspace, { receiptPath, stagedReplyPath });
      const config = JSON.parse(fs.readFileSync(tool.configPath, 'utf8')) as {
        mcpServers: { workday: { command: string; args: string[]; env: NodeJS.ProcessEnv } };
      };
      expect(tool.toolName).toBe(RESIDENT_REPLY_TOOL_NAME);
      expect(config.mcpServers.workday.command).toBe(process.execPath);

      const responses = runMcp(config.mcpServers.workday.args[0], {
        ...process.env,
        ...config.mcpServers.workday.env,
      }, [
        { jsonrpc: '2.0', id: 1, method: 'initialize', params: { protocolVersion: '2024-11-05' } },
        { jsonrpc: '2.0', id: 2, method: 'tools/list' },
        {
          jsonrpc: '2.0', id: 3, method: 'tools/call',
          params: { name: 'reply_to_operator', arguments: { content: 'Here is the actual reply.', message_type: 'QUESTION' } },
        },
      ]) as Array<Record<string, unknown>>;

      expect((responses[1].result as { tools: Array<{ name: string }> }).tools).toEqual([
        expect.objectContaining({ name: 'reply_to_operator' }),
      ]);
      expect((responses[2].result as { content: Array<{ text: string }> }).content[0].text)
        .toContain('accepted for delivery');
      expect((responses[2].result as { structuredContent: { accepted: boolean } }).structuredContent.accepted)
        .toBe(true);
      expect(JSON.parse(fs.readFileSync(stagedReplyPath, 'utf8'))).toEqual({
        type: 'QUESTION',
        content: 'Here is the actual reply.',
        ...receipt,
      });
    } finally {
      fs.rmSync(workspace, { recursive: true, force: true });
    }
  });

  it('does not overwrite the first staged reply when called twice', () => {
    const workspace = fs.mkdtempSync(path.join(os.tmpdir(), 'resident-reply-mcp-repeat-'));
    const receiptPath = path.join(workspace, '.resident-reply-receipt.json');
    const stagedReplyPath = path.join(workspace, '.resident-reply-staged-message.json');
    fs.writeFileSync(receiptPath, JSON.stringify({
      idempotency_key: 'resident-reply-key', covered_message_ids: ['message-1'],
    }), { mode: 0o600 });
    try {
      const tool = provisionResidentReplyTool(workspace, { receiptPath, stagedReplyPath });
      const config = JSON.parse(fs.readFileSync(tool.configPath, 'utf8')) as {
        mcpServers: { workday: { args: string[]; env: NodeJS.ProcessEnv } };
      };
      const responses = runMcp(config.mcpServers.workday.args[0], {
        ...process.env,
        ...config.mcpServers.workday.env,
      }, [
        { jsonrpc: '2.0', id: 1, method: 'tools/call', params: { name: 'reply_to_operator', arguments: { content: 'first' } } },
        { jsonrpc: '2.0', id: 2, method: 'tools/call', params: { name: 'reply_to_operator', arguments: { content: 'second' } } },
      ]) as Array<Record<string, unknown>>;

      expect((responses[1].result as { isError: boolean }).isError).toBe(true);
      expect(JSON.parse(fs.readFileSync(stagedReplyPath, 'utf8'))).toMatchObject({ content: 'first' });
    } finally {
      fs.rmSync(workspace, { recursive: true, force: true });
    }
  });
});
