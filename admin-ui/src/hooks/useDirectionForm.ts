import { useState } from 'react';
import type { Direction } from '../components/DirectionsPanel';

export function useDirectionForm() {
  // Add form state
  const [newText, setNewText] = useState('');
  const [newPriority, setNewPriority] = useState<'high' | 'medium' | 'low'>('medium');
  const [newPathTags, setNewPathTags] = useState<string[]>([]);
  const [newPathInput, setNewPathInput] = useState('');

  // Edit form state
  const [editingId, setEditingId] = useState<string | null>(null);
  const [editText, setEditText] = useState('');
  const [editPriority, setEditPriority] = useState<'high' | 'medium' | 'low'>('medium');
  const [editStatus, setEditStatus] = useState<'active' | 'completed' | 'paused' | 'proposed' | 'expired'>('active');
  const [editPathTags, setEditPathTags] = useState<string[]>([]);
  const [editPathInput, setEditPathInput] = useState('');

  function startEdit(d: Direction) {
    setEditingId(d.id);
    setEditText(d.text);
    setEditPriority(d.priority);
    setEditStatus(d.status);
    setEditPathTags(d.paths ?? []);
    setEditPathInput('');
  }

  function resetAddForm() {
    setNewText('');
    setNewPriority('medium');
    setNewPathTags([]);
    setNewPathInput('');
  }

  return {
    newText, setNewText,
    newPriority, setNewPriority,
    newPathTags, setNewPathTags,
    newPathInput, setNewPathInput,
    editingId, setEditingId,
    editText, setEditText,
    editPriority, setEditPriority,
    editStatus, setEditStatus,
    editPathTags, setEditPathTags,
    editPathInput, setEditPathInput,
    startEdit,
    resetAddForm,
  };
}
