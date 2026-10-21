import type { ReactNode } from 'react';
import { Term } from '@/components/Term';

// "ATR" as a word on its own. "ATR10" and "ATR50" name other windows (10 and 50 days), not the
// 14-day average the tooltip defines, and "ATR" inside a longer word is not the term at all.
const ATR_WORD = /(\bATR\b)/;

/**
 * Text from the API with each stand-alone "ATR" given its tooltip. The words are left exactly as
 * sent: only the three letters are wrapped, so "ATR10 / ATR50" is not touched.
 */
export function TermText({ children }: { children: string }): ReactNode {
  // Splitting on a pattern with one group gives text, term, text, term, ...: the odd entries are terms.
  const parts = children.split(ATR_WORD);
  if (parts.length === 1) return children;
  return parts.map((part, index) =>
    index % 2 === 1 ? (
      <Term key={index} k="ATR">
        {part}
      </Term>
    ) : (
      part
    ),
  );
}
