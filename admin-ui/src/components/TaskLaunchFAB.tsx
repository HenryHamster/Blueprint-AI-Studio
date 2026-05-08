import { Plus } from 'lucide-react';

interface TaskLaunchFABProps {
  onClick: () => void;
  visible: boolean;
}

export function TaskLaunchFAB({ onClick, visible }: TaskLaunchFABProps) {
  if (!visible) return null;

  return (
    <>
      {/* Fixed FAB positioned at viewport bottom-right: intentionally extends beyond viewport bounds on small screens */}
      <div
        className="fixed bottom-0 right-0 p-4 sm:hidden z-40 pointer-events-none"
        style={{ paddingBottom: 'max(1rem, env(safe-area-inset-bottom))' }}
        data-viewport-allow-overflow
      >
      <button
        onClick={onClick}
        className="w-14 h-14 rounded-full bg-white hover:bg-neutral-200 text-neutral-950 shadow-lg flex items-center justify-center transition-colors pointer-events-auto"
        title="Launch new task"
      >
        <Plus className="w-6 h-6" />
      </button>
      </div>
    </>
  );
}
