export default function Loading() {
  return (
    <main className="loading-state" aria-busy="true">
      <span className="loading-mark" aria-hidden="true" />
      <p>Loading forecasts</p>
    </main>
  );
}
