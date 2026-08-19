import type { ReactNode } from 'react';

interface ModalShellProps {
  children: ReactNode;
  onClose: () => void;
  maxWidth?: string;
}

/** Shared modal overlay: fixed backdrop + centered card container. Clicking the backdrop calls onClose. */
export function ModalShell({ children, onClose, maxWidth = 'max-w-sm' }: ModalShellProps) {
  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 backdrop-blur-sm"
      onClick={onClose}
    >
      <div
        className={`bg-workbench-base border border-neutral-700 rounded-lg p-6 ${maxWidth} w-full mx-4 shadow-2xl`}
        onClick={e => e.stopPropagation()}
      >
        {children}
      </div>
    </div>
  );
}
