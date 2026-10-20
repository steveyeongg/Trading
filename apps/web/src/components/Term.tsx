'use client';

import { useId, type ReactNode } from 'react';

export type TermKey = 'R' | 'ATR' | 'RS';

// Plain-English definitions for the jargon the dashboard cannot avoid.
export const TERM_TEXT: Record<TermKey, string> = {
  R: 'R = the amount you risk on the trade (buy price minus stop). +2R means you made twice what you risked.',
  ATR: "ATR = the stock's average daily price range over the last 14 days.",
  RS: "Relative strength: how this stock's past-year performance ranks against all US stocks in the universe (99 = strongest).",
};

/**
 * A term with its definition as a tooltip. The `title` shows on mouse hover; the term itself is
 * focusable (Tab, or a tap) and shows the same text beneath it, and `aria-describedby` makes a
 * screen reader read it.
 */
export function Term({ k, children }: { k: TermKey; children: ReactNode }) {
  const id = useId();
  const text = TERM_TEXT[k];
  return (
    <span className="group relative inline-block">
      <span
        tabIndex={0}
        title={text}
        aria-describedby={id}
        className="cursor-help border-b border-dotted border-ink-subtle outline-none focus-visible:border-accent focus-visible:text-accent"
      >
        {children}
      </span>
      <span
        id={id}
        role="tooltip"
        className="pointer-events-none absolute left-0 top-full z-20 mt-1 hidden w-64 max-w-[calc(100vw-3rem)] rounded border border-line-strong bg-bg-subtle px-2.5 py-2 text-left text-xs font-normal normal-case leading-snug tracking-normal text-ink shadow-lg group-focus-within:block"
      >
        {text}
      </span>
    </span>
  );
}
