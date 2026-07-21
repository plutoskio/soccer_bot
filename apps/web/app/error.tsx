"use client";

export default function ErrorBoundary({ reset }: { error: Error; reset: () => void }) {
  return (
    <main className="system-state">
      <p className="section-label">Interface error</p>
      <h1>Something interrupted the forecast view.</h1>
      <button className="primary-button" type="button" onClick={reset}>Try again</button>
    </main>
  );
}
