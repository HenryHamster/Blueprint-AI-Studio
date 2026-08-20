export interface MessagesBatchActionBarProps {
  selectedCount: number;
  batchCreating: boolean;
  onCreateDirections: () => void;
}

/**
 * Batch-action bar shown above the composer when select-mode is active and at
 * least one feedback message is selected. Purely presentational — selection
 * state and the create-directions handler stay owned by MessagesPanel.
 */
export function MessagesBatchActionBar({ selectedCount, batchCreating, onCreateDirections }: MessagesBatchActionBarProps) {
  return (
    <div className="px-4 md:px-6 py-2 border-t border-neutral-800 bg-workbench-base flex items-center gap-3 shrink-0">
      <span className="text-xs text-neutral-400">{selectedCount} selected</span>
      <button
        onClick={onCreateDirections}
        disabled={batchCreating}
        className="text-xs bg-purple-600 hover:bg-purple-500 text-white px-3 py-1.5 rounded-lg disabled:opacity-40 transition-colors min-h-[44px] min-w-[44px] md:min-h-[36px] md:min-w-auto flex items-center justify-center"
      >
        {batchCreating ? 'Creating...' : 'Create Directions'}
      </button>
    </div>
  );
}
