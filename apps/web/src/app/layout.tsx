import type { Metadata } from 'next';
import Link from 'next/link';
import './globals.css';
import { Providers } from './providers';
import { NavLinks } from '@/components/NavLinks';
import { StockSearch } from '@/components/StockSearch';

export const metadata: Metadata = {
  title: 'ATLAS · Swing trade ideas',
  description: 'Rules-based daily swing trade ideas for US stocks.',
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <body className="min-h-screen bg-bg text-ink">
        <Providers>
          <header className="border-b border-line bg-bg-surface">
            {/* Phone: the logo and the search on one row, the nav wrapped onto its own row. */}
            <div className="mx-auto flex max-w-7xl flex-wrap items-center gap-x-6 gap-y-2 px-6 py-3">
              <Link href="/" className="font-mono text-lg font-semibold tracking-tight text-accent">
                ATLAS
              </Link>
              <NavLinks />
              <div className="ml-auto flex items-center gap-4">
                <StockSearch />
                <span className="hidden font-mono text-xs text-ink-subtle md:inline">v0.1.0</span>
              </div>
            </div>
          </header>
          <main className="mx-auto max-w-7xl px-6 py-6">{children}</main>
        </Providers>
      </body>
    </html>
  );
}
