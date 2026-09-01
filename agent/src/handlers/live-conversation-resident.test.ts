import { describe, expect, it, vi } from 'vitest';
import fs from 'fs';
import os from 'os';
import path from 'path';
import {
  CallCoordinatorState, CallPacketStaging, parseCallLeaseProjection,
  CallCoordinatorWakeGate, FastSpeechTextBuffer, ForegroundCallController, callLeaseIsPubliclyActive, parseCallTransportProjection, validateCallWorkPacket,
  callLeaseKey, shouldImmediatelyReconcileCallMedia, speechOriginIsCurrent,
  type CallWorkPacket,
} from './live-conversation-resident.js';
import type { ForegroundResidentAdapter } from '../providers/foreground-resident.js';

const lease = {
  call_id: 'call-1', generation: 3, status: 'active', input_revision: 7,
  expires_at: '2099-01-01T00:00:00Z', adapter: 'claude',
};

const packet: CallWorkPacket = {
  version: 1, packet_id: 'packet-1', project_id: 'project-1', call_session_id: 'call-1',
  call_generation: 3, request_id: 'request-1', originating_user_turn_ids: ['message-1'],
  input_revision: 7, context_sequence: 42, kind: 'final', spoken_text: 'The conclusion holds.',
  exact_text: 'E[X] = 0', display_blocks: [{ type: 'latex', content: 'E[X] = 0' }],
};

describe('live conversation coordinator', () => {
  it('paces same-generation media failures and only collapses lease races', () => {
    expect(shouldImmediatelyReconcileCallMedia('call-1:3', 'call-1:3', null)).toBe(false);
    expect(shouldImmediatelyReconcileCallMedia('call-1:3', 'call-1:4', null)).toBe(true);
    expect(shouldImmediatelyReconcileCallMedia('call-1:3', 'call-1:4', 'call-1:4')).toBe(false);
    expect(shouldImmediatelyReconcileCallMedia(null, null, null)).toBe(false);
  });

  it('rejects late audio and finalized STT from a superseded speech generation', () => {
    const callA = parseCallLeaseProjection(lease);
    const callB = parseCallLeaseProjection({ ...lease, generation: 4 });
    const originA = callLeaseKey(callA);
    expect(originA).toBe('call-1:3');
    expect(speechOriginIsCurrent(originA ?? '', callA, originA)).toBe(true);
    // Both late TTS audio and late finalized STT use this exact pipeline fence.
    expect(speechOriginIsCurrent(originA ?? '', callB, callLeaseKey(callB))).toBe(false);
    expect(speechOriginIsCurrent(originA ?? '', null, null)).toBe(false);
  });

  it('treats only a fresh typed lease projection as public ownership', () => {
    expect(parseCallLeaseProjection(lease)).toMatchObject({ callId: 'call-1', generation: 3 });
    expect(parseCallLeaseProjection({ ...lease, expires_at: '2000-01-01T00:00:00Z' })).toBeNull();
    expect(parseCallLeaseProjection({ ...lease, generation: 0 })).toBeNull();
  });

  it('revokes in-memory call authority when a previously fresh lease expires', () => {
    // Use fake timers so the active→expired transition is deterministic and not
    // dependent on wall-clock execution speed (the previous 20ms margin was flaky).
    const frozenNow = 1_700_000_000_000;
    vi.useFakeTimers();
    vi.setSystemTime(frozenNow);
    try {
      const parsed = parseCallLeaseProjection({
        ...lease,
        expires_at: new Date(frozenNow + 60_000).toISOString(),
      });
      expect(parsed).not.toBeNull();
      expect(callLeaseIsPubliclyActive(parsed)).toBe(true);
      // Advance frozen time past the lease expiry — same object now reads as stale.
      vi.setSystemTime(frozenNow + 120_000);
      expect(callLeaseIsPubliclyActive(parsed)).toBe(false);
      const coordinator = new CallCoordinatorState();
      coordinator.updateLease(parsed);
      expect(() => coordinator.beginPublicTurn(['message-1'], 42)).toThrow('no active call lease');
    } finally {
      vi.useRealTimers();
    }
  });

  it('retains the short-lived transport projection without exposing it to model state', () => {
    const transport = parseCallTransportProjection({
      server_url: 'wss://live.example.test', participant_token: 'short-lived',
      room: 'opaque-room', participant_identity: 'coordinator', operator_identity: 'operator-1',
    });
    const coordinator = new CallCoordinatorState();
    coordinator.updateControlProjection(parseCallLeaseProjection(lease), transport);
    expect(coordinator.currentTransport()).toMatchObject({ room: 'opaque-room' });
    coordinator.updateControlProjection(null, transport);
    expect(coordinator.currentTransport()).toBeNull();
  });

  it('fences duplicate, stale-generation, and stale-input packets before they reach Fast', () => {
    const coordinator = new CallCoordinatorState();
    coordinator.updateLease(parseCallLeaseProjection(lease));
    expect(coordinator.acceptWorkPacket(packet, 'project-1')).toMatchObject({ packet_id: 'packet-1' });
    expect(coordinator.acceptWorkPacket(packet, 'project-1')).toBeNull();
    expect(coordinator.acceptWorkPacket({ ...packet, packet_id: 'packet-2', call_generation: 2 }, 'project-1')).toBeNull();
    expect(coordinator.acceptWorkPacket({ ...packet, packet_id: 'packet-3', input_revision: 6 }, 'project-1')).toBeNull();
  });

  it('drops an old Fast completion after a lease revision changes and retains only heard playout', () => {
    const coordinator = new CallCoordinatorState();
    coordinator.updateLease(parseCallLeaseProjection(lease));
    const receipt = coordinator.beginPublicTurn(['message-1'], 42);
    expect(coordinator.interruptPlayback('The con')).toBe(1);
    expect(coordinator.playbackState()).toEqual({ revision: 1, heardPrefix: 'The con' });
    coordinator.updateLease(parseCallLeaseProjection({ ...lease, input_revision: 8 }));
    expect(coordinator.completionIsCurrent(receipt)).toBe(false);
  });

  it('rejects URL evidence and durably stages only final/blocked packets', () => {
    expect(() => validateCallWorkPacket({ ...packet, evidence_refs: ['https://example.test/x'] })).toThrow(/URLs/);
    const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'call-packet-stage-'));
    try {
      const staging = new CallPacketStaging(directory);
      expect(staging.stage({ ...packet, kind: 'progress' })).toBeNull();
      const staged = staging.stage(packet);
      expect(staged).not.toBeNull();
      expect(staging.read(staged as string)).toMatchObject({ exact_text: 'E[X] = 0' });
    } finally {
      fs.rmSync(directory, { recursive: true, force: true });
    }
  });

  it('replaces a crashed Fast child and accepts only receipt-bound completion', async () => {
    const runtime = fs.mkdtempSync(path.join(os.tmpdir(), 'foreground-call-'));
    let starts = 0;
    const adapter: ForegroundResidentAdapter = {
      id: 'fake', validation: 'qualified',
      start({ mcpConfigPath }) {
        starts += 1;
        const config = JSON.parse(fs.readFileSync(mcpConfigPath, 'utf8')) as {
          mcpServers: { call: { env: { CALL_TURN_RECEIPT_PATH: string; CALL_LOCAL_STAGE_DIR: string } } };
        };
        const env = config.mcpServers.call.env;
        const result = { processGone: starts === 1 } as Awaited<ReturnType<import('../providers/resident-session.js').ResidentSession['sendTurn']>>;
        return {
          isAlive: () => true,
          close: () => undefined,
          onEvent: () => undefined,
          sendTurn: () => {
            if (starts > 1) {
              const receipt = JSON.parse(fs.readFileSync(env.CALL_TURN_RECEIPT_PATH, 'utf8')) as Record<string, unknown>;
              fs.writeFileSync(path.join(env.CALL_LOCAL_STAGE_DIR, 'complete-test.json'), JSON.stringify({
                receipt, spoken_text: 'Fenced completion.', durable: true,
              }));
            }
            return Promise.resolve(result);
          },
        } as unknown as import('../providers/resident-session.js').ResidentSession;
      },
    };
    const controller = new ForegroundCallController({ adapter, runtimeRoot: runtime, environment: { PATH: process.env.PATH } });
    try {
      const coordinator = new CallCoordinatorState();
      coordinator.updateLease(parseCallLeaseProjection(lease));
      const receipt = coordinator.beginPublicTurn(['message-1'], 42);
      let turn = await controller.dispatch(receipt, 'hello');
      expect(turn.result.processGone).toBe(true);
      controller.replaceAfterCrash();
      turn = await controller.dispatch(receipt, 'hello again');
      expect(turn.completion?.spoken_text).toBe('Fenced completion.');
      expect(starts).toBe(2);
    } finally {
      controller.close();
      fs.rmSync(runtime, { recursive: true, force: true });
    }
  });

  it('cannot bind a late completion from an interrupted Fast child to the next turn', async () => {
    const runtime = fs.mkdtempSync(path.join(os.tmpdir(), 'foreground-stale-turn-'));
    const launches: Array<{ receiptPath: string; stageDir: string }> = [];
    let turnCalls = 0;
    const adapter: ForegroundResidentAdapter = {
      id: 'fake', validation: 'qualified',
      start({ mcpConfigPath }) {
        const config = JSON.parse(fs.readFileSync(mcpConfigPath, 'utf8')) as {
          mcpServers: { call: { env: { CALL_TURN_RECEIPT_PATH: string; CALL_LOCAL_STAGE_DIR: string } } };
        };
        const env = config.mcpServers.call.env;
        launches.push({ receiptPath: env.CALL_TURN_RECEIPT_PATH, stageDir: env.CALL_LOCAL_STAGE_DIR });
        return {
          isAlive: () => true,
          close: () => undefined,
          onEvent: () => undefined,
          sendTurn: () => {
            turnCalls += 1;
            if (turnCalls === 1) {
              const receipt = JSON.parse(fs.readFileSync(env.CALL_TURN_RECEIPT_PATH, 'utf8')) as Record<string, unknown>;
              fs.mkdirSync(env.CALL_LOCAL_STAGE_DIR, { recursive: true });
              fs.writeFileSync(path.join(env.CALL_LOCAL_STAGE_DIR, 'complete-early-a.json'), JSON.stringify({
                receipt, spoken_text: 'Abandoned A completion.', durable: true,
              }));
              return Promise.resolve({ interrupted: true, timedOut: false });
            }
            // B's receipt is installed now. Simulate A's still-alive MCP call
            // completing late, before B writes its own accepted completion.
            const staleLaunch = launches[0];
            const staleReceipt = JSON.parse(fs.readFileSync(staleLaunch.receiptPath, 'utf8')) as Record<string, unknown>;
            fs.mkdirSync(staleLaunch.stageDir, { recursive: true });
            fs.writeFileSync(path.join(staleLaunch.stageDir, 'complete-00-late-a.json'), JSON.stringify({
              receipt: staleReceipt, spoken_text: 'Stale A completion.', durable: true,
            }));
            const currentReceipt = JSON.parse(fs.readFileSync(env.CALL_TURN_RECEIPT_PATH, 'utf8')) as Record<string, unknown>;
            fs.mkdirSync(env.CALL_LOCAL_STAGE_DIR, { recursive: true });
            fs.writeFileSync(path.join(env.CALL_LOCAL_STAGE_DIR, 'complete-10-current-b.json'), JSON.stringify({
              receipt: currentReceipt, spoken_text: 'Current B completion.', durable: true,
            }));
            return Promise.resolve({ interrupted: false, timedOut: false });
          },
        } as unknown as import('../providers/resident-session.js').ResidentSession;
      },
    };
    const controller = new ForegroundCallController({
      adapter, runtimeRoot: runtime, environment: { PATH: process.env.PATH },
    });
    try {
      const coordinator = new CallCoordinatorState();
      coordinator.updateLease(parseCallLeaseProjection(lease));
      const turnA = coordinator.beginPublicTurn(['message-a'], 41);
      const resultA = await controller.dispatch(turnA, 'turn A');
      expect(resultA.completion).toBeNull();
      const turnB = coordinator.beginPublicTurn(['message-b'], 42);
      const resultB = await controller.dispatch(turnB, 'turn B');
      expect(resultB.completion?.spoken_text).toBe('Current B completion.');
      expect(launches).toHaveLength(2);
      expect(launches[0].receiptPath).not.toBe(launches[1].receiptPath);
      expect(launches[0].stageDir).not.toBe(launches[1].stageDir);
    } finally {
      controller.close();
      fs.rmSync(runtime, { recursive: true, force: true });
    }
  });

  it('lets authenticated media/speech wake the coordinator without waiting for the poll timeout', async () => {
    const gate = new CallCoordinatorWakeGate();
    const waiting = gate.wait(10_000);
    gate.notify();
    await expect(waiting).resolves.toBe('wake');
    gate.close();
  });

  it('refuses an unbenchmarked Fast adapter outside an explicit test override', async () => {
    const runtime = fs.mkdtempSync(path.join(os.tmpdir(), 'foreground-gate-'));
    const controller = new ForegroundCallController({
      adapter: { id: 'not-benchmarked', validation: 'unvalidated', start: () => { throw new Error('must not start'); } },
      runtimeRoot: runtime,
    });
    const coordinator = new CallCoordinatorState();
    coordinator.updateLease(parseCallLeaseProjection(lease));
    await expect(controller.dispatch(coordinator.beginPublicTurn(['message-1'], 42), 'hello'))
      .rejects.toThrow('not qualification-approved');
    controller.close();
    fs.rmSync(runtime, { recursive: true, force: true });
  });

  it('releases Fast text only at sentence or bounded phrase boundaries for TTS', () => {
    const buffer = new FastSpeechTextBuffer();
    expect(buffer.push('The proof works because ')).toEqual([]);
    expect(buffer.push('the error is bounded. Next,')).toEqual(['The proof works because the error is bounded.']);
    expect(buffer.flush()).toBe('Next,');
  });
});
