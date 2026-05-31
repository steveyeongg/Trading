'use client';

import { useStream } from '@/lib/stream';

const LABEL: Record<string, string> = {
  live: 'live',
  connecting: 'connecting',
  offline: 'offline',
};
const COLOR: Record<string, string> = {
  live: 'bg-bull',
  connecting: 'bg-warn',
  offline: 'bg-bear',
};

export function LiveDot() {
  const { status } = useStream();
  return (
    <span className="flex items-center gap-1.5 font-mono text-xs text-ink-subtle">
      <span className={`inline-block h-2 w-2 rounded-full ${COLOR[status]} ${status === 'live' ? 'animate-pulse' : ''}`} />
      {LABEL[status]}
    </span>
  );
}
