import type { ReactNode } from 'react';
import { Term } from '@/components/Term';

/**
 * Text from the API with each "ATR" given its tooltip. The words are left exactly as sent: only
 * the three letters are wrapped, so "ATR10 / ATR50" still reads "ATR10 / ATR50".
 */
export function TermText({ children }: { children: string }): ReactNode {
  const parts = children.split(/(ATR)/);
  if (parts.length === 1) return children;
  return parts.map((part, index) =>
    part === 'ATR' ? (
      // Parts alternate text, term, text, ...; the position is the identity.
      <Term key={index} k="ATR">
        ATR
      </Term>
    ) : (
      part
    ),
  );
}
