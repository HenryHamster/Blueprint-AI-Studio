import { describe, it, expect, beforeEach } from 'vitest';
import {
  type BoardSavedView,
  BOARD_SAVED_VIEWS_CAP,
  boardSavedViewsKey,
  loadBoardSavedViews,
  saveBoardSavedViews,
} from './boardSavedViews';

function makeView(overrides: Partial<BoardSavedView> = {}): BoardSavedView {
  return {
    id: 'v1',
    name: 'My View',
    query: 'auth',
    taskTypeFilter: ['create', 'fix'],
    statusFilter: ['FAILED'],
    dateRange: '24h',
    blocked: true,
    requiresApproval: false,
    stuck: true,
    stuckMinSeconds: 3600,
    failureClassFilter: ['CODE_ERROR'],
    sortBy: 'oldest',
    createdAt: 1000,
    ...overrides,
  };
}

describe('boardSavedViews', () => {
  beforeEach(() => {
    localStorage.clear();
  });

  it('namespaces the storage key by user and project', () => {
    expect(boardSavedViewsKey('alice@x.com', 'proj1')).toBe('board_saved_views:alice@x.com:proj1');
    expect(boardSavedViewsKey(null, null)).toBe('board_saved_views:anonymous:default');
  });

  it('returns an empty array when nothing is stored', () => {
    expect(loadBoardSavedViews('u', 'p')).toEqual([]);
  });

  it('round-trips a saved view with all fields preserved', () => {
    const view = makeView();
    saveBoardSavedViews('u', 'p', [view]);
    const loaded = loadBoardSavedViews('u', 'p');
    expect(loaded).toHaveLength(1);
    expect(loaded[0]).toEqual(view);
  });

  it('persists as a version-wrapped file', () => {
    saveBoardSavedViews('u', 'p', [makeView()]);
    const stored = localStorage.getItem(boardSavedViewsKey('u', 'p'));
    expect(stored).not.toBeNull();
    const raw = JSON.parse(stored ?? '');
    expect(raw.version).toBe(1);
    expect(Array.isArray(raw.views)).toBe(true);
  });

  it('removes the key when saving an empty list', () => {
    saveBoardSavedViews('u', 'p', [makeView()]);
    saveBoardSavedViews('u', 'p', []);
    expect(localStorage.getItem(boardSavedViewsKey('u', 'p'))).toBeNull();
  });

  it('falls back to an empty array on malformed JSON', () => {
    localStorage.setItem(boardSavedViewsKey('u', 'p'), '{not json');
    expect(loadBoardSavedViews('u', 'p')).toEqual([]);
  });

  it('drops entries missing id or name', () => {
    const file = { version: 1, views: [makeView(), { ...makeView({ id: 'v2' }), name: '' }, { name: 'no id' }] };
    localStorage.setItem(boardSavedViewsKey('u', 'p'), JSON.stringify(file));
    const loaded = loadBoardSavedViews('u', 'p');
    expect(loaded).toHaveLength(1);
    expect(loaded[0].id).toBe('v1');
  });

  it('coerces invalid field values to safe defaults', () => {
    const file = {
      version: 1,
      views: [{
        id: 'v1',
        name: 'bad fields',
        query: 42,
        taskTypeFilter: 'not-an-array',
        statusFilter: ['QUEUED', 7, null],
        dateRange: 'nonsense',
        blocked: 'yes',
        stuckMinSeconds: -5,
        failureClassFilter: null,
        sortBy: 'bogus',
        createdAt: 'soon',
      }],
    };
    localStorage.setItem(boardSavedViewsKey('u', 'p'), JSON.stringify(file));
    const [v] = loadBoardSavedViews('u', 'p');
    expect(v.query).toBe('');
    expect(v.taskTypeFilter).toEqual([]);
    expect(v.statusFilter).toEqual(['QUEUED']); // non-string entries filtered out
    expect(v.dateRange).toBe('all');
    expect(v.blocked).toBe(false);
    expect(v.stuckMinSeconds).toBe(0);
    expect(v.failureClassFilter).toEqual([]);
    expect(v.sortBy).toBe('newest');
    expect(typeof v.createdAt).toBe('number');
  });

  it('reads a bare array payload (no version wrapper)', () => {
    localStorage.setItem(boardSavedViewsKey('u', 'p'), JSON.stringify([makeView()]));
    expect(loadBoardSavedViews('u', 'p')).toHaveLength(1);
  });

  it('enforces the cap on both save and load', () => {
    const many = Array.from({ length: BOARD_SAVED_VIEWS_CAP + 5 }, (_, i) => makeView({ id: `v${i}`, name: `View ${i}` }));
    saveBoardSavedViews('u', 'p', many);
    expect(loadBoardSavedViews('u', 'p')).toHaveLength(BOARD_SAVED_VIEWS_CAP);

    // A payload written out-of-band that exceeds the cap is also clamped on read.
    localStorage.setItem(boardSavedViewsKey('u', 'p'), JSON.stringify({ version: 1, views: many }));
    expect(loadBoardSavedViews('u', 'p')).toHaveLength(BOARD_SAVED_VIEWS_CAP);
  });

  it('scopes views per user/project', () => {
    saveBoardSavedViews('alice', 'p1', [makeView({ id: 'a' })]);
    saveBoardSavedViews('bob', 'p1', [makeView({ id: 'b1' }), makeView({ id: 'b2' })]);
    expect(loadBoardSavedViews('alice', 'p1')).toHaveLength(1);
    expect(loadBoardSavedViews('bob', 'p1')).toHaveLength(2);
    expect(loadBoardSavedViews('alice', 'p2')).toEqual([]);
  });
});
