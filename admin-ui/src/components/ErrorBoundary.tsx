import { Component, type ErrorInfo, type ReactNode } from 'react';
import { AlertTriangle, RefreshCw } from 'lucide-react';

interface ErrorBoundaryProps {
  children: ReactNode;
  fallback?: ReactNode | ((error: Error, reset: () => void) => ReactNode);
  level?: 'app' | 'panel' | 'widget';
  onError?: (error: Error, errorInfo: ErrorInfo) => void;
}

interface ErrorBoundaryState {
  error: Error | null;
}

export class ErrorBoundary extends Component<ErrorBoundaryProps, ErrorBoundaryState> {
  state: ErrorBoundaryState = { error: null };

  static getDerivedStateFromError(error: Error): ErrorBoundaryState {
    return { error };
  }

  componentDidCatch(error: Error, errorInfo: ErrorInfo) {
    console.error(`[ErrorBoundary] Caught error:`, error, errorInfo);
    this.props.onError?.(error, errorInfo);
  }

  reset = () => {
    this.setState({ error: null });
  };

  render() {
    if (!this.state.error) return this.props.children;

    if (this.props.fallback) {
      const node =
        typeof this.props.fallback === 'function'
          ? this.props.fallback(this.state.error, this.reset)
          : this.props.fallback;
      return <div data-error-boundary-fallback style={{ display: 'contents' }}>{node}</div>;
    }

    const level = this.props.level ?? 'panel';

    if (level === 'app') {
      return (
        <div data-error-boundary-fallback className="min-h-screen bg-workbench-shell flex items-center justify-center p-8">
          <div className="max-w-md w-full bg-workbench-base border border-red-900/50 rounded-lg p-6 text-center">
            <AlertTriangle className="w-12 h-12 text-red-400 mx-auto mb-4" />
            <h1 className="text-xl font-semibold text-neutral-200 mb-2">Something went wrong</h1>
            <p className="text-sm text-neutral-400 mb-4">{this.state.error.message}</p>
            <button
              onClick={() => window.location.reload()}
              className="inline-flex items-center gap-2 px-4 py-2 bg-red-900/30 hover:bg-red-900/50 text-red-300 rounded-md text-sm transition-colors"
            >
              <RefreshCw className="w-4 h-4" />
              Reload page
            </button>
          </div>
        </div>
      );
    }

    if (level === 'widget') {
      return (
        <div data-error-boundary-fallback className="border border-red-900/50 rounded-md p-2 text-center bg-red-950/20">
          <p className="text-xs text-red-400">Error rendering</p>
          <button onClick={this.reset} className="text-xs text-red-300 underline mt-1">Retry</button>
        </div>
      );
    }

    // panel level (default)
    return (
      <div data-error-boundary-fallback className="border border-red-900/50 rounded-lg p-4 bg-red-950/20">
        <div className="flex items-center gap-2 mb-2">
          <AlertTriangle className="w-5 h-5 text-red-400" />
          <h3 className="text-sm font-medium text-red-300">Something went wrong</h3>
        </div>
        <p className="text-xs text-neutral-400 mb-3">{this.state.error.message}</p>
        <button
          onClick={this.reset}
          className="inline-flex items-center gap-1.5 px-3 py-1.5 bg-red-900/30 hover:bg-red-900/50 text-red-300 rounded-md text-xs transition-colors"
        >
          <RefreshCw className="w-3 h-3" />
          Retry
        </button>
      </div>
    );
  }
}
