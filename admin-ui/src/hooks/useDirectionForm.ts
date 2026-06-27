import { useState, useRef, type SetStateAction } from 'react';
import type { Direction } from '../components/DirectionsPanel';

export type DirectionCategory = 'product' | 'structural' | 'mixed' | null;

export function useDirectionForm() {
  // Add form state
  const [newText, setNewTextState] = useState('');
  const [newPriority, setNewPriorityState] = useState<'high' | 'medium' | 'low'>('medium');
  const [newCategory, setNewCategoryState] = useState<DirectionCategory>(null);
  const [newPathTags, setNewPathTagsState] = useState<string[]>([]);
  const [newPathInput, setNewPathInput] = useState('');
  const [newCompletionCriteria, setNewCompletionCriteriaState] = useState('');
  // Acceptance-evidence contract authored at create time, stored under
  // Direction.context. Mirrors completion_criteria plumbing (trim-or-null on send).
  const [newSuccessEvidence, setNewSuccessEvidenceState] = useState('');
  const [newRetirementCondition, setNewRetirementConditionState] = useState('');
  // When true, create the direction directly in the 'staged' state (atomic
  // born-staged create) instead of 'active', so a next-version idea never
  // passes through a research-eligible active state. Gated in the UI to an
  // open release version, mirroring the edit-form 'staged' status option.
  const [newStaged, setNewStagedState] = useState(false);
  // Problem model gap this direction is being created to address. Set when
  // the operator clicks "Spawn direction" on a gap card; cleared on reset.
  const [newLinkedGapId, setNewLinkedGapIdState] = useState<string | null>(null);

  // Client idempotency identity for the add path. Minted lazily on first submit
  // and reused across double-clicks / failed retries / lost-response replays so
  // the backend collapses them into one direction. Cleared by resetAddForm()
  // (i.e. on a successful create or a fresh draft) so the next logical add mints
  // a new key.
  const addIdempotencyKeyRef = useRef<string | null>(null);
  // A failed submit keeps the current key alive for an unchanged retry, but the
  // first real draft edit after that failure must rotate the key so a newly
  // edited add does not replay the prior logical request.
  const rotateAddIdempotencyKeyOnDraftChangeRef = useRef(false);

  // Edit form state
  const [editingId, setEditingId] = useState<string | null>(null);
  const [editText, setEditText] = useState('');
  const [editPriority, setEditPriority] = useState<'high' | 'medium' | 'low'>('medium');
  const [editStatus, setEditStatus] = useState<'active' | 'completed' | 'paused' | 'proposed' | 'expired' | 'staged'>('active');
  const [editCategory, setEditCategory] = useState<DirectionCategory>(null);
  const [editPathTags, setEditPathTags] = useState<string[]>([]);
  const [editPathInput, setEditPathInput] = useState('');
  const [editCompletionCriteria, setEditCompletionCriteria] = useState('');
  // Acceptance-evidence contract for inline edit; seeded from the direction's
  // context when entering edit mode (see startEdit).
  const [editSuccessEvidence, setEditSuccessEvidence] = useState('');
  const [editRetirementCondition, setEditRetirementCondition] = useState('');
  // Cost budget in USD — seeded from direction.cost_budget_usd (or empty string
  // when unset); sent as a float or null on PATCH.
  const [editCostBudget, setEditCostBudget] = useState('');

  function startEdit(d: Direction) {
    setEditingId(d.id);
    setEditText(d.text);
    setEditPriority(d.priority);
    setEditStatus(d.status);
    setEditCategory(d.category ?? null);
    setEditPathTags(d.paths ?? []);
    setEditPathInput('');
    setEditCompletionCriteria(d.completion_criteria ?? '');
    setEditSuccessEvidence(d.context?.success_evidence ?? '');
    setEditRetirementCondition(d.context?.retirement_condition ?? '');
    setEditCostBudget(d.cost_budget_usd != null ? String(d.cost_budget_usd) : '');
  }

  function resetAddKeyForEditedDraft() {
    if (!rotateAddIdempotencyKeyOnDraftChangeRef.current) return;
    addIdempotencyKeyRef.current = null;
    rotateAddIdempotencyKeyOnDraftChangeRef.current = false;
  }

  function setNewText(value: string) {
    resetAddKeyForEditedDraft();
    setNewTextState(value);
  }

  function setNewPriority(value: 'high' | 'medium' | 'low') {
    resetAddKeyForEditedDraft();
    setNewPriorityState(value);
  }

  function setNewCategory(value: DirectionCategory) {
    resetAddKeyForEditedDraft();
    setNewCategoryState(value);
  }

  function setNewPathTags(value: SetStateAction<string[]>) {
    resetAddKeyForEditedDraft();
    setNewPathTagsState(value);
  }

  function setNewCompletionCriteria(value: string) {
    resetAddKeyForEditedDraft();
    setNewCompletionCriteriaState(value);
  }

  function setNewSuccessEvidence(value: string) {
    resetAddKeyForEditedDraft();
    setNewSuccessEvidenceState(value);
  }

  function setNewRetirementCondition(value: string) {
    resetAddKeyForEditedDraft();
    setNewRetirementConditionState(value);
  }

  function setNewStaged(value: boolean) {
    resetAddKeyForEditedDraft();
    setNewStagedState(value);
  }

  function setNewLinkedGapId(value: string | null) {
    resetAddKeyForEditedDraft();
    setNewLinkedGapIdState(value);
  }

  function markAddAttemptFailed() {
    rotateAddIdempotencyKeyOnDraftChangeRef.current = true;
  }

  function resetAddForm() {
    setNewTextState('');
    setNewPriorityState('medium');
    setNewCategoryState(null);
    setNewPathTagsState([]);
    setNewPathInput('');
    setNewCompletionCriteriaState('');
    setNewSuccessEvidenceState('');
    setNewRetirementConditionState('');
    setNewStagedState(false);
    setNewLinkedGapIdState(null);
    addIdempotencyKeyRef.current = null;
    rotateAddIdempotencyKeyOnDraftChangeRef.current = false;
  }

  function seedFromMessage(text: string) {
    resetAddForm();
    setNewTextState(text.slice(0, 2000));
  }

  return {
    newText, setNewText,
    newPriority, setNewPriority,
    newCategory, setNewCategory,
    newPathTags, setNewPathTags,
    newPathInput, setNewPathInput,
    newCompletionCriteria, setNewCompletionCriteria,
    newSuccessEvidence, setNewSuccessEvidence,
    newRetirementCondition, setNewRetirementCondition,
    newStaged, setNewStaged,
    newLinkedGapId, setNewLinkedGapId,
    addIdempotencyKeyRef,
    markAddAttemptFailed,
    editingId, setEditingId,
    editText, setEditText,
    editPriority, setEditPriority,
    editStatus, setEditStatus,
    editCategory, setEditCategory,
    editPathTags, setEditPathTags,
    editPathInput, setEditPathInput,
    editCompletionCriteria, setEditCompletionCriteria,
    editSuccessEvidence, setEditSuccessEvidence,
    editRetirementCondition, setEditRetirementCondition,
    editCostBudget, setEditCostBudget,
    startEdit,
    resetAddForm,
    seedFromMessage,
  };
}
