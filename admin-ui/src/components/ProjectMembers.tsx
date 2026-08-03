import { useState } from 'react';
import { Shield, Eye, User, Trash2, Plus, Loader2 } from 'lucide-react';
import type { ProjectMemberEntry } from '../types/project';

interface ProjectMembersProps {
  projectId: string;
  members: ProjectMemberEntry[];
  myRole: 'owner' | 'member' | 'viewer';
  canManageMembers: boolean;
  onAdd: (email: string, role: string) => Promise<void>;
  onUpdateRole: (memberId: string, role: string) => Promise<void>;
  onRemove: (memberId: string) => Promise<void>;
}

const ROLE_BADGE: Record<string, { bg: string; text: string; border: string; icon: typeof Shield }> = {
  owner:  { bg: 'bg-amber-500/10',   text: 'text-amber-400',  border: 'border-amber-500/20',  icon: Shield },
  member: { bg: 'bg-cyan-500/10',    text: 'text-cyan-400',   border: 'border-cyan-500/20',   icon: User },
  viewer: { bg: 'bg-neutral-700/50', text: 'text-neutral-400', border: 'border-neutral-600/30', icon: Eye },
};

export function ProjectMembers({ members, myRole, canManageMembers, onAdd, onUpdateRole, onRemove }: ProjectMembersProps) {
  const [newEmail, setNewEmail] = useState('');
  const [newRole, setNewRole] = useState<'member' | 'viewer'>('viewer');
  const [loading, setLoading] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  const isOwner = myRole === 'owner' && canManageMembers;

  const handleAdd = async () => {
    if (!newEmail.trim()) return;
    setLoading('add');
    setError(null);
    try {
      await onAdd(newEmail.trim(), newRole);
      setNewEmail('');
      setNewRole('viewer');
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Failed to invite member');
    } finally {
      setLoading(null);
    }
  };

  const handleRoleChange = async (member: ProjectMemberEntry, nextRole: string) => {
    if (nextRole === member.role) return;
    setLoading(member.id);
    setError(null);
    try {
      await onUpdateRole(member.id, nextRole);
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Failed to update role');
    } finally {
      setLoading(null);
    }
  };

  const handleRemove = async (member: ProjectMemberEntry) => {
    const memberLabel = member.email || member.user_id || 'this invitation';
    const action = member.status === 'pending' ? 'Cancel the invitation for' : 'Remove';
    if (!confirm(`${action} ${memberLabel}?`)) return;
    setLoading(member.id);
    setError(null);
    try {
      await onRemove(member.id);
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Failed to remove member');
    } finally {
      setLoading(null);
    }
  };

  return (
    <div
      className="bg-neutral-900 border border-neutral-800 rounded-xl overflow-hidden"
    >
      <div className="px-4 py-3 border-b border-neutral-800">
        <h3 className="text-sm font-semibold text-neutral-100">Project Members</h3>
      </div>

      {error && (
        <div className="mx-4 mt-3 px-3 py-2 bg-red-500/10 border border-red-500/20 rounded text-xs text-red-400">
          {error}
        </div>
      )}

      {/* Invite form — owner only */}
      {isOwner && (
        <div className="px-4 py-3 border-b border-neutral-800">
          <p className="mb-3 text-xs text-neutral-500">
            Viewer is read-only; Member can run tasks and edit project work. Blueprint does not send an email, so tell the person to sign in with this address.
          </p>
          <div className="flex gap-2 items-end">
            <div className="flex-1">
              <label className="block text-ui-sm text-neutral-500 mb-1">Email</label>
              <input
                type="email"
                value={newEmail}
                onChange={e => setNewEmail(e.target.value)}
                onKeyDown={e => e.key === 'Enter' && handleAdd()}
                placeholder="user@example.com"
                className="w-full bg-neutral-800 border border-neutral-700 rounded px-2.5 py-1.5 text-xs text-neutral-200 placeholder:text-neutral-600 focus:outline-none focus:border-cyan-500/50"
              />
            </div>
            <div>
              <label className="block text-ui-sm text-neutral-500 mb-1">Role</label>
              <select
                value={newRole}
                onChange={e => setNewRole(e.target.value as 'member' | 'viewer')}
                className="bg-neutral-800 border border-neutral-700 rounded px-2.5 py-1.5 text-xs text-neutral-200 focus:outline-none focus:border-cyan-500/50"
                data-viewport-allow-small-target
              >
                <option value="viewer">Viewer</option>
                <option value="member">Member</option>
              </select>
            </div>
            <button
              onClick={handleAdd}
              disabled={!newEmail.trim() || loading === 'add'}
              className="min-h-[44px] px-3 py-1.5 bg-cyan-600 hover:bg-cyan-500 disabled:opacity-50 disabled:cursor-not-allowed text-white text-xs rounded font-medium transition-colors flex items-center gap-1.5"
            >
              {loading === 'add' ? <Loader2 className="w-3 h-3 animate-spin" /> : <Plus className="w-3 h-3" />}
              Invite
            </button>
          </div>
        </div>
      )}

      {/* Members list */}
      <div className="max-h-60 overflow-y-auto">
        {members.length === 0 ? (
          <div className="px-4 py-6 text-center text-xs text-neutral-500">No members</div>
        ) : (
          <div className="divide-y divide-neutral-800/50">
            {members.map(m => {
              const badge = ROLE_BADGE[m.role] || ROLE_BADGE.viewer;
              const Icon = badge.icon;
              const isRoleOwner = m.role === 'owner';
              const isPending = m.status === 'pending';
              const displayIdentity = m.email || (!isPending ? m.user_id : null) || 'Pending invitation';

              return (
                <div key={m.id} className="flex items-center gap-3 px-4 py-2.5 hover:bg-neutral-800/30">
                  {/* User info */}
                  <div className="flex-1 min-w-0">
                    <div className="text-xs text-neutral-200 truncate">
                      {displayIdentity}
                    </div>
                    <span className={`mt-1 inline-flex rounded-full border px-1.5 py-0.5 text-ui-sm font-medium ${
                      isPending
                        ? 'border-amber-500/20 bg-amber-500/10 text-amber-400'
                        : 'border-emerald-500/20 bg-emerald-500/10 text-emerald-400'
                    }`}>
                      {isPending ? 'Pending sign-in' : 'Active'}
                    </span>
                  </div>

                  {/* Role badge / selector */}
                  {isOwner && !isRoleOwner ? (
                    <select
                      value={m.role}
                      onChange={e => handleRoleChange(m, e.target.value)}
                      disabled={loading === m.id}
                      className={`inline-flex items-center gap-1 px-2 py-0.5 rounded-full text-ui-sm font-medium ${badge.bg} ${badge.text} border ${badge.border} bg-transparent cursor-pointer focus:outline-none`}
                      data-viewport-allow-small-target
                    >
                      <option value="member">member</option>
                      <option value="viewer">viewer</option>
                    </select>
                  ) : (
                    <span className={`inline-flex items-center gap-1 px-2 py-0.5 rounded-full text-ui-sm font-medium ${badge.bg} ${badge.text} border ${badge.border}`}>
                      {loading === m.id ? (
                        <Loader2 className="w-3 h-3 animate-spin" />
                      ) : (
                        <Icon className="w-3 h-3" />
                      )}
                      {m.role}
                    </span>
                  )}

                  {/* Remove button — owner can remove non-owners */}
                  {isOwner && !isRoleOwner ? (
                    <button
                      onClick={() => handleRemove(m)}
                      disabled={loading === m.id}
                      className="p-1 rounded text-neutral-500 hover:text-red-400 hover:bg-red-500/10 transition-colors disabled:opacity-30 disabled:cursor-not-allowed"
                      title={isPending ? 'Cancel invitation' : 'Remove member'}
                      aria-label={isPending ? `Cancel invitation for ${displayIdentity}` : `Remove ${displayIdentity}`}
                      data-viewport-allow-small-target
                    >
                      <Trash2 className="w-3.5 h-3.5" />
                    </button>
                  ) : (
                    /* Spacer to keep alignment when no remove button */
                    <div className="w-6" />
                  )}
                </div>
              );
            })}
          </div>
        )}
      </div>
    </div>
  );
}
