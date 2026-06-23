import { useState, useRef, useEffect } from 'react';
import { ChevronDown, FolderKanban } from 'lucide-react';
import type { Project } from '../types/project';

interface ProjectSwitcherDropdownProps {
  projects: Project[];
  selectedProjectId: string | null;
  onSelectProject: (projectId: string | null) => void;
}

export function ProjectSwitcherDropdown({
  projects,
  selectedProjectId,
  onSelectProject,
}: ProjectSwitcherDropdownProps) {
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement>(null);

  const selected = projects.find(p => p.id === selectedProjectId);
  const label = selected?.name ?? selected?.github_repo ?? 'All Projects';

  useEffect(() => {
    function handleClickOutside(e: MouseEvent) {
      if (ref.current && !ref.current.contains(e.target as Node)) {
        setOpen(false);
      }
    }
    if (open) document.addEventListener('mousedown', handleClickOutside);
    return () => document.removeEventListener('mousedown', handleClickOutside);
  }, [open]);

  return (
    <div ref={ref} className="relative shrink-0">
      <button
        onClick={() => setOpen(o => !o)}
        className="flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-sm font-medium text-neutral-300 hover:text-neutral-100 hover:bg-neutral-800/60 transition-colors min-h-[36px]"
        aria-haspopup="listbox"
        aria-expanded={open}
      >
        <FolderKanban className="w-3.5 h-3.5 text-neutral-500 shrink-0" />
        <span className="max-w-[160px] truncate">{label}</span>
        <ChevronDown className="w-3.5 h-3.5 text-neutral-500 shrink-0" />
      </button>

      {open && (
        <div
          className="absolute left-0 top-full mt-1 z-50 w-56 bg-neutral-900 border border-neutral-800 rounded-lg shadow-xl overflow-hidden"
          role="listbox"
        >
          <div className="max-h-72 overflow-y-auto custom-scrollbar">
            <button
              role="option"
              aria-selected={selectedProjectId === null}
              onClick={() => { onSelectProject(null); setOpen(false); }}
              className={`w-full text-left px-3 py-2 text-sm transition-colors ${
                selectedProjectId === null
                  ? 'bg-neutral-800 text-neutral-100'
                  : 'text-neutral-400 hover:bg-neutral-800/60 hover:text-neutral-200'
              }`}
            >
              All Projects
            </button>
            {projects.map(p => (
              <button
                key={p.id}
                role="option"
                aria-selected={p.id === selectedProjectId}
                onClick={() => { onSelectProject(p.id); setOpen(false); }}
                className={`w-full text-left px-3 py-2 text-sm transition-colors ${
                  p.id === selectedProjectId
                    ? 'bg-neutral-800 text-neutral-100'
                    : 'text-neutral-400 hover:bg-neutral-800/60 hover:text-neutral-200'
                }`}
              >
                <span className="block truncate">{p.name ?? p.github_repo ?? p.id}</span>
                {p.github_owner && p.github_repo && (
                  <span className="block text-[10px] text-neutral-600 truncate">
                    {p.github_owner}/{p.github_repo}
                  </span>
                )}
              </button>
            ))}
          </div>
        </div>
      )}
    </div>
  );
}
