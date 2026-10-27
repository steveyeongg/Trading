import { API_DOWN_TEXT } from '@/lib/copy';

// What a page shows instead of an empty area when the API can't be reached: the copy is in
// `lib/copy.ts`, so a lib module can use it without importing a component.
export { API_DOWN_TEXT };

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
