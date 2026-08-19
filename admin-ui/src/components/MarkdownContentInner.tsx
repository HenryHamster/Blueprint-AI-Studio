import ReactMarkdown, { defaultUrlTransform } from 'react-markdown';
import remarkGfm from 'remark-gfm';
import remarkMath from 'remark-math';
import rehypeKatex from 'rehype-katex';
import type { Components } from 'react-markdown';
import { hasMathDelimiters, normalizeMathDelimiters } from '../utils/mathContent';
import { CITATION_URL_PREFIX, linkifyCitations, parseCitationHref } from '../utils/citationLinks';

// react-markdown's default urlTransform strips any scheme it doesn't recognize
// (http/https/mailto/relative) to prevent XSS — our internal citation:// scheme
// needs an explicit allowance, everything else still goes through the default.
function urlTransform(url: string): string {
  return url.startsWith(CITATION_URL_PREFIX) ? url : defaultUrlTransform(url);
}

export interface MarkdownContentInnerProps {
  content: string;
  onOpenTask?: (taskId: string) => void;
  onOpenDirection?: (directionId: string) => void;
  githubRepo?: string | null;
}

// Lazy KaTeX CSS: only import when content contains math delimiters.
// KaTeX CSS is ~30KB; eagerly loading it on every view inflates the initial
// bundle on routes that never render math.
let _katexCssLoaded = false;
function ensureKaTexCss(): void {
  if (_katexCssLoaded) return;
  _katexCssLoaded = true;
  import('katex/dist/katex.min.css').catch(() => { /* CSS import failure is non-fatal */ });
}

function buildMdComponents(
  onOpenTask?: (taskId: string) => void,
  onOpenDirection?: (directionId: string) => void,
  githubRepo?: string | null,
): Components {
  return {
    ...baseMdComponents,
    a: ({ children, href, ...props }) => {
      const citation = href ? parseCitationHref(href) : null;
      if (citation) {
        if (citation.kind === 'task' && onOpenTask) {
          return (
            <button type="button" onClick={() => onOpenTask(citation.id)} className="text-blue-400 hover:text-blue-300 underline underline-offset-2">
              {children}
            </button>
          );
        }
        if (citation.kind === 'direction' && onOpenDirection) {
          return (
            <button type="button" onClick={() => onOpenDirection(citation.id)} className="text-blue-400 hover:text-blue-300 underline underline-offset-2">
              {children}
            </button>
          );
        }
        if (citation.kind === 'pr' && githubRepo) {
          return (
            <a className="text-blue-400 hover:text-blue-300 underline break-all" href={`https://github.com/${githubRepo}/pull/${citation.id}`} target="_blank" rel="noopener noreferrer">
              {children}
            </a>
          );
        }
        // Target cannot be resolved (no handler/repo wired) — degrade to the
        // original literal text rather than rendering a raw citation:// href.
        return <>{children}</>;
      }
      return <a className="text-blue-400 hover:text-blue-300 underline break-all" href={href} target="_blank" rel="noopener noreferrer" {...props}>{children}</a>;
    },
  };
}

const baseMdComponents: Components = {
  h1: ({ children, ...props }) => <h1 className="text-base font-bold text-neutral-100 mt-4 mb-2" {...props}>{children}</h1>,
  h2: ({ children, ...props }) => <h2 className="text-sm font-semibold text-neutral-200 mt-3 mb-1.5" {...props}>{children}</h2>,
  h3: ({ children, ...props }) => <h3 className="text-sm font-medium text-neutral-300 mt-2 mb-1" {...props}>{children}</h3>,
  p: ({ children, ...props }) => <p className="text-sm text-neutral-300 leading-relaxed mb-2" {...props}>{children}</p>,
  code: ({ children, ...props }) => {
    const isBlock = String(children).includes('\n');
    if (isBlock) return <code className="text-xs font-mono" {...props}>{children}</code>;
    return <code className="bg-workbench-panel text-neutral-200 px-1 py-0.5 rounded text-xs font-mono break-all" {...props}>{children}</code>;
  },
  pre: ({ children, ...props }) => <pre className="bg-workbench-base border border-neutral-800 rounded-lg p-3 overflow-x-auto text-xs mb-2" data-viewport-allow-overflow data-viewport-allow-truncation {...props}>{children}</pre>,
  ul: ({ children, ...props }) => <ul className="list-disc list-inside space-y-0.5 text-sm text-neutral-300 mb-2" {...props}>{children}</ul>,
  ol: ({ children, ...props }) => <ol className="list-decimal list-inside space-y-0.5 text-sm text-neutral-300 mb-2" {...props}>{children}</ol>,
  li: ({ children, ...props }) => <li className="text-sm text-neutral-300" {...props}>{children}</li>,
  blockquote: ({ children, ...props }) => <blockquote className="border-l-2 border-neutral-700 pl-3 text-sm text-neutral-400 italic mb-2" {...props}>{children}</blockquote>,
  table: ({ children, ...props }) => <div className="overflow-x-auto mb-2"><table className="text-xs text-neutral-300 border-collapse w-full" {...props}>{children}</table></div>,
  th: ({ children, ...props }) => <th className="border border-neutral-700 px-2 py-1 text-left text-neutral-200 bg-workbench-panel/50" {...props}>{children}</th>,
  td: ({ children, ...props }) => <td className="border border-neutral-700 px-2 py-1" {...props}>{children}</td>,
  strong: ({ children, ...props }) => <strong className="text-neutral-100 font-semibold" {...props}>{children}</strong>,
  hr: (props) => <hr className="border-neutral-800 my-3" {...props} />,
};

export default function MarkdownContentInner({ content, onOpenTask, onOpenDirection, githubRepo }: MarkdownContentInnerProps) {
  const hasMath = hasMathDelimiters(content);
  if (hasMath) ensureKaTexCss();

  const cited = linkifyCitations(content);
  const normalized = hasMath ? normalizeMathDelimiters(cited) : cited;
  const components = buildMdComponents(onOpenTask, onOpenDirection, githubRepo);

  return (
    <ReactMarkdown
      remarkPlugins={[remarkGfm, remarkMath]}
      rehypePlugins={[rehypeKatex]}
      components={components}
      urlTransform={urlTransform}
    >
      {normalized}
    </ReactMarkdown>
  );
}
