import { describe, expect, it } from 'vitest';
import { execFileSync } from 'child_process';
import fs from 'fs';
import os from 'os';
import path from 'path';
import { provisionCallResidentTool } from './call-resident-tool.js';

function invoke(script: string, env: NodeJS.ProcessEnv, request: unknown): Record<string, unknown> {
  return JSON.parse(execFileSync(process.execPath, [script], { input: `${JSON.stringify(request)}\n`, encoding: 'utf8', env }));
}

describe('local Call Resident MCP tools', () => {
  it('exposes no Workday/public reply tool to Fast and stages only receipt-bound completion', () => {
    const workspace = fs.mkdtempSync(path.join(os.tmpdir(), 'call-mcp-'));
    const receiptPath = path.join(workspace, 'receipt.json');
    const stageDir = path.join(workspace, 'stage');
    fs.writeFileSync(receiptPath, JSON.stringify({ call_id: 'call', call_generation: 1, input_revision: 2, idempotency_key: 'key' }));
    try {
      const tool = provisionCallResidentTool(workspace, 'fast', { receiptPath, stageDir });
      const config = JSON.parse(fs.readFileSync(tool.configPath, 'utf8')) as { mcpServers: { call: { args: string[]; env: NodeJS.ProcessEnv } } };
      const listed = invoke(config.mcpServers.call.args[0], { ...process.env, ...config.mcpServers.call.env }, { jsonrpc: '2.0', id: 1, method: 'tools/list' });
      expect((listed.result as { tools: Array<{ name: string }> }).tools.map((toolDef) => toolDef.name)).toEqual(['complete_turn', 'request_workday', 'show_display_blocks']);
      invoke(config.mcpServers.call.args[0], { ...process.env, ...config.mcpServers.call.env }, { jsonrpc: '2.0', id: 2, method: 'tools/call', params: { name: 'complete_turn', arguments: { spoken_text: 'Done.' } } });
      const staged = fs.readdirSync(stageDir).filter((name) => name.startsWith('complete-'));
      expect(staged).toHaveLength(1);
      expect(JSON.parse(fs.readFileSync(path.join(stageDir, staged[0]), 'utf8'))).toMatchObject({ spoken_text: 'Done.', receipt: { idempotency_key: 'key' } });
    } finally { fs.rmSync(workspace, { recursive: true, force: true }); }
  });

  it('gives Workday only the private packet emitter', () => {
    const workspace = fs.mkdtempSync(path.join(os.tmpdir(), 'call-workday-mcp-'));
    try {
      const tool = provisionCallResidentTool(workspace, 'workday', { stageDir: path.join(workspace, 'stage') });
      const config = JSON.parse(fs.readFileSync(tool.configPath, 'utf8')) as { mcpServers: { call: { args: string[]; env: NodeJS.ProcessEnv } } };
      const listed = invoke(config.mcpServers.call.args[0], { ...process.env, ...config.mcpServers.call.env }, { jsonrpc: '2.0', id: 1, method: 'tools/list' });
      expect((listed.result as { tools: Array<{ name: string }> }).tools.map((toolDef) => toolDef.name)).toEqual(['emit_call_packet']);
      invoke(config.mcpServers.call.args[0], { ...process.env, ...config.mcpServers.call.env }, {
        jsonrpc: '2.0', id: 2, method: 'tools/call', params: { name: 'emit_call_packet', arguments: { packet: {
          version: 1, packet_id: 'packet-1', project_id: 'project-1', call_session_id: 'call-1',
          call_generation: 1, request_id: 'request-1', input_revision: 0, kind: 'final', spoken_text: 'Result.',
        } } },
      });
      expect(fs.readdirSync(path.join(workspace, 'stage')).some((name) => name.startsWith('packet-'))).toBe(true);
    } finally { fs.rmSync(workspace, { recursive: true, force: true }); }
  });
});
