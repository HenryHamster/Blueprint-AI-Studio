import { describe, expect, it } from 'vitest';
import { execFileSync } from 'child_process';
import fs from 'fs';
import os from 'os';
import path from 'path';
import { fileURLToPath } from 'url';
import {
  provisionResidentReplyTool, RESIDENT_PROJECT_READ_TOOL_NAME,
  RESIDENT_REPLY_TOOL_NAME, RESIDENT_STRATEGIC_TOOL_NAME,
} from './resident-reply-tool.js';

const fixtureHelper = path.resolve(
  path.dirname(fileURLToPath(import.meta.url)),
  '../_agent_scripts/resident_mcp_test_helper.py',
);

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
    const receipt = {
      idempotency_key: 'resident-reply-key',
      covered_message_ids: ['message-1', 'message-2'],
      reply_to_message_id: 'message-1',
    };
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
      reply_to_message_id: 'message-1',
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

  it('exposes only typed strategic/read actions and invokes fixed helpers with a clean environment', () => {
    const workspace = fs.mkdtempSync(path.join(os.tmpdir(), 'resident-workday-mcp-'));
    const receiptPath = path.join(workspace, '.resident-reply-receipt.json');
    const stagedReplyPath = path.join(workspace, '.resident-reply-staged-message.json');
    const markerPath = path.join(workspace, '.resident-private-turn.json');
    const helperPath = path.join(workspace, 'fixed helper;not-shell.mjs');
    fs.copyFileSync(fixtureHelper, helperPath);
    fs.chmodSync(helperPath, 0o700);
    fs.writeFileSync(markerPath, JSON.stringify({
      kind: 'typed-consultation',
      incarnation_id: 'incarnation-1',
      packet_id: 'packet-1',
      workday_claim_id: 'claim-1',
    }), { mode: 0o600 });
    try {
      const tool = provisionResidentReplyTool(workspace, {
        receiptPath,
        stagedReplyPath,
        strategic: {
          helperPath,
          readHelperPath: helperPath,
          readVerbs: ['problem-model', 'direction'],
          orchestratorUrl: 'http://blueprint-api:8000',
          agentTokenFile: '/run/blueprint/agent-token.jwt',
          incarnationId: 'incarnation-1',
          replyKeyPath: path.join(workspace, '.reply-key'),
          replyReceiptPath: receiptPath,
          stagedReplyPath,
          privateTurnMarkerPath: markerPath,
        },
      });
      expect(tool.strategicToolName).toBe(RESIDENT_STRATEGIC_TOOL_NAME);
      expect(tool.projectReadToolName).toBe(RESIDENT_PROJECT_READ_TOOL_NAME);
      const config = JSON.parse(fs.readFileSync(tool.configPath, 'utf8')) as {
        mcpServers: { workday: { args: string[]; env: NodeJS.ProcessEnv } };
      };
      const responses = runMcp(config.mcpServers.workday.args[0], {
        ...process.env,
        TMPDIR: workspace,
        CODEX_HOME: '/must-not-reach-helper',
        OPENAI_API_KEY: 'must-not-reach-helper',
        ...config.mcpServers.workday.env,
      }, [
        { jsonrpc: '2.0', id: 1, method: 'tools/list' },
        {
          jsonrpc: '2.0', id: 2, method: 'tools/call',
          params: {
            name: 'strategic_operation',
            arguments: {
              operation: 'set-running-update',
              data: { markdown: 'hello; exit 99' },
              idempotency_key: 'contract-running-update-1',
            },
          },
        },
        {
          jsonrpc: '2.0', id: 3, method: 'tools/call',
          params: { name: 'project_read', arguments: { resource: 'direction', id: 'd-123' } },
        },
        {
          jsonrpc: '2.0', id: 4, method: 'tools/call',
          params: {
            name: 'strategic_operation',
            arguments: {
              operation: '../../bin/sh', data: {}, idempotency_key: 'invalid-op-1',
            },
          },
        },
      ]) as Array<Record<string, unknown>>;

      expect((responses[0].result as { tools: Array<{ name: string }> }).tools.map(({ name }) => name))
        .toEqual(['reply_to_operator', 'strategic_operation', 'project_read']);
      const strategicDefinition = (responses[0].result as {
        tools: Array<{ name: string; inputSchema: { properties: {
          operation: { enum: string[] }; data: { description: string };
        } } }>;
      }).tools.find(({ name }) => name === 'strategic_operation');
      expect(strategicDefinition?.inputSchema.properties.operation.enum).not.toContain('create-message');
      expect(strategicDefinition?.inputSchema.properties.operation.enum).not.toContain('resolve-message');
      const contractMarker = 'contract-json=';
      const contractDescription = strategicDefinition?.inputSchema.properties.data.description ?? '';
      const contractOffset = contractDescription.indexOf(contractMarker);
      expect(contractOffset).toBeGreaterThanOrEqual(0);
      const advertisedContracts = JSON.parse(contractDescription.slice(contractOffset + contractMarker.length)) as Record<
        string, { required: string[]; fields: string[]; requirements: string[]; example: Record<string, unknown> }
      >;
      expect(advertisedContracts).toMatchObject({
        'spawn-task': {
          required: ['type', 'title'],
          fields: expect.arrayContaining(['prompt', 'direction_id', 'criterion_ids', 'depends_on']),
          requirements: expect.arrayContaining([
            expect.stringContaining('prompt must be a non-empty string'),
            expect.stringContaining('criteria inherited from direction_id'),
            expect.stringContaining('test_path'),
          ]),
          example: {
            acceptance_evidence: {
              kind: 'named_test',
              test_path: expect.any(String),
            },
          },
        },
        'set-running-update': { required: ['markdown'], fields: ['markdown'] },
        'set-memory': {
          required: [],
          fields: expect.arrayContaining(['body']),
          requirements: expect.arrayContaining([expect.stringContaining('body or note')]),
        },
        'set-acceptance-criteria': {
          required: [],
          fields: ['criteria', 'retract'],
          requirements: expect.arrayContaining([
            expect.stringContaining('direction_keys'),
            expect.stringContaining('expected_text'),
            expect.stringContaining('operator_authored_commits'),
            expect.stringContaining('must omit direction_keys'),
          ]),
          example: {
            criteria: [{
              text: expect.any(String),
              evidence_contract: {
                type: 'operator_authored_commits',
                predicate: { max_count: 0 },
              },
            }],
          },
        },
        'request-qualification': {
          required: [],
          fields: ['reason', 'probes'],
          requirements: expect.arrayContaining([expect.stringContaining('omit probes')]),
          example: { reason: expect.any(String) },
        },
      });
      expect(advertisedContracts['request-qualification'].example).not.toHaveProperty('probes');
      const strategic = (responses[1].result as {
        structuredContent: { applied: boolean; commit_acknowledged: boolean; receipt_confirmed: boolean; operation_fingerprint: string; response: {
          args: string[]; env: Record<string, string>; leaked: string[];
        } };
      }).structuredContent;
      expect(strategic).toMatchObject({
        applied: true, commit_acknowledged: true, receipt_confirmed: true,
      });
      expect(strategic.operation_fingerprint)
        .toBe('a59cf4767aca9d73463acd745c248fedaa9f96c438ea88e4720904212dd0112f');
      expect(strategic.response.args).toEqual([
        'set-running-update', '--json', '{"markdown":"hello; exit 99"}',
      ]);
      expect(strategic.response.env.PATH).toBe('/usr/local/bin:/usr/bin:/bin');
      expect(strategic.response.env.TMPDIR).toBe(workspace);
      expect(strategic.response.env.BLUEPRINT_STRATEGIC_OPERATION_ID).toMatch(/^[0-9a-f-]{36}$/);
      expect(strategic.response.env.BLUEPRINT_STRATEGIC_MAX_ATTEMPTS).toBe('2');
      expect(strategic.response.leaked).toEqual([]);

      const readText = (responses[2].result as { content: Array<{ text: string }> }).content[0].text;
      const read = JSON.parse(readText) as { args: string[]; leaked: string[] };
      expect(read.args).toEqual(['direction', 'd-123']);
      expect(read.leaked).toEqual([]);
      expect((responses[3].result as { isError: boolean }).isError).toBe(true);
      expect(fs.statSync(tool.configPath).mode & 0o777).toBe(0o600);
      expect(fs.statSync(path.join(workspace, '.resident-strategic-ops.json')).mode & 0o777).toBe(0o600);

      fs.unlinkSync(markerPath);
      const unbound = runMcp(config.mcpServers.workday.args[0], {
        ...process.env,
        TMPDIR: workspace,
        ...config.mcpServers.workday.env,
      }, [{
        jsonrpc: '2.0', id: 5, method: 'tools/call',
        params: {
          name: 'strategic_operation',
          arguments: {
            operation: 'set-running-update', data: { markdown: 'unbound' },
            idempotency_key: 'unbound-running-update-1',
          },
        },
      }]) as Array<{ result: { structuredContent: { receipt_confirmed: boolean } } }>;
      expect(unbound[0].result.structuredContent.receipt_confirmed).toBe(false);
    } finally {
      fs.rmSync(workspace, { recursive: true, force: true });
    }
  });

  it('reports exact refusal/replay receipt semantics and kills the whole helper process group', async () => {
    const workspace = fs.mkdtempSync(path.join(os.tmpdir(), 'resident-workday-helper-stop-'));
    const markerPath = path.join(workspace, '.resident-private-turn.json');
    const orphanPath = path.join(workspace, 'must-not-be-written');
    const helperPath = path.join(workspace, 'fixed-helper.py');
    fs.copyFileSync(fixtureHelper, helperPath);
    fs.chmodSync(helperPath, 0o700);
    fs.writeFileSync(markerPath, JSON.stringify({
      kind: 'typed-consultation', incarnation_id: 'incarnation-stop',
      packet_id: 'packet-stop', workday_claim_id: 'claim-stop',
    }), { mode: 0o600 });
    try {
      const tool = provisionResidentReplyTool(workspace, {
        receiptPath: path.join(workspace, '.receipt'),
        stagedReplyPath: path.join(workspace, '.staged'),
        strategic: {
          helperPath, readHelperPath: helperPath, readVerbs: ['running-update'],
          orchestratorUrl: 'http://blueprint-api:8000', agentTokenFile: '/run/agent-token',
          incarnationId: 'incarnation-stop', replyKeyPath: path.join(workspace, '.key'),
          replyReceiptPath: path.join(workspace, '.receipt'),
          stagedReplyPath: path.join(workspace, '.staged'), privateTurnMarkerPath: markerPath,
        },
      });
      const config = JSON.parse(fs.readFileSync(tool.configPath, 'utf8')) as {
        mcpServers: { workday: { args: string[]; env: NodeJS.ProcessEnv } };
      };
      const call = (id: number, markdown: string, key: string) => ({
        jsonrpc: '2.0', id, method: 'tools/call',
        params: {
          name: 'strategic_operation',
          arguments: { operation: 'set-running-update', data: { markdown }, idempotency_key: key },
        },
      });
      const responses = runMcp(config.mcpServers.workday.args[0], {
        ...process.env, TMPDIR: workspace, ...config.mcpServers.workday.env,
      }, [
        call(1, 'fixture-response:{"status":"refused","applied":false,"http_status":409}', 'refused-1'),
        call(2, 'fixture-response:{"status":"ok","idempotent_replay":true}', 'replay-1'),
        call(3, 'fixture-response:{"status":"blocked"}', 'blocked-1'),
        call(4, 'fixture-response:{"status":"preview"}', 'preview-1'),
        call(5, `fixture-overflow-child:${orphanPath}`, 'overflow-1'),
      ]) as Array<{ result: {
        isError?: boolean;
        structuredContent: {
          outcome: string; applied: boolean; commit_acknowledged?: boolean;
          receipt_confirmed?: boolean;
        };
      } }>;

      expect(responses[0].result.structuredContent).toMatchObject({
        outcome: 'refused', applied: false, commit_acknowledged: false,
        receipt_confirmed: false,
      });
      expect(responses[1].result.structuredContent).toMatchObject({
        outcome: 'replayed', applied: false, commit_acknowledged: false,
        receipt_confirmed: true,
      });
      expect(responses[2].result.structuredContent).toMatchObject({
        outcome: 'deferred', applied: false, receipt_confirmed: false,
      });
      expect(responses[3].result.structuredContent).toMatchObject({
        outcome: 'deferred', applied: false, receipt_confirmed: false,
      });
      expect(responses[4].result.isError).toBe(true);
      expect(responses[4].result.structuredContent).toMatchObject({
        outcome: 'ambiguous', applied: false,
      });
      await new Promise(resolve => setTimeout(resolve, 700));
      expect(fs.existsSync(orphanPath)).toBe(false);
    } finally {
      fs.rmSync(workspace, { recursive: true, force: true });
    }
  });
});
