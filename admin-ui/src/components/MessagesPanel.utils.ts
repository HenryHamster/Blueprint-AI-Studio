// Re-export barrel — keeps backward compatibility for importers (App.tsx, test files)
// that reference './MessagesPanel.utils'. Actual implementations live in focused modules:
//   eventPrefs.ts, muteWindow.ts, savedViews.ts, temporaryMutes.ts,
//   messageGrouping.ts, lastSeen.ts, serverSync.ts
//
// EVENT TYPE INVARIANT: See backend/testing/event_type_snapshot.json for the canonical list
// of all 21+ event_types emitted by the backend. The invariant test in
// MessagesPanel.invariant.test.ts validates that every event_type appears in either
// HIGH_SIGNAL_NOTE_EVENTS or ROUTINE_EVENT_LABELS. When adding a new event_type:
// 1. Update backend/testing/event_type_snapshot.json with (name, classification, source_fn)
// 2. Update HIGH_SIGNAL_NOTE_EVENTS, ROUTINE_EVENT_LABELS, or ESCALATION_EVENT_TYPES in eventPrefs.ts
// 3. If escalation, add a badge label to ESCALATION_BADGE_LABELS
// The invariant test will fail and tell you exactly what's missing.

export { ESCALATION_EVENT_TYPES, ESCALATION_BADGE_LABELS, HIGH_SIGNAL_NOTE_BADGE_LABELS, HIGH_SIGNAL_NOTE_EVENTS, CRITICAL_NOTE_EVENTS, ROUTINE_EVENT_LABELS, STRATEGIST_INTERNAL_FALLBACK_BLOCK, type EventPref, EVENT_PREFS_KEY, eventPrefsKey, loadEventPrefs, saveEventPrefs, classifySignalLevel, effectiveSignal, getToastSeverity, isCriticalSeverity, countHighSignalMessages, countNewSinceLastVisit } from './eventPrefs';
export { type MuteWindow, muteWindowKey, loadMuteWindow, saveMuteWindow, clearMuteWindow, isMuteWindowActive, formatMuteCountdown } from './muteWindow';
export { SAVED_VIEWS_CAP, type SavedView, type SavedViewsFile, savedViewsKey, loadSavedViews, saveSavedViews } from './savedViews';
export { type TemporaryMutes, temporaryMutesKey, loadTemporaryMutes, saveTemporaryMutes, isEventTypeTemporarilyMuted } from './temporaryMutes';
export { STALE_ROUTINE_COLLAPSE_AGE_MS, type RoutineRow, type MessageWithDivider, groupRoutineMessages, outcomeBreakdown, isFailingGroup, routineSummary, collapseStaleRoutineRows, formatDailyDigestSummary, insertTimeDividers, formatTimestamp, formatSinceTimestamp } from './messageGrouping';
export { lastSeenKey, loadLastSeen, saveLastSeen, resolveLastSeen } from './lastSeen';
export { fetchServerNoisePrefs, saveServerNoisePrefs, resolveNoisePrefs, fetchServerSavedViews, saveServerSavedViews, resolveServerSavedViews, fetchServerTempMutes, saveServerTempMutes, resolveServerTempMutes, fetchServerMuteWindow, saveServerMuteWindow, resolveServerMuteWindow, fetchBackendLastSeen } from './serverSync';
