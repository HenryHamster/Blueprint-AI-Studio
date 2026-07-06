import { memo, lazy, Suspense } from 'react';

const LazyMarkdownInner = lazy(() => import('./MarkdownContentInner'));

function MarkdownContentImpl({ content }: { content: string }) {
  return (
    <Suspense fallback={<span className="block min-h-[1em]" aria-hidden />}>
      <LazyMarkdownInner content={content} />
    </Suspense>
  );
}

export const MarkdownContent = memo(MarkdownContentImpl);
