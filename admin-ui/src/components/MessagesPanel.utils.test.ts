import { describe, it, expect, beforeEach, afterEach } from 'vitest';
import { loadEventPrefs, saveEventPrefs, type EventPref, eventPrefsKey } from './MessagesPanel.utils';

describe('MessagesPanel.utils - Event Preferences', () => {
  beforeEach(() => {
    localStorage.clear();
  });

  afterEach(() => {
    localStorage.clear();
  });

  it('load with no key returns empty object', () => {
    const prefs = loadEventPrefs('user1', 'project1');
    expect(prefs).toEqual({});
  });

  it('save then load round-trips correctly', () => {
    const userId = 'user1';
    const projectId = 'project1';
    const testPrefs: Record<string, EventPref> = {
      pr_merged: 'mute',
      failure_alert: 'surfaced',
      cycle_summary: 'routine',
    };

    saveEventPrefs(userId, projectId, testPrefs);
    const loaded = loadEventPrefs(userId, projectId);

    expect(loaded).toEqual(testPrefs);
  });

  it('two project IDs hold independent prefs', () => {
    const userId = 'user1';
    const project1Prefs: Record<string, EventPref> = { pr_merged: 'mute' };
    const project2Prefs: Record<string, EventPref> = { pr_merged: 'surfaced' };

    saveEventPrefs(userId, 'project1', project1Prefs);
    saveEventPrefs(userId, 'project2', project2Prefs);

    const loaded1 = loadEventPrefs(userId, 'project1');
    const loaded2 = loadEventPrefs(userId, 'project2');

    expect(loaded1).toEqual(project1Prefs);
    expect(loaded2).toEqual(project2Prefs);
  });

  it('different users have independent prefs for same project', () => {
    const projectId = 'project1';
    const user1Prefs: Record<string, EventPref> = { pr_merged: 'mute' };
    const user2Prefs: Record<string, EventPref> = { pr_merged: 'surfaced' };

    saveEventPrefs('user1', projectId, user1Prefs);
    saveEventPrefs('user2', projectId, user2Prefs);

    const loaded1 = loadEventPrefs('user1', projectId);
    const loaded2 = loadEventPrefs('user2', projectId);

    expect(loaded1).toEqual(user1Prefs);
    expect(loaded2).toEqual(user2Prefs);
  });

  it('migration: loads from legacy global key if project-scoped key absent', () => {
    const legacyKey = 'messages_panel_event_prefs';
    const legacyPrefs: Record<string, EventPref> = { pr_merged: 'mute' };

    localStorage.setItem(legacyKey, JSON.stringify(legacyPrefs));

    const loaded = loadEventPrefs('user1', 'project1');
    expect(loaded).toEqual(legacyPrefs);

    // Verify legacy key is still there (not deleted during migration)
    expect(localStorage.getItem(legacyKey)).toBe(JSON.stringify(legacyPrefs));
  });

  it('migration: prefers project-scoped key over legacy key', () => {
    const legacyKey = 'messages_panel_event_prefs';
    const legacyPrefs: Record<string, EventPref> = { pr_merged: 'mute' };
    const projectPrefs: Record<string, EventPref> = { pr_merged: 'surfaced' };

    localStorage.setItem(legacyKey, JSON.stringify(legacyPrefs));
    saveEventPrefs('user1', 'project1', projectPrefs);

    const loaded = loadEventPrefs('user1', 'project1');
    expect(loaded).toEqual(projectPrefs);
  });

  it('save with empty prefs removes the key', () => {
    const userId = 'user1';
    const projectId = 'project1';

    saveEventPrefs(userId, projectId, { pr_merged: 'mute' });
    expect(localStorage.getItem(eventPrefsKey(userId, projectId))).not.toBeNull();

    saveEventPrefs(userId, projectId, {});
    expect(localStorage.getItem(eventPrefsKey(userId, projectId))).toBeNull();
  });

  it('handles undefined userId and projectId gracefully', () => {
    const testPrefs: Record<string, EventPref> = { pr_merged: 'mute' };

    saveEventPrefs(undefined, undefined, testPrefs);
    const loaded = loadEventPrefs(undefined, undefined);

    expect(loaded).toEqual(testPrefs);
  });

  it('handles null userId and projectId gracefully', () => {
    const testPrefs: Record<string, EventPref> = { pr_merged: 'mute' };

    saveEventPrefs(null, null, testPrefs);
    const loaded = loadEventPrefs(null, null);

    expect(loaded).toEqual(testPrefs);
  });

  it('handles invalid JSON gracefully', () => {
    const key = eventPrefsKey('user1', 'project1');
    localStorage.setItem(key, 'invalid json {');

    const loaded = loadEventPrefs('user1', 'project1');
    expect(loaded).toEqual({});
  });
});
