'use client';

import Link from 'next/link';
import { usePathname } from 'next/navigation';

const LINKS = [
  { href: '/', label: 'Today' },
  { href: '/positions', label: 'Positions' },
  { href: '/track-record', label: 'Track record' },
  { href: '/settings', label: 'Settings' },
] as const;

const isActive = (pathname: string, href: string) =>
  href === '/' ? pathname === '/' : pathname === href || pathname.startsWith(`${href}/`);

/** The main navigation. It wraps rather than scrolls, so it fits a phone-width screen. */
export function NavLinks() {
  const pathname = usePathname() ?? '';
  return (
    <nav
      aria-label="Main"
      className="order-last flex w-full flex-wrap gap-x-5 gap-y-1 text-sm md:order-none md:w-auto"
    >
      {LINKS.map(({ href, label }) => {
        const active = isActive(pathname, href);
        return (
          <Link
            key={href}
            href={href}
            aria-current={active ? 'page' : undefined}
            className={`whitespace-nowrap border-b py-1 ${
              active
                ? 'border-accent text-ink'
                : 'border-transparent text-ink-muted hover:text-ink'
            }`}
          >
            {label}
          </Link>
        );
      })}
    </nav>
  );
}
