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
      const markerPath = path.join(workspace, 'private-turn-marker.json');
      const tool = provisionCallResidentTool(workspace, 'workday', {
        stageDir: path.join(workspace, 'stage'), privateTurnMarkerPath: markerPath,
      });
      const config = JSON.parse(fs.readFileSync(tool.configPath, 'utf8')) as { mcpServers: { call: { args: string[]; env: NodeJS.ProcessEnv } } };
      const listed = invoke(config.mcpServers.call.args[0], { ...process.env, ...config.mcpServers.call.env }, { jsonrpc: '2.0', id: 1, method: 'tools/list' });
      const tools = (listed.result as { tools: Array<{ name: string; inputSchema: Record<string, unknown> }> }).tools;
      expect(tools.map((toolDef) => toolDef.name)).toEqual(['emit_call_packet']);
      const packetSchema = ((tools[0].inputSchema.properties as Record<string, unknown>).packet as {
        properties: Record<string, unknown>; required: string[]; additionalProperties: boolean;
      });
      expect(packetSchema.required).toEqual([
        'version', 'packet_id', 'project_id', 'call_session_id', 'call_generation',
        'request_id', 'originating_user_turn_ids', 'input_revision',
        'context_sequence', 'kind', 'spoken_text',
      ]);
      expect(packetSchema.additionalProperties).toBe(false);
      expect(packetSchema.properties.originating_user_turn_ids).toMatchObject({
        type: 'array', minItems: 1, maxItems: 32,
      });
      fs.writeFileSync(markerPath, JSON.stringify({
        kind: 'active-call', incarnation_id: 'resident-1',
      }));
      const rejected = invoke(config.mcpServers.call.args[0], { ...process.env, ...config.mcpServers.call.env }, {
        jsonrpc: '2.0', id: 2, method: 'tools/call', params: { name: 'emit_call_packet', arguments: { packet: {
          version: 1, packet_id: 'incomplete', project_id: 'project-1', call_session_id: 'call-1',
          call_generation: 1, request_id: 'request-1', input_revision: 0, kind: 'final', spoken_text: 'Result.',
        } } },
      });
      expect((rejected.result as { isError: boolean }).isError).toBe(true);
      expect(JSON.stringify(rejected)).toContain('originating_user_turn_ids');
      expect(fs.readdirSync(path.join(workspace, 'stage'))).toEqual([]);
      fs.writeFileSync(markerPath, JSON.stringify({
        kind: 'typed-consultation', incarnation_id: 'resident-1',
      }));
      const wrongTurn = invoke(config.mcpServers.call.args[0], { ...process.env, ...config.mcpServers.call.env }, {
        jsonrpc: '2.0', id: 3, method: 'tools/call', params: { name: 'emit_call_packet', arguments: { packet: {
          version: 1, packet_id: 'wrong-turn', project_id: 'project-1', call_session_id: 'call-1',
          call_generation: 1, request_id: 'request-1', originating_user_turn_ids: ['message-1'],
          input_revision: 0, context_sequence: 0, kind: 'final', spoken_text: 'Result.',
        } } },
      });
      expect((wrongTurn.result as { isError: boolean }).isError).toBe(true);
      expect(JSON.stringify(wrongTurn)).toContain('requires an active private call turn');
      fs.writeFileSync(markerPath, JSON.stringify({
        kind: 'active-call', incarnation_id: 'resident-1',
      }));
      invoke(config.mcpServers.call.args[0], { ...process.env, ...config.mcpServers.call.env }, {
        jsonrpc: '2.0', id: 4, method: 'tools/call', params: { name: 'emit_call_packet', arguments: { packet: {
          version: 1, packet_id: 'packet-1', project_id: 'project-1', call_session_id: 'call-1',
          call_generation: 1, request_id: 'request-1', originating_user_turn_ids: ['message-1'],
          input_revision: 0, context_sequence: 0, kind: 'final', spoken_text: 'Result.',
        } } },
      });
      const staged = fs.readdirSync(path.join(workspace, 'stage')).filter((name) => name.startsWith('packet-'));
      expect(staged).toHaveLength(1);
      expect(JSON.parse(fs.readFileSync(path.join(workspace, 'stage', staged[0]), 'utf8'))).toMatchObject({
        packet_id: 'packet-1', originating_user_turn_ids: ['message-1'], context_sequence: 0,
      });
    } finally { fs.rmSync(workspace, { recursive: true, force: true }); }
  });
});
