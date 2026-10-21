'use client';

import {
  useCallback,
  useEffect,
  useId,
  useLayoutEffect,
  useRef,
  useState,
  type ReactNode,
} from 'react';

export type TermKey =
  | 'R'
  | 'ATR'
  | 'RS'
  | 'EXPECTANCY'
  | 'PROFIT_FACTOR'
  | 'CAGR'
  | 'MAX_DRAWDOWN'
  | 'SHARPE'
  | 'EXPOSURE';

// Plain-English definitions for the jargon the dashboard cannot avoid.
export const TERM_TEXT: Record<TermKey, string> = {
  R: 'R = the amount you risk on the trade (buy price minus stop). +2R means you made twice what you risked.',
  ATR: "ATR = the stock's average daily price range over the last 14 days.",
  RS: "Relative strength: how this stock's past-year performance ranks against all US stocks in the universe (99 = strongest).",
  // The Track record page's measures.
  EXPECTANCY: 'Average result per trade, in R.',
  PROFIT_FACTOR: 'Total gains divided by total losses; above 1 means gains outweighed losses.',
  CAGR: 'Compound annual growth rate of the simulated account.',
  MAX_DRAWDOWN: 'Largest fall from a peak in the simulated account.',
  SHARPE: 'Return per unit of volatility (annualised); higher is steadier.',
  EXPOSURE: 'Share of days the simulated account held at least one position.',
};

// `useLayoutEffect` warns when a page is rendered on the server; there the effect would do nothing.
const useIsomorphicLayoutEffect = typeof window === 'undefined' ? useEffect : useLayoutEffect;

const WIDTH = 256; // the tooltip's preferred width, px
const MARGIN = 16; // it keeps at least this far from the viewport edge, px
const GAP = 4; // between the term and the tooltip, px

/**
 * A term with its definition as a tooltip.
 *
 * The tooltip shows while the mouse is over the term, while it has keyboard focus (Tab) and after
 * a tap (a tap focuses it); Escape dismisses it. The term points at the tooltip with
 * `aria-describedby`, so a screen reader reads the definition once (there is no `title`).
 *
 * The tooltip is `position: fixed` and placed from the term's bounding box, so it is never
 * clipped by a scrolling table around the term and never overflows the viewport's width: it is at
 * most `100vw - 2 * MARGIN` wide, kept inside both side edges, and flips above the term when there
 * is no room below. It follows the term when the page or a table scrolls, or the window resizes.
 */
export function Term({ k, children }: { k: TermKey; children: ReactNode }) {
  const id = useId();
  const text = TERM_TEXT[k];
  const triggerRef = useRef<HTMLSpanElement>(null);
  const tipRef = useRef<HTMLSpanElement>(null);
  const [hovered, setHovered] = useState(false);
  const [focused, setFocused] = useState(false);
  const [dismissed, setDismissed] = useState(false);
  const open = (hovered || focused) && !dismissed;

  // `left`, `top` and `width` are written straight to the element: they come from measurements of
  // the term and the viewport, not from React state.
  const place = useCallback(() => {
    const trigger = triggerRef.current;
    const tip = tipRef.current;
    if (trigger === null || tip === null) return;
    const viewWidth = document.documentElement.clientWidth;
    const viewHeight = window.innerHeight;
    const width = Math.max(0, Math.min(WIDTH, viewWidth - 2 * MARGIN));
    const box = trigger.getBoundingClientRect();
    tip.style.width = `${width}px`;
    tip.style.left = `${Math.max(MARGIN, Math.min(box.left, viewWidth - width - MARGIN))}px`;
    const height = tip.offsetHeight;
    const below = box.bottom + GAP;
    const above = box.top - GAP - height;
    tip.style.top = `${below + height > viewHeight - GAP && above >= GAP ? above : below}px`;
  }, []);

  // Place the tooltip before the browser paints it, and again whenever the term moves under it.
  useIsomorphicLayoutEffect(() => {
    if (open) place();
  }, [open, place]);

  useEffect(() => {
    if (!open) return;
    const onKey = (event: KeyboardEvent) => {
      if (event.key === 'Escape') setDismissed(true);
    };
    document.addEventListener('keydown', onKey);
    window.addEventListener('scroll', place, true);
    window.addEventListener('resize', place);
    return () => {
      document.removeEventListener('keydown', onKey);
      window.removeEventListener('scroll', place, true);
      window.removeEventListener('resize', place);
    };
  }, [open, place]);

  return (
    <span className="inline-block">
      <span
        ref={triggerRef}
        tabIndex={0}
        aria-describedby={id}
        onMouseEnter={() => {
          setDismissed(false);
          setHovered(true);
        }}
        onMouseLeave={() => setHovered(false)}
        onFocus={() => {
          setDismissed(false);
          setFocused(true);
        }}
        onBlur={() => setFocused(false)}
        className="cursor-help border-b border-dotted border-ink-subtle outline-none focus-visible:border-accent focus-visible:text-accent"
      >
        {children}
      </span>
      <span
        ref={tipRef}
        id={id}
        role="tooltip"
        className={`pointer-events-none fixed z-50 max-w-[calc(100vw-2rem)] rounded border border-line-strong bg-bg-subtle px-2.5 py-2 text-left text-xs font-normal normal-case leading-snug tracking-normal text-ink shadow-lg ${
          open ? 'block' : 'hidden'
        }`}
      >
        {text}
      </span>
    </span>
  );
}
