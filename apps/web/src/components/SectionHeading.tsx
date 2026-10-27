import type { ReactNode } from 'react';

/** The small upper-case heading that opens a block on Today ("YOUR POSITIONS: ...", "TRADE IDEAS ..."). */
export const SECTION_HEADING_CLASS = 'text-sm font-semibold uppercase tracking-wider';

export function SectionHeading({ id, children }: { id: string; children: ReactNode }) {
  return (
    <h2 id={id} className={SECTION_HEADING_CLASS}>
      {children}
    </h2>
  );
}
