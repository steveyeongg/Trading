import type { Metadata } from 'next';
import Link from 'next/link';
import './globals.css';
import { Providers } from './providers';
import { LiveDot } from '@/components/LiveDot';
import { TierBadge } from '@/components/TierBadge';

export const metadata: Metadata = {
  title: 'ATLAS — Trading Intelligence',
  description: 'Multi-asset signal engine.',
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <body className="min-h-screen bg-bg text-ink">
        <Providers>
          <header className="border-b border-line bg-bg-surface">
            <div className="mx-auto flex max-w-7xl items-center gap-6 px-6 py-3">
              <Link href="/" className="font-mono text-lg font-semibold tracking-tight text-accent">
                ATLAS
              </Link>
              <nav className="flex gap-4 text-sm text-ink-muted">
                <Link href="/" className="hover:text-ink">Dashboard</Link>
                <Link href="/backtest" className="hover:text-ink">Backtest</Link>
                <Link href="/portfolio" className="hover:text-ink">Portfolio</Link>
                <Link href="/journal" className="hover:text-ink">Journal</Link>
                <Link href="/settings" className="hover:text-ink">Settings</Link>
              </nav>
              <div className="ml-auto flex items-center gap-4">
                <LiveDot />
                <TierBadge />
                <span className="text-xs font-mono text-ink-subtle">v0.1.0</span>
              </div>
            </div>
          </header>
          <main className="mx-auto max-w-7xl px-6 py-6">{children}</main>
        </Providers>
      </body>
    </html>
  );
}
