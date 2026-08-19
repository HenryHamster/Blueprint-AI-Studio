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
    <div ref={ref} className="relative min-w-0 shrink">
      <button
        onClick={() => setOpen(o => !o)}
        className="flex min-h-11 max-w-[calc(100vw-7.5rem)] items-center gap-1.5 rounded-md border border-neutral-800 bg-workbench-control px-2.5 py-2 text-sm font-medium text-neutral-300 transition-colors hover:border-neutral-700 hover:bg-workbench-hover hover:text-neutral-100 md:h-7 md:min-h-0 md:max-w-[18rem] md:px-2.5 md:py-0 md:text-xs"
        aria-haspopup="listbox"
        aria-expanded={open}
      >
        <FolderKanban className="w-3.5 h-3.5 text-neutral-500 shrink-0" />
        <span className="min-w-0 max-w-[13rem] truncate">{label}</span>
        <ChevronDown className="w-3.5 h-3.5 text-neutral-500 shrink-0" />
      </button>

      {open && (
        <div
          className="absolute left-0 top-full mt-1 z-50 w-56 bg-workbench-base border border-neutral-800 rounded-lg shadow-xl overflow-hidden"
          role="listbox"
        >
          <div className="max-h-72 overflow-y-auto custom-scrollbar">
            <button
              role="option"
              aria-selected={selectedProjectId === null}
              onClick={() => { onSelectProject(null); setOpen(false); }}
              className={`w-full text-left px-3 py-2 text-sm transition-colors ${
                selectedProjectId === null
                  ? 'bg-workbench-panel text-neutral-100'
                  : 'text-neutral-400 hover:bg-workbench-hover/60 hover:text-neutral-200'
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
                    ? 'bg-workbench-panel text-neutral-100'
                    : 'text-neutral-400 hover:bg-workbench-hover/60 hover:text-neutral-200'
                }`}
              >
                <span className="block truncate">{p.name ?? p.github_repo ?? p.id}</span>
                {p.github_owner && p.github_repo && (
                  <span className="block text-ui-sm text-neutral-600 truncate">
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
