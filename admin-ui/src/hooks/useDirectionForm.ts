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

  // Edit form state
  const [editingId, setEditingId] = useState<string | null>(null);
  const [editText, setEditText] = useState('');
  const [editPriority, setEditPriority] = useState<'high' | 'medium' | 'low'>('medium');
  const [editStatus, setEditStatus] = useState<'active' | 'completed' | 'paused' | 'proposed' | 'expired'>('active');
  const [editCategory, setEditCategory] = useState<DirectionCategory>(null);
  const [editPathTags, setEditPathTags] = useState<string[]>([]);
  const [editPathInput, setEditPathInput] = useState('');

  function startEdit(d: Direction) {
    setEditingId(d.id);
    setEditText(d.text);
    setEditPriority(d.priority);
    setEditStatus(d.status);
    setEditCategory(d.category ?? null);
    setEditPathTags(d.paths ?? []);
    setEditPathInput('');
  }

  function resetAddForm() {
    setNewText('');
    setNewPriority('medium');
    setNewCategory(null);
    setNewPathTags([]);
    setNewPathInput('');
  }

  return {
    newText, setNewText,
    newPriority, setNewPriority,
    newCategory, setNewCategory,
    newPathTags, setNewPathTags,
    newPathInput, setNewPathInput,
    editingId, setEditingId,
    editText, setEditText,
    editPriority, setEditPriority,
    editStatus, setEditStatus,
    editCategory, setEditCategory,
    editPathTags, setEditPathTags,
    editPathInput, setEditPathInput,
    startEdit,
    resetAddForm,
  };
}
