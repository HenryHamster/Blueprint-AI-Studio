import { type Dispatch, type SetStateAction, type RefObject } from 'react';
import { MoreVertical, Pencil, Trash2 } from 'lucide-react';
import { type SavedView } from './savedViews';

export interface SavedViewsTabStripProps {
  savedViews: SavedView[];
  activeViewId: string;
  defaultViewId: string;
  activeTabModified: boolean;
  openKebabId: string | null;
  setOpenKebabId: Dispatch<SetStateAction<string | null>>;
  kebabRef: RefObject<HTMLDivElement | null>;
  onApplyView: (viewId: string) => void;
  onRenameView: (view: SavedView) => void;
  onDeleteView: (viewId: string) => void;
}

/**
 * Saved-views tab strip: renders the Default tab plus one tab per saved view,
 * the active-tab highlight, the "filters modified" dot, and the per-view
 * Rename/Delete kebab menu. Purely presentational — all state and persistence
 * stay owned by MessagesPanel and are wired through props.
 */
export function SavedViewsTabStrip({
  savedViews,
  activeViewId,
  defaultViewId,
  activeTabModified,
  openKebabId,
  setOpenKebabId,
  kebabRef,
  onApplyView,
  onRenameView,
  onDeleteView,
}: SavedViewsTabStripProps) {
  const tabs: { id: string; name: string; isDefault: boolean; view: SavedView | null }[] = [
    { id: defaultViewId, name: 'Default', isDefault: true, view: null },
    ...savedViews.map(v => ({ id: v.id, name: v.name, isDefault: false, view: v })),
  ];
  return (
    <div
      className="flex items-center gap-1 overflow-x-auto -mx-1 px-1"
      data-testid="saved-views-tab-strip"
      data-viewport-allow-small-target
    >
      {tabs.map(tab => {
        const isActive = activeViewId === tab.id;
        const showModified = isActive && activeTabModified;
        return (
          <div key={tab.id} className="relative shrink-0 flex items-center">
            <button
              onClick={() => onApplyView(tab.id)}
              title={tab.view?.description || tab.name}
              className={`flex items-center gap-1 px-2.5 py-1 rounded-md text-xs transition-colors border min-h-[44px] md:min-h-[36px] ${
                isActive
                  ? 'bg-blue-500/15 text-blue-300 border-blue-500/40'
                  : 'text-neutral-500 hover:text-neutral-300 border-transparent hover:border-neutral-800/60'
              }`}
            >
              <span>{tab.name}</span>
              {showModified && (
                <span title="Filters changed" className="w-1.5 h-1.5 rounded-full bg-amber-400" data-testid="view-modified-dot" />
              )}
            </button>
            {!tab.isDefault && tab.view && (
              <div ref={openKebabId === tab.id ? kebabRef : undefined} className="relative">
                <button
                  onClick={() => setOpenKebabId(prev => (prev === tab.id ? null : tab.id))}
                  title="Manage view"
                  aria-label={`Manage view ${tab.name}`}
                  className="text-neutral-600 hover:text-neutral-300 p-1 rounded transition-colors min-h-[44px] min-w-[44px] md:min-h-auto md:min-w-auto flex items-center justify-center"
                >
                  <MoreVertical className="w-3.5 h-3.5" />
                </button>
                {openKebabId === tab.id && (
                  <div className="absolute right-0 top-full mt-1 z-50 min-w-[120px] bg-workbench-base border border-neutral-700 rounded-lg shadow-xl overflow-hidden">
                    <button
                      onClick={() => tab.view && onRenameView(tab.view)}
                      className="w-full flex items-center gap-2 px-3 py-2 text-xs text-neutral-300 hover:bg-workbench-hover transition-colors min-h-[44px]"
                    >
                      <Pencil className="w-3 h-3" />
                      Rename
                    </button>
                    <button
                      onClick={() => onDeleteView(tab.id)}
                      className="w-full flex items-center gap-2 px-3 py-2 text-xs text-red-400 hover:bg-red-500/10 transition-colors min-h-[44px]"
                    >
                      <Trash2 className="w-3 h-3" />
                      Delete
                    </button>
                  </div>
                )}
              </div>
            )}
          </div>
        );
      })}
    </div>
  );
}
