// What a page shows instead of an empty area when the API can't be reached.
export const API_DOWN_TEXT = "Can't reach the ATLAS API. Start it with scripts/atlas.sh up.";

export function ApiDown() {
  return (
    <div
      role="alert"
      className="rounded border border-warn/40 bg-bg-surface px-4 py-3 text-sm text-ink"
    >
      {API_DOWN_TEXT}
    </div>
  );
}
