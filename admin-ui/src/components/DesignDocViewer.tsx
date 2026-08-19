import { useState } from 'react';
import { ChevronDown, ChevronRight, FileText, FolderOpen } from 'lucide-react';
import { MarkdownContent } from './MarkdownContent';
import { timeAgo } from '../utils/time';

interface DesignDocViewerProps {
  designDoc: Record<string, unknown>;
}

const statusColors: Record<string, string> = {
  mature: 'bg-cyan-500',
  functional: 'bg-emerald-500',
  partial: 'bg-amber-500',
  stub: 'bg-orange-500',
  missing: 'bg-red-500',
  in_progress: 'bg-amber-500',
  planned: 'bg-workbench-card',
};


/** Recursively render an arbitrary JSON value with appropriate formatting */
function RenderValue({ value, depth = 0 }: { value: unknown; depth?: number }) {
  if (value === null || value === undefined) {
    return <span className="text-neutral-600 italic text-sm">empty</span>;
  }

  if (typeof value === 'string') {
    return <MarkdownContent content={value} />;
  }

  if (typeof value === 'number' || typeof value === 'boolean') {
    return <span className="text-sm text-neutral-300 font-mono">{String(value)}</span>;
  }

  // Arrays
  if (Array.isArray(value)) {
    if (value.length === 0) {
      return <span className="text-neutral-600 italic text-sm">empty list</span>;
    }
    // Array of strings — render as markdown list
    if (value.every(item => typeof item === 'string')) {
      return (
        <ul className="space-y-1">
          {value.map((item, i) => (
            <li key={i} className="flex items-start gap-2 text-sm">
              <span className="text-neutral-600 mt-0.5 shrink-0">-</span>
              <div className="min-w-0"><MarkdownContent content={item as string} /></div>
            </li>
          ))}
        </ul>
      );
    }
    // Array of objects
    if (depth < 3) {
      return (
        <div className="space-y-2">
          {value.map((item, i) => (
            <div key={i} className="border border-neutral-800/50 rounded p-2 bg-workbench-base/30">
              <RenderValue value={item} depth={depth + 1} />
            </div>
          ))}
        </div>
      );
    }
    // Fallback: JSON
    return <pre className="bg-workbench-base border border-neutral-800 rounded-lg p-3 text-xs text-neutral-400 overflow-x-auto">{JSON.stringify(value, null, 2)}</pre>;
  }

  // Objects
  if (typeof value === 'object') {
    const entries = Object.entries(value as Record<string, unknown>);
    if (entries.length === 0) {
      return <span className="text-neutral-600 italic text-sm">empty</span>;
    }
    if (depth >= 3) {
      return <pre className="bg-workbench-base border border-neutral-800 rounded-lg p-3 text-xs text-neutral-400 overflow-x-auto">{JSON.stringify(value, null, 2)}</pre>;
    }
    return (
      <div className="space-y-3">
        {entries.map(([key, val]) => (
          <div key={key}>
            <div className="text-ui-sm font-semibold text-neutral-500 uppercase tracking-wider mb-0.5 flex items-center gap-1.5">
              {key}
              {key === 'status' && typeof val === 'string' && (
                <span className={`w-1.5 h-1.5 rounded-full ${statusColors[val] || 'bg-workbench-card'}`} />
              )}
            </div>
            <div className="pl-2 border-l border-neutral-800/50">
              <RenderValue value={val} depth={depth + 1} />
            </div>
          </div>
        ))}
      </div>
    );
  }

  return <span className="text-sm text-neutral-400">{String(value)}</span>;
}

/** Build page tree from flat keys */
function buildPageTree(designDoc: Record<string, unknown>): { topLevel: string[]; groups: Map<string, string[]> } {
  const topLevel: string[] = [];
  const groups = new Map<string, string[]>();

  for (const key of Object.keys(designDoc)) {
    if (key.startsWith('_')) continue;
    const slashIdx = key.indexOf('/');
    if (slashIdx === -1) {
      topLevel.push(key);
    } else {
      const prefix = key.slice(0, slashIdx);
      const rest = key.slice(slashIdx + 1);
      const existing = groups.get(prefix);
      if (existing) {
        existing.push(rest);
      } else {
        groups.set(prefix, [rest]);
      }
    }
  }

  return { topLevel, groups };
}

function getPageStatus(value: unknown): string | null {
  if (typeof value === 'object' && value && !Array.isArray(value)) {
    const status = (value as Record<string, unknown>).status;
    if (typeof status === 'string') return status;
  }
  return null;
}

function getPagePreview(value: unknown): string {
  if (typeof value === 'string') return value.slice(0, 80);
  if (Array.isArray(value)) return `${value.length} items`;
  if (typeof value === 'object' && value) {
    const obj = value as Record<string, unknown>;
    if (typeof obj.name === 'string') return obj.name;
    if (typeof obj.description === 'string') return obj.description.slice(0, 80);
    return `${Object.keys(obj).length} fields`;
  }
  return String(value).slice(0, 80);
}

function prettifyKey(key: string): string {
  return key.replace(/[-_]/g, ' ').replace(/\b\w/g, c => c.toUpperCase());
}

export default function DesignDocViewer({ designDoc }: DesignDocViewerProps) {
  const [selectedPage, setSelectedPage] = useState<string | null>(null);
  const [collapsedGroups, setCollapsedGroups] = useState<Set<string>>(new Set());

  const meta = (designDoc._meta || {}) as Record<string, unknown>;
  const { topLevel, groups } = buildPageTree(designDoc);
  const pageCount = topLevel.length + Array.from(groups.values()).reduce((s, arr) => s + arr.length, 0);

  const toggleGroup = (prefix: string) => {
    setCollapsedGroups(prev => {
      const next = new Set(prev);
      if (next.has(prefix)) next.delete(prefix);
      else next.add(prefix);
      return next;
    });
  };

  const selectedValue = selectedPage ? designDoc[selectedPage] : null;

  return (
    <div className="flex h-full">
      {/* Sidebar */}
      <div className="w-[180px] shrink-0 border-r border-neutral-800 overflow-y-auto custom-scrollbar">
        <div className="p-2">
          {/* Overview link */}
          {/* eslint-disable-next-line viewport/min-touch-target -- pre-existing, dense UI; audit per rule introduction */}
          <button
            onClick={() => setSelectedPage(null)}
            className={`w-full text-left px-2 py-1.5 rounded text-xs transition-colors ${
              selectedPage === null
                ? 'bg-workbench-panel text-neutral-100'
                : 'text-neutral-400 hover:text-neutral-200 hover:bg-workbench-hover/50'
            }`}
          >
            Overview
          </button>

          {/* Top-level pages */}
          {topLevel.length > 0 && (
            <div className="mt-2 space-y-0.5">
              {topLevel.map(key => {
                const status = getPageStatus(designDoc[key]);
                return (
                  // eslint-disable-next-line viewport/min-touch-target -- pre-existing, dense UI; audit per rule introduction
                  <button
                    key={key}
                    onClick={() => setSelectedPage(key)}
                    className={`w-full text-left px-2 py-1 rounded text-xs flex items-center gap-1.5 transition-colors ${
                      selectedPage === key
                        ? 'bg-workbench-panel text-neutral-100'
                        : 'text-neutral-400 hover:text-neutral-200 hover:bg-workbench-hover/50'
                    }`}
                  >
                    <FileText className="w-3 h-3 shrink-0 text-neutral-600" />
                    <span className="truncate flex-1">{prettifyKey(key)}</span>
                    {status && <span className={`w-1.5 h-1.5 rounded-full shrink-0 ${statusColors[status] || 'bg-workbench-card'}`} />}
                  </button>
                );
              })}
            </div>
          )}

          {/* Grouped pages */}
          {Array.from(groups.entries()).map(([prefix, children]) => {
            const isCollapsed = collapsedGroups.has(prefix);
            return (
              <div key={prefix} className="mt-2">
                {/* eslint-disable-next-line viewport/min-touch-target -- pre-existing, dense UI; audit per rule introduction */}
                <button
                  onClick={() => toggleGroup(prefix)}
                  className="w-full text-left px-2 py-1 rounded text-ui-sm font-semibold uppercase tracking-wider text-neutral-500 hover:text-neutral-300 flex items-center gap-1 transition-colors"
                >
                  {isCollapsed
                    ? <ChevronRight className="w-3 h-3 shrink-0" />
                    : <ChevronDown className="w-3 h-3 shrink-0" />
                  }
                  <FolderOpen className="w-3 h-3 shrink-0 text-neutral-600" />
                  <span className="truncate">{prettifyKey(prefix)}</span>
                  <span className="text-neutral-600 ml-auto">{children.length}</span>
                </button>
                {!isCollapsed && (
                  <div className="ml-3 mt-0.5 space-y-0.5">
                    {children.map(child => {
                      const fullKey = `${prefix}/${child}`;
                      const status = getPageStatus(designDoc[fullKey]);
                      return (
                        // eslint-disable-next-line viewport/min-touch-target -- pre-existing, dense UI; audit per rule introduction
                        <button
                          key={fullKey}
                          onClick={() => setSelectedPage(fullKey)}
                          className={`w-full text-left px-2 py-1 rounded text-xs flex items-center gap-1.5 transition-colors ${
                            selectedPage === fullKey
                              ? 'bg-workbench-panel text-neutral-100'
                              : 'text-neutral-400 hover:text-neutral-200 hover:bg-workbench-hover/50'
                          }`}
                        >
                          <span className="truncate flex-1">{prettifyKey(child)}</span>
                          {status && <span className={`w-1.5 h-1.5 rounded-full shrink-0 ${statusColors[status] || 'bg-workbench-card'}`} />}
                        </button>
                      );
                    })}
                  </div>
                )}
              </div>
            );
          })}
        </div>
      </div>

      {/* Content area */}
      <div className="flex-1 overflow-y-auto custom-scrollbar p-4">
        {selectedPage === null ? (
          /* Overview / dashboard */
          <div className="space-y-4">
            <div>
              <h3 className="text-base font-bold text-neutral-100 tracking-tight mb-1">Design Document</h3>
              <div className="flex items-center gap-3 text-xs text-neutral-500">
                <span className="bg-workbench-panel px-1.5 py-0.5 rounded text-neutral-400 font-mono">{pageCount} pages</span>
                {typeof meta.revision === 'number' && <span className="bg-workbench-panel px-1.5 py-0.5 rounded text-neutral-400 font-mono">v{meta.revision}</span>}
                {typeof meta.last_updated === 'string' && <span>updated {timeAgo(meta.last_updated as string)}</span>}
              </div>
            </div>

            {pageCount === 0 ? (
              <div className="border border-neutral-800 border-dashed rounded-lg p-6 text-center">
                <p className="text-sm text-neutral-500">No design document yet. The architecture agent will build one on the next strategic cycle.</p>
              </div>
            ) : (
              <div className="space-y-1">
                {/* Top-level pages */}
                {topLevel.map(key => (
                  <button
                    key={key}
                    onClick={() => setSelectedPage(key)}
                    className="w-full text-left px-3 py-2 rounded-lg border border-neutral-800/50 hover:border-neutral-700 hover:bg-workbench-hover/50 transition-colors group"
                  >
                    <div className="flex items-center gap-2">
                      <FileText className="w-3.5 h-3.5 text-neutral-600 group-hover:text-neutral-400 transition-colors" />
                      <span className="text-sm font-medium text-neutral-300 group-hover:text-neutral-100 transition-colors">{prettifyKey(key)}</span>
                      {(() => {
                        const status = getPageStatus(designDoc[key]);
                        return status ? <span className={`text-ui-xs px-1.5 py-0.5 rounded ${statusColors[status] ? `${statusColors[status]}/20` : 'bg-workbench-panel'} text-neutral-400`}>{status}</span> : null;
                      })()}
                    </div>
                    <p className="text-xs text-neutral-500 mt-0.5 truncate pl-[22px]">{getPagePreview(designDoc[key])}</p>
                  </button>
                ))}

                {/* Grouped pages */}
                {Array.from(groups.entries()).map(([prefix, children]) => (
                  <div key={prefix} className="mt-2">
                    <div className="text-ui-sm font-semibold uppercase tracking-wider text-neutral-500 px-3 py-1">{prettifyKey(prefix)}</div>
                    {children.map(child => {
                      const fullKey = `${prefix}/${child}`;
                      return (
                        <button
                          key={fullKey}
                          onClick={() => setSelectedPage(fullKey)}
                          className="w-full text-left px-3 py-1.5 rounded-lg hover:bg-workbench-hover/50 transition-colors group"
                        >
                          <div className="flex items-center gap-2">
                            <span className="text-sm text-neutral-400 group-hover:text-neutral-200 transition-colors">{prettifyKey(child)}</span>
                            {(() => {
                              const status = getPageStatus(designDoc[fullKey]);
                              return status ? <span className={`w-1.5 h-1.5 rounded-full ${statusColors[status] || 'bg-workbench-card'}`} /> : null;
                            })()}
                          </div>
                          <p className="text-xs text-neutral-600 truncate">{getPagePreview(designDoc[fullKey])}</p>
                        </button>
                      );
                    })}
                  </div>
                ))}
              </div>
            )}
          </div>
        ) : (
          /* Selected page content */
          <div>
            <div className="flex items-center gap-2 mb-4">
              <button
                onClick={() => setSelectedPage(null)}
                className="text-xs text-neutral-500 hover:text-neutral-300 transition-colors"
              >
                All pages
              </button>
              <span className="text-neutral-700">/</span>
              <span className="text-sm font-medium text-neutral-200">{prettifyKey(selectedPage)}</span>
              {(() => {
                const status = getPageStatus(selectedValue);
                return status ? <span className={`w-2 h-2 rounded-full ${statusColors[status] || 'bg-workbench-card'}`} /> : null;
              })()}
            </div>
            <RenderValue value={selectedValue} />
          </div>
        )}
      </div>
    </div>
  );
}
