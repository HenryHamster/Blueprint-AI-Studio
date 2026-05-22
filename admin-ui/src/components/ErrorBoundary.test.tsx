import { describe, it, expect, vi } from 'vitest';
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { ErrorBoundary } from './ErrorBoundary';

function ThrowingComponent({ shouldThrow }: { shouldThrow: boolean }) {
  if (shouldThrow) throw new Error('Test error');
  return <div>No error</div>;
}

describe('ErrorBoundary', () => {
  // Suppress React error boundary console.error noise
  const originalError = console.error;
  beforeEach(() => { console.error = vi.fn(); });
  afterEach(() => { console.error = originalError; });

  it('renders children when no error', () => {
    render(
      <ErrorBoundary level="panel">
        <div>Hello</div>
      </ErrorBoundary>
    );
    expect(screen.getByText('Hello')).toBeInTheDocument();
  });

  it('renders app-level fallback on error', () => {
    render(
      <ErrorBoundary level="app">
        <ThrowingComponent shouldThrow />
      </ErrorBoundary>
    );
    expect(screen.getByText('Something went wrong')).toBeInTheDocument();
    expect(screen.getByText('Test error')).toBeInTheDocument();
    expect(screen.getByText('Reload page')).toBeInTheDocument();
  });

  it('renders panel-level fallback with retry button', () => {
    render(
      <ErrorBoundary level="panel">
        <ThrowingComponent shouldThrow />
      </ErrorBoundary>
    );
    expect(screen.getByText('Something went wrong')).toBeInTheDocument();
    expect(screen.getByText('Retry')).toBeInTheDocument();
  });

  it('renders widget-level fallback', () => {
    render(
      <ErrorBoundary level="widget">
        <ThrowingComponent shouldThrow />
      </ErrorBoundary>
    );
    expect(screen.getByText('Error rendering')).toBeInTheDocument();
  });

  it('calls onError callback', () => {
    const onError = vi.fn();
    render(
      <ErrorBoundary level="panel" onError={onError}>
        <ThrowingComponent shouldThrow />
      </ErrorBoundary>
    );
    expect(onError).toHaveBeenCalledOnce();
    expect(onError.mock.calls[0][0]).toBeInstanceOf(Error);
    expect(onError.mock.calls[0][0].message).toBe('Test error');
  });

  it('recovers after clicking retry on panel level', async () => {
    const user = userEvent.setup();
    let shouldThrow = true;

    function Toggler() {
      if (shouldThrow) throw new Error('oops');
      return <div>Recovered</div>;
    }

    const { rerender } = render(
      <ErrorBoundary level="panel">
        <Toggler />
      </ErrorBoundary>
    );
    expect(screen.getByText('Retry')).toBeInTheDocument();

    shouldThrow = false;
    await user.click(screen.getByText('Retry'));

    rerender(
      <ErrorBoundary level="panel">
        <Toggler />
      </ErrorBoundary>
    );
    expect(screen.getByText('Recovered')).toBeInTheDocument();
  });

  it('app-level fallback has data-error-boundary-fallback attribute', () => {
    const { container } = render(
      <ErrorBoundary level="app">
        <ThrowingComponent shouldThrow />
      </ErrorBoundary>
    );
    expect(container.querySelector('[data-error-boundary-fallback]')).toBeInTheDocument();
  });

  it('panel-level fallback has data-error-boundary-fallback attribute', () => {
    const { container } = render(
      <ErrorBoundary level="panel">
        <ThrowingComponent shouldThrow />
      </ErrorBoundary>
    );
    expect(container.querySelector('[data-error-boundary-fallback]')).toBeInTheDocument();
  });

  it('widget-level fallback has data-error-boundary-fallback attribute', () => {
    const { container } = render(
      <ErrorBoundary level="widget">
        <ThrowingComponent shouldThrow />
      </ErrorBoundary>
    );
    expect(container.querySelector('[data-error-boundary-fallback]')).toBeInTheDocument();
  });

  it('custom fallback has data-error-boundary-fallback attribute', () => {
    const { container } = render(
      <ErrorBoundary fallback={<div>Custom</div>}>
        <ThrowingComponent shouldThrow />
      </ErrorBoundary>
    );
    expect(container.querySelector('[data-error-boundary-fallback]')).toBeInTheDocument();
  });

  it('uses custom fallback function', () => {
    render(
      <ErrorBoundary fallback={(error, reset) => (
        <div>
          <span>Custom: {error.message}</span>
          <button onClick={reset}>Reset</button>
        </div>
      )}>
        <ThrowingComponent shouldThrow />
      </ErrorBoundary>
    );
    expect(screen.getByText('Custom: Test error')).toBeInTheDocument();
  });
});
