import { useState } from 'react';
import { X, Shield, User, Trash2, Plus, Loader2 } from 'lucide-react';
import { parseTimestamp } from '../utils/time';

interface AllowedUserEntry {
  id: string;
  email: string;
  role: string;
  display_name: string | null;
  last_login_at: string | null;
  created_at: string | null;
}

interface UserManagementProps {
  users: AllowedUserEntry[];
  currentUserEmail: string;
  onAdd: (email: string, role: string) => Promise<void>;
  onUpdateRole: (userId: string, role: string) => Promise<void>;
  onRemove: (userId: string) => Promise<void>;
  onClose: () => void;
}

export function UserManagement({ users, currentUserEmail, onAdd, onUpdateRole, onRemove, onClose }: UserManagementProps) {
  const [newEmail, setNewEmail] = useState('');
  const [newRole, setNewRole] = useState<'user' | 'admin'>('user');
  const [loading, setLoading] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  const handleAdd = async () => {
    if (!newEmail.trim()) return;
    setLoading('add');
    setError(null);
    try {
      await onAdd(newEmail.trim(), newRole);
      setNewEmail('');
      setNewRole('user');
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Failed to add user');
    } finally {
      setLoading(null);
    }
  };

  const handleToggleRole = async (user: AllowedUserEntry) => {
    const nextRole = user.role === 'admin' ? 'user' : 'admin';
    setLoading(user.id);
    setError(null);
    try {
      await onUpdateRole(user.id, nextRole);
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Failed to update role');
    } finally {
      setLoading(null);
    }
  };

  const handleRemove = async (user: AllowedUserEntry) => {
    if (!confirm(`Remove ${user.email}?`)) return;
    setLoading(user.id);
    setError(null);
    try {
      await onRemove(user.id);
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Failed to remove user');
    } finally {
      setLoading(null);
    }
  };

  const formatDate = (iso: string | null) => {
    if (!iso) return 'Never';
    const d = parseTimestamp(iso);
    return d.toLocaleDateString(undefined, { month: 'short', day: 'numeric', year: 'numeric' });
  };

  return (
    <div className="bg-workbench-base border border-neutral-800 rounded-lg overflow-hidden">
      <div className="flex items-center justify-between px-4 py-3 border-b border-neutral-800">
        <h2 className="text-sm font-semibold text-neutral-100">User Management</h2>
        {/* eslint-disable-next-line viewport/min-touch-target -- pre-existing, dense UI; audit per rule introduction */}
        <button onClick={onClose} className="p-1 hover:bg-workbench-hover rounded transition-colors">
          <X className="w-4 h-4 text-neutral-400" />
        </button>
      </div>

      {error && (
        <div className="mx-4 mt-3 px-3 py-2 bg-red-500/10 border border-red-500/20 rounded text-xs text-red-400">
          {error}
        </div>
      )}

      {/* Add user form */}
      <div className="px-4 py-3 border-b border-neutral-800 flex gap-2 items-end">
        <div className="flex-1">
          <label className="block text-ui-sm text-neutral-500 mb-1">Email</label>
          <input
            type="email"
            value={newEmail}
            onChange={e => setNewEmail(e.target.value)}
            onKeyDown={e => e.key === 'Enter' && handleAdd()}
            placeholder="user@example.com"
            className="w-full bg-workbench-panel border border-neutral-700 rounded px-2.5 py-1.5 text-xs text-neutral-200 placeholder:text-neutral-600 workbench-focus"
          />
        </div>
        <div>
          <label className="block text-ui-sm text-neutral-500 mb-1">Role</label>
          <select
            value={newRole}
            onChange={e => setNewRole(e.target.value as 'user' | 'admin')}
            className="bg-workbench-panel border border-neutral-700 rounded px-2.5 py-1.5 text-xs text-neutral-200 workbench-focus"
          >
            <option value="user">User</option>
            <option value="admin">Admin</option>
          </select>
        </div>
        <button
          onClick={handleAdd}
          disabled={!newEmail.trim() || loading === 'add'}
          className="px-3 py-1.5 bg-cyan-600 hover:bg-cyan-500 disabled:opacity-50 disabled:cursor-not-allowed text-white text-xs rounded font-medium transition-colors flex items-center gap-1.5"
        >
          {loading === 'add' ? <Loader2 className="w-3 h-3 animate-spin" /> : <Plus className="w-3 h-3" />}
          Add
        </button>
      </div>

      {/* User list */}
      <div className="max-h-80 overflow-y-auto">
        {users.length === 0 ? (
          <div className="px-4 py-6 text-center text-xs text-neutral-500">No users</div>
        ) : (
          <table className="w-full text-xs">
            <thead>
              <tr className="text-neutral-500 text-ui-sm uppercase tracking-wider border-b border-neutral-800">
                <th className="text-left px-4 py-2">Email</th>
                <th className="text-left px-4 py-2">Role</th>
                <th className="text-left px-4 py-2 hidden sm:table-cell">Last Login</th>
                <th className="text-right px-4 py-2">Actions</th>
              </tr>
            </thead>
            <tbody>
              {users.map(u => {
                const isSelf = u.email === currentUserEmail?.toLowerCase();
                return (
                  <tr key={u.id} className="border-b border-neutral-800/50 hover:bg-workbench-hover/30">
                    <td className="px-4 py-2.5">
                      <div className="text-neutral-200">{u.display_name || u.email}</div>
                      {u.display_name && <div className="text-neutral-500 text-ui-sm">{u.email}</div>}
                      {isSelf && <span className="text-ui-sm text-cyan-500 ml-1">(you)</span>}
                    </td>
                    <td className="px-4 py-2.5">
                      {/* eslint-disable-next-line viewport/min-touch-target -- pre-existing, dense UI; audit per rule introduction */}
                      <button
                        onClick={() => handleToggleRole(u)}
                        disabled={isSelf || loading === u.id}
                        className={`inline-flex items-center gap-1 px-2 py-0.5 rounded-full text-ui-sm font-medium transition-colors ${
                          u.role === 'admin'
                            ? 'bg-amber-500/10 text-amber-400 border border-amber-500/20'
                            : 'bg-workbench-layer/50 text-neutral-400 border border-neutral-600/30'
                        } ${isSelf ? 'cursor-default' : 'hover:opacity-80 cursor-pointer'}`}
                        title={isSelf ? 'Cannot change your own role' : `Click to make ${u.role === 'admin' ? 'user' : 'admin'}`}
                      >
                        {loading === u.id ? (
                          <Loader2 className="w-3 h-3 animate-spin" />
                        ) : u.role === 'admin' ? (
                          <Shield className="w-3 h-3" />
                        ) : (
                          <User className="w-3 h-3" />
                        )}
                        {u.role}
                      </button>
                    </td>
                    <td className="px-4 py-2.5 text-neutral-500 hidden sm:table-cell">
                      {formatDate(u.last_login_at)}
                    </td>
                    <td className="px-4 py-2.5 text-right">
                      {/* eslint-disable-next-line viewport/min-touch-target -- pre-existing, dense UI; audit per rule introduction */}
                      <button
                        onClick={() => handleRemove(u)}
                        disabled={isSelf || loading === u.id}
                        className="p-1 rounded text-neutral-500 hover:text-red-400 hover:bg-red-500/10 transition-colors disabled:opacity-30 disabled:cursor-not-allowed"
                        title={isSelf ? 'Cannot remove yourself' : 'Remove user'}
                      >
                        <Trash2 className="w-3.5 h-3.5" />
                      </button>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        )}
      </div>
    </div>
  );
}
