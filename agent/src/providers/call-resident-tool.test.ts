import { describe, expect, it } from 'vitest';
import { execFileSync } from 'child_process';
import fs from 'fs';
import os from 'os';
import path from 'path';
import {
  CallCoordinatorState, activeCallPacketAuthority, parseCallLeaseProjection,
} from '../handlers/live-conversation-resident.js';
import { provisionCallResidentTool } from './call-resident-tool.js';

function invoke(script: string, env: NodeJS.ProcessEnv, request: unknown): Record<string, unknown> {
  const parsed: unknown = JSON.parse(execFileSync(process.execPath, [script], {
    input: `${JSON.stringify(request)}\n`, encoding: 'utf8', env,
  }));
  if (parsed === null || typeof parsed !== 'object' || Array.isArray(parsed)) {
    throw new Error('local Call Resident MCP returned a non-object response');
  }
  return parsed as Record<string, unknown>;
}

describe('local Call Resident MCP tools', () => {
  it('exposes no Workday/public reply tool to Fast and stages only receipt-bound completion', () => {
    const workspace = fs.mkdtempSync(path.join(os.tmpdir(), 'call-mcp-'));
    const receiptPath = path.join(workspace, 'receipt.json');
    const stageDir = path.join(workspace, 'stage');
    fs.writeFileSync(receiptPath, JSON.stringify({
      call_id: 'call', call_generation: 1, input_revision: 2,
      public_input_message_id: 'message-1', originating_user_turn_ids: ['message-1'],
      context_sequence: 42, idempotency_key: 'key',
    }));
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
      expect(packetSchema.required).toEqual(['packet_id', 'kind', 'spoken_text']);
      expect(packetSchema.additionalProperties).toBe(false);
      expect(packetSchema.properties).not.toHaveProperty('project_id');
      expect(packetSchema.properties).not.toHaveProperty('request_id');
      const missingMarker = invoke(config.mcpServers.call.args[0], {
        ...process.env, ...config.mcpServers.call.env,
      }, {
        jsonrpc: '2.0', id: 2, method: 'tools/call', params: {
          name: 'emit_call_packet', arguments: { packet: {
            packet_id: 'missing-marker', kind: 'final', spoken_text: 'Result.',
          } },
        },
      });
      expect((missingMarker.result as { isError: boolean }).isError).toBe(true);
      expect(JSON.stringify(missingMarker)).toContain('requires an active private call turn');
      expect(JSON.stringify(missingMarker)).not.toContain('ENOENT');
      expect(fs.readdirSync(path.join(workspace, 'stage'))).toEqual([]);
      fs.writeFileSync(markerPath, JSON.stringify({
        kind: 'active-call', incarnation_id: 'resident-1',
        project_id: 'project-1', call_session_id: 'call-1', call_generation: 1,
        request_id: 'request-1', originating_user_turn_ids: ['message-1'],
        input_revision: 0, context_sequence: 42,
      }));
      const rejected = invoke(config.mcpServers.call.args[0], { ...process.env, ...config.mcpServers.call.env }, {
        jsonrpc: '2.0', id: 2, method: 'tools/call', params: { name: 'emit_call_packet', arguments: { packet: {
          packet_id: 'authority-injection', project_id: 'wrong-project',
          kind: 'final', spoken_text: 'Result.',
        } } },
      });
      expect((rejected.result as { isError: boolean }).isError).toBe(true);
      expect(JSON.stringify(rejected)).toContain('authority is Coordinator-owned');
      expect(fs.readdirSync(path.join(workspace, 'stage'))).toEqual([]);
      fs.writeFileSync(markerPath, JSON.stringify({
        kind: 'typed-consultation', incarnation_id: 'resident-1',
      }));
      const wrongTurn = invoke(config.mcpServers.call.args[0], { ...process.env, ...config.mcpServers.call.env }, {
        jsonrpc: '2.0', id: 3, method: 'tools/call', params: { name: 'emit_call_packet', arguments: { packet: {
          packet_id: 'wrong-turn', kind: 'final', spoken_text: 'Result.',
        } } },
      });
      expect((wrongTurn.result as { isError: boolean }).isError).toBe(true);
      expect(JSON.stringify(wrongTurn)).toContain('requires an active private call turn');
      fs.writeFileSync(markerPath, JSON.stringify({
        kind: 'active-call', incarnation_id: 'resident-1',
        project_id: 'project-1', call_session_id: 'call-1', call_generation: 1,
        request_id: 'request-1', originating_user_turn_ids: ['message-1'],
        input_revision: 0, context_sequence: 42,
      }));
      invoke(config.mcpServers.call.args[0], { ...process.env, ...config.mcpServers.call.env }, {
        jsonrpc: '2.0', id: 4, method: 'tools/call', params: { name: 'emit_call_packet', arguments: { packet: {
          packet_id: 'packet-1', kind: 'final', spoken_text: 'Result.',
        } } },
      });
      const staged = fs.readdirSync(path.join(workspace, 'stage')).filter((name) => name.startsWith('packet-'));
      expect(staged).toHaveLength(1);
      expect(JSON.parse(fs.readFileSync(path.join(workspace, 'stage', staged[0]), 'utf8'))).toMatchObject({
        version: 1, packet_id: 'packet-1', project_id: 'project-1', call_session_id: 'call-1',
        call_generation: 1, request_id: 'request-1',
        originating_user_turn_ids: ['message-1'], input_revision: 0, context_sequence: 42,
      });
    } finally { fs.rmSync(workspace, { recursive: true, force: true }); }
  });

  it('stages at most one private Workday request for an immutable Fast turn receipt', () => {
    const workspace = fs.mkdtempSync(path.join(os.tmpdir(), 'call-request-once-'));
    const receiptPath = path.join(workspace, 'receipt.json');
    const stageDir = path.join(workspace, 'stage');
    fs.writeFileSync(receiptPath, JSON.stringify({
      call_id: 'call-once', call_generation: 1, input_revision: 2,
      public_input_message_id: 'message-once', originating_user_turn_ids: ['message-once'],
      context_sequence: 42, idempotency_key: 'immutable-turn-key',
    }));
    try {
      const tool = provisionCallResidentTool(workspace, 'fast', { receiptPath, stageDir });
      const config = JSON.parse(fs.readFileSync(tool.configPath, 'utf8')) as {
        mcpServers: { call: { args: string[]; env: NodeJS.ProcessEnv } };
      };
      const call = (id: number, requestId: string): Record<string, unknown> => invoke(
        config.mcpServers.call.args[0],
        { ...process.env, ...config.mcpServers.call.env },
        { jsonrpc: '2.0', id, method: 'tools/call', params: {
          name: 'request_workday',
          arguments: { request_id: requestId, prompt: `Private request ${requestId}` },
        } },
      );

      expect(JSON.stringify(call(1, 'model-request-1'))).toContain('accepted');
      const duplicate = call(2, 'model-request-2');
      expect((duplicate.result as { isError: boolean }).isError).toBe(true);
      expect(fs.readdirSync(stageDir).filter((name) => name.startsWith('request-')))
        .toHaveLength(1);
    } finally { fs.rmSync(workspace, { recursive: true, force: true }); }
  });

  it('binds a Fast request_workday receipt into a Coordinator-accepted Workday packet', () => {
    const workspace = fs.mkdtempSync(path.join(os.tmpdir(), 'call-bound-packet-'));
    try {
      const coordinator = new CallCoordinatorState();
      coordinator.updateLease(parseCallLeaseProjection({
        call_id: 'call-bound', generation: 4, status: 'active', input_revision: 7,
        expires_at: '2099-01-01T00:00:00Z', adapter: 'claude',
      }));
      const receipt = coordinator.beginPublicTurn(['message-bound'], 42);
      const receiptPath = path.join(workspace, 'fast-receipt.json');
      const fastStageDir = path.join(workspace, 'fast-stage');
      fs.writeFileSync(receiptPath, JSON.stringify(receipt));
      const fastTool = provisionCallResidentTool(workspace, 'fast', {
        receiptPath, stageDir: fastStageDir,
      });
      const fastConfig = JSON.parse(fs.readFileSync(fastTool.configPath, 'utf8')) as {
        mcpServers: { call: { args: string[]; env: NodeJS.ProcessEnv } };
      };
      invoke(fastConfig.mcpServers.call.args[0], {
        ...process.env, ...fastConfig.mcpServers.call.env,
      }, {
        jsonrpc: '2.0', id: 1, method: 'tools/call', params: {
          name: 'request_workday',
          arguments: { request_id: 'request-bound', prompt: 'Prove the bounded result.' },
        },
      });
      const requestName = fs.readdirSync(fastStageDir).find((name) => name.startsWith('request-'));
      expect(requestName).toBeTruthy();
      const stagedRequest = JSON.parse(fs.readFileSync(
        path.join(fastStageDir, requestName as string), 'utf8',
      )) as Parameters<typeof activeCallPacketAuthority>[0];
      const markerPath = path.join(workspace, 'active-call-marker.json');
      fs.writeFileSync(markerPath, JSON.stringify({
        kind: 'active-call', incarnation_id: 'resident-bound',
        ...activeCallPacketAuthority(stagedRequest, 'project-bound'),
      }));

      const workdayRoot = path.join(workspace, 'workday');
      fs.mkdirSync(workdayRoot);
      const workdayStageDir = path.join(workspace, 'workday-stage');
      const workdayTool = provisionCallResidentTool(workdayRoot, 'workday', {
        stageDir: workdayStageDir, privateTurnMarkerPath: markerPath,
      });
      const workdayConfig = JSON.parse(fs.readFileSync(workdayTool.configPath, 'utf8')) as {
        mcpServers: { call: { args: string[]; env: NodeJS.ProcessEnv } };
      };
      invoke(workdayConfig.mcpServers.call.args[0], {
        ...process.env, ...workdayConfig.mcpServers.call.env,
      }, {
        jsonrpc: '2.0', id: 2, method: 'tools/call', params: {
          name: 'emit_call_packet',
          arguments: { packet: {
            packet_id: 'packet-bound', kind: 'final', spoken_text: 'Bound result.',
          } },
        },
      });
      const packetName = fs.readdirSync(workdayStageDir).find((name) => name.startsWith('packet-'));
      expect(packetName).toBeTruthy();
      const stagedPacket: unknown = JSON.parse(fs.readFileSync(
        path.join(workdayStageDir, packetName as string), 'utf8',
      ));
      expect(coordinator.acceptWorkPacket(stagedPacket, 'project-bound')).toMatchObject({
        packet_id: 'packet-bound', project_id: 'project-bound',
        call_session_id: 'call-bound', call_generation: 4,
        request_id: 'request-bound', originating_user_turn_ids: ['message-bound'],
        input_revision: 7, context_sequence: 42,
      });
    } finally { fs.rmSync(workspace, { recursive: true, force: true }); }
  });
});
