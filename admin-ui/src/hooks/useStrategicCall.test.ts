import { act, cleanup, renderHook, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, test, vi } from 'vitest';
import { asTrustedOperatorCaption, isTrustedCallCaption, isTrustedCallPacket, isTrustedCallReadyPacket, parseTrustedOperatorCaption, useStrategicCall } from './useStrategicCall';
import { endStrategicCall, fetchStrategicCall, startStrategicCall } from '../strategicStateApi';

const rtc = vi.hoisted(() => ({
  connect: vi.fn<(...args: unknown[]) => Promise<void>>(),
  disconnect: vi.fn(),
  startAudio: vi.fn<() => Promise<void>>(),
  setMicrophoneEnabled: vi.fn<(...args: unknown[]) => Promise<void>>(),
}));

vi.mock('livekit-client', () => {
  class Room {
    localParticipant = { setMicrophoneEnabled: rtc.setMicrophoneEnabled };
    canPlaybackAudio = true;
    on() { return this; }
    connect(...args: unknown[]) { return rtc.connect(...args); }
    disconnect() { rtc.disconnect(); }
    startAudio() { return rtc.startAudio(); }
  }
  class RemoteAudioTrack {
    detach() { return undefined; }
  }
  return {
    Room,
    RemoteAudioTrack,
    RoomEvent: {
      TrackSubscribed: 'track-subscribed', TrackUnsubscribed: 'track-unsubscribed',
      ConnectionStateChanged: 'connection-state', AudioPlaybackStatusChanged: 'audio-playback',
      TranscriptionReceived: 'transcription', DataReceived: 'data',
    },
    Track: { Kind: { Audio: 'audio' } },
  };
});

vi.mock('../strategicStateApi', async () => {
  const { createStrategicStateApiMock } = await import('../test/strategic-state-api-mock-complete');
  return createStrategicStateApiMock();
});

const mockStartStrategicCall = vi.mocked(startStrategicCall);
const mockFetchStrategicCall = vi.mocked(fetchStrategicCall);
const mockEndStrategicCall = vi.mocked(endStrategicCall);

const activeCall = { call_id: 'call-1', generation: 4, status: 'active' };
const endedCall = { ...activeCall, status: 'ended' };
const connection = {
  call: activeCall,
  server_url: 'wss://livekit.example.test',
  participant_token: 'browser-token',
  agent_identity: 'coordinator-opaque',
};

beforeEach(() => {
  rtc.connect.mockResolvedValue(undefined);
  rtc.startAudio.mockResolvedValue(undefined);
  rtc.setMicrophoneEnabled.mockResolvedValue(undefined);
  mockFetchStrategicCall.mockResolvedValue(null);
  mockEndStrategicCall.mockResolvedValue({ call: endedCall });
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

const call = { call_id: 'call-1', generation: 4, status: 'active' };

describe('isTrustedCallPacket', () => {
  test('accepts only the issued agent participant and current generation', () => {
    expect(isTrustedCallPacket(
      { v: 1, call_id: 'call-1', generation: 4 },
      'coordinator-opaque',
      call,
      'coordinator-opaque',
    )).toBe(true);
  });

  test('ignores forged browser data even when its packet fields look valid', () => {
    expect(isTrustedCallPacket(
      { v: 1, call_id: 'call-1', generation: 4, type: 'blueprint_call_display_blocks' },
      'operator-browser',
      call,
      'coordinator-opaque',
    )).toBe(false);
  });

  test('rejects stale generation and responses that omit an agent identity', () => {
    expect(isTrustedCallPacket({ v: 1, call_id: 'call-1', generation: 3 }, 'coordinator-opaque', call, 'coordinator-opaque')).toBe(false);
    expect(isTrustedCallPacket({ v: 1, call_id: 'call-1', generation: 4 }, 'coordinator-opaque', call, null)).toBe(false);
    expect(isTrustedCallPacket({ call_id: 'call-1', generation: 4 }, 'coordinator-opaque', call, 'coordinator-opaque')).toBe(false);
  });

  test('accepts native transcription only from the issued coordinator', () => {
    expect(isTrustedCallCaption('coordinator-opaque', 'coordinator-opaque')).toBe(true);
    expect(isTrustedCallCaption('operator-browser', 'coordinator-opaque')).toBe(false);
    expect(isTrustedCallCaption('coordinator-opaque', null)).toBe(false);
  });

  test('admits a bounded operator partial caption only after the same agent and generation fence', () => {
    const packet = {
      v: 1,
      type: 'blueprint_call_operator_caption',
      call_id: 'call-1',
      generation: 4,
      segment_id: 'turn-8',
      text: 'Let eta be small.',
      final: false,
    };
    expect(parseTrustedOperatorCaption(packet, 'coordinator-opaque', call, 'coordinator-opaque')).toEqual({
      id: 'operator:turn-8', text: 'Let eta be small.', final: false, speaker: 'operator',
    });
  });

  test('does not treat forged or stale caption-shaped data as an operator caption', () => {
    const packet = {
      v: 1,
      type: 'blueprint_call_operator_caption',
      call_id: 'call-1',
      generation: 4,
      segment_id: 'turn-8',
      text: 'forged text',
      final: false,
    };
    expect(parseTrustedOperatorCaption(packet, 'operator-browser', call, 'coordinator-opaque')).toBeNull();
    expect(parseTrustedOperatorCaption({ ...packet, generation: 3 }, 'coordinator-opaque', call, 'coordinator-opaque')).toBeNull();
  });

  test('rejects oversized or malformed trusted-agent caption payloads', () => {
    expect(asTrustedOperatorCaption({
      type: 'blueprint_call_operator_caption', segment_id: 'turn-8', text: 'x'.repeat(4_001), final: true,
    })).toBeNull();
    expect(asTrustedOperatorCaption({
      type: 'blueprint_call_operator_caption', segment_id: 'turn-8', text: 'not final', final: 'true',
    })).toBeNull();
  });

  test('admits readiness only from the exact issued coordinator and active generation', () => {
    const ready = { v: 1, type: 'blueprint_call_ready', call_id: 'call-1', generation: 4 };
    expect(isTrustedCallReadyPacket(ready, 'coordinator-opaque', call, 'coordinator-opaque')).toBe(true);
    expect(isTrustedCallReadyPacket(ready, 'operator-browser', call, 'coordinator-opaque')).toBe(false);
    expect(isTrustedCallReadyPacket({ ...ready, generation: 3 }, 'coordinator-opaque', call, 'coordinator-opaque')).toBe(false);
    expect(isTrustedCallReadyPacket({ ...ready, type: 'blueprint_call_display_blocks' }, 'coordinator-opaque', call, 'coordinator-opaque')).toBe(false);
  });
});

describe('useStrategicCall failed-start cleanup', () => {
  test('fences and ends the lease newly acquired by a failed RTC connection', async () => {
    mockStartStrategicCall.mockResolvedValue(connection);
    rtc.connect.mockRejectedValue(new Error('RTC unavailable'));
    const { result } = renderHook(() => useStrategicCall('project-1'));

    await waitFor(() => expect(mockFetchStrategicCall).toHaveBeenCalled());
    await act(async () => { await result.current.start(); });

    expect(mockEndStrategicCall).toHaveBeenCalledWith('project-1', {
      call_id: 'call-1', generation: 4,
    });
    expect(result.current.call).toEqual(endedCall);
    expect(result.current.connectionState).toBe('error');
    expect(result.current.error).toBe('RTC unavailable');
  });

  test('fences and ends the lease newly acquired when microphone activation fails', async () => {
    mockStartStrategicCall.mockResolvedValue(connection);
    rtc.setMicrophoneEnabled.mockRejectedValue(new Error('Microphone denied'));
    const { result } = renderHook(() => useStrategicCall('project-1'));

    await waitFor(() => expect(mockFetchStrategicCall).toHaveBeenCalled());
    await act(async () => { await result.current.start(); });

    expect(mockEndStrategicCall).toHaveBeenCalledWith('project-1', {
      call_id: 'call-1', generation: 4,
    });
    expect(result.current.call).toEqual(endedCall);
    expect(result.current.microphoneState).toBe('denied');
  });

  test('does not end an existing lease when reconnect fails', async () => {
    const { result } = renderHook(() => useStrategicCall('project-1'));
    await waitFor(() => expect(mockFetchStrategicCall).toHaveBeenCalled());
    mockFetchStrategicCall.mockResolvedValue(connection);
    rtc.connect.mockRejectedValue(new Error('RTC reconnect unavailable'));

    await act(async () => { await result.current.reconnect(); });

    expect(mockEndStrategicCall).not.toHaveBeenCalled();
    expect(result.current.call).toEqual(activeCall);
    expect(result.current.connectionState).toBe('error');
  });
});
