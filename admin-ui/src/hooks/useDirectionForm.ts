import { useState } from 'react';
import type { Direction } from '../components/DirectionsPanel';

export type DirectionCategory = 'product' | 'structural' | 'mixed' | null;

export function useDirectionForm() {
  // Add form state
  const [newText, setNewText] = useState('');
  const [newPriority, setNewPriority] = useState<'high' | 'medium' | 'low'>('medium');
  const [newCategory, setNewCategory] = useState<DirectionCategory>(null);
  const [newPathTags, setNewPathTags] = useState<string[]>([]);
  const [newPathInput, setNewPathInput] = useState('');
  const [newCompletionCriteria, setNewCompletionCriteria] = useState('');
  // When true, create the direction directly in the 'staged' state (atomic
  // born-staged create) instead of 'active', so a next-version idea never
  // passes through a research-eligible active state. Gated in the UI to an
  // open release version, mirroring the edit-form 'staged' status option.
  const [newStaged, setNewStaged] = useState(false);

  // Edit form state
  const [editingId, setEditingId] = useState<string | null>(null);
  const [editText, setEditText] = useState('');
  const [editPriority, setEditPriority] = useState<'high' | 'medium' | 'low'>('medium');
  const [editStatus, setEditStatus] = useState<'active' | 'completed' | 'paused' | 'proposed' | 'expired' | 'staged'>('active');
  const [editCategory, setEditCategory] = useState<DirectionCategory>(null);
  const [editPathTags, setEditPathTags] = useState<string[]>([]);
  const [editPathInput, setEditPathInput] = useState('');
  const [editCompletionCriteria, setEditCompletionCriteria] = useState('');

  function startEdit(d: Direction) {
    setEditingId(d.id);
    setEditText(d.text);
    setEditPriority(d.priority);
    setEditStatus(d.status);
    setEditCategory(d.category ?? null);
    setEditPathTags(d.paths ?? []);
    setEditPathInput('');
    setEditCompletionCriteria(d.completion_criteria ?? '');
  }

  function resetAddForm() {
    setNewText('');
    setNewPriority('medium');
    setNewCategory(null);
    setNewPathTags([]);
    setNewPathInput('');
    setNewCompletionCriteria('');
    setNewStaged(false);
  }

  return {
    newText, setNewText,
    newPriority, setNewPriority,
    newCategory, setNewCategory,
    newPathTags, setNewPathTags,
    newPathInput, setNewPathInput,
    newCompletionCriteria, setNewCompletionCriteria,
    newStaged, setNewStaged,
    editingId, setEditingId,
    editText, setEditText,
    editPriority, setEditPriority,
    editStatus, setEditStatus,
    editCategory, setEditCategory,
    editPathTags, setEditPathTags,
    editPathInput, setEditPathInput,
    editCompletionCriteria, setEditCompletionCriteria,
    startEdit,
    resetAddForm,
  };
}
