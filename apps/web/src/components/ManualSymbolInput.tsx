'use client';

import { useRouter } from 'next/navigation';
import { useState } from 'react';

/**
 * Quick manual symbol entry — BLUEPRINT §4.2 / §11.2.
 *
 * A single ticker jumps to its symbol page; multiple tickers route to the
 * scanner with the list prefilled via query params. Always-on entry point so
 * the user can investigate any symbol without editing their watchlist.
 */
export function ManualSymbolInput() {
  const router = useRouter();
  const [input, setInput] = useState('');

  const onSubmit = (e: React.FormEvent) => {
    e.preventDefault();
    const symbols = input
      .split(/[\s,]+/)
      .map((s) => s.trim().toUpperCase())
      .filter(Boolean);
    if (symbols.length === 0) return;
    if (symbols.length === 1) {
      router.push(`/symbols/${encodeURIComponent(symbols[0])}`);
    } else {
      const qs = new URLSearchParams({ symbols: symbols.join(',') });
      router.push(`/scanner?${qs.toString()}`);
    }
    setInput('');
  };

  return (
    <form
      onSubmit={onSubmit}
      className="rounded-lg border border-line bg-bg-surface p-3"
    >
      <label className="mb-2 block text-xs uppercase tracking-wider text-ink-muted">
        Quick scan — enter symbols
      </label>
      <div className="flex gap-2">
        <input
          className="flex-1 rounded border border-line bg-bg px-3 py-1.5 font-mono text-sm"
          placeholder="AAPL, NVDA, TSLA"
          value={input}
          onChange={(e) => setInput(e.target.value)}
          autoComplete="off"
          spellCheck={false}
        />
        <button
          type="submit"
          className="rounded bg-accent px-3 py-1.5 text-sm font-medium text-bg"
        >
          Go
        </button>
      </div>
      <p className="mt-2 text-xs text-ink-subtle">
        One ticker opens its signal page; multiple tickers run the scanner.
      </p>
    </form>
  );
}
