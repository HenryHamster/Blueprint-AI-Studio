import { memo, lazy, Suspense } from 'react';
import type { MarkdownContentInnerProps } from './MarkdownContentInner';

const LazyMarkdownInner = lazy(() => import('./MarkdownContentInner'));

function MarkdownContentImpl({ content, onOpenTask, onOpenDirection, githubRepo }: MarkdownContentInnerProps) {
  return (
    <Suspense fallback={<span className="block min-h-[1em]" aria-hidden />}>
      <LazyMarkdownInner content={content} onOpenTask={onOpenTask} onOpenDirection={onOpenDirection} githubRepo={githubRepo} />
    </Suspense>
  );
}

export const MarkdownContent = memo(MarkdownContentImpl);
