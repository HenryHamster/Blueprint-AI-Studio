import { Component, type ReactNode } from 'react';

interface Props {
  children: ReactNode;
  fallback?: ReactNode;
}

interface State {
  error: Error | null;
}

export class ErrorBoundary extends Component<Props, State> {
  state: State = { error: null };

  static getDerivedStateFromError(error: Error): State {
    return { error };
  }

  componentDidCatch(error: Error, info: { componentStack: string }) {
    console.error('[ErrorBoundary] Caught error:', error, info.componentStack);
  }

  render() {
    if (this.state.error) {
      if (this.props.fallback) return this.props.fallback;
      return (
        <div className="h-screen flex items-center justify-center bg-neutral-900">
          <div className="bg-neutral-800 border border-neutral-700 rounded-xl p-8 text-center max-w-md">
            <h2 className="text-lg font-semibold text-neutral-100 mb-2">Something went wrong</h2>
            <p className="text-xs text-neutral-400 mb-6 font-mono break-all">
              {this.state.error.message}
            </p>
            <button
              onClick={() => window.location.reload()}
              className="bg-amber-500 hover:bg-amber-400 text-neutral-950 px-6 py-2 rounded text-sm font-semibold transition-colors"
            >
              Reload
            </button>
          </div>
        </div>
      );
    }
    return this.props.children;
  }
}
