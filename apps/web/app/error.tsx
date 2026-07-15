"use client";

export default function ErrorBoundary({ reset }: { error: Error; reset: () => void }) {
  return (
    <main className="system-state">
      <div className="wordmark"><span aria-hidden="true">SB</span> Soccer Bot</div>
      <p className="eyebrow">Interface error</p>
      <h1>The desk stopped before showing an unreliable state.</h1>
      <button className="retry-button" type="button" onClick={reset}>Retry snapshot</button>
    </main>
  );
}
