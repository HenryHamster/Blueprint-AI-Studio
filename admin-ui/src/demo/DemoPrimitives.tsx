import type { ReactNode } from 'react';
import { statusLabels } from './model';
import type { DemoTask } from './model';

export function Stat({
  label,
  value,
  note,
  icon,
}: {
  label: string;
  value: number;
  note: string;
  icon: ReactNode;
}) {
  return (
    <div className="stat">
      <div>
        {label}
        {icon}
      </div>
      <strong>{String(value).padStart(2, '0')}</strong>
      <p>{note}</p>
    </div>
  );
}

export function Status({ task }: { task: DemoTask }) {
  return (
    <span className={`status status-${task.status.toLowerCase()}`}>
      <span />
      {statusLabels[task.status]}
    </span>
  );
}
