'use client';

import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { useState, type ReactNode } from 'react';
import { StreamProvider } from '@/lib/stream';

export function Providers({ children }: { children: ReactNode }) {
  // Per Next.js App Router guidance: instantiate QueryClient inside a
  // useState so it's stable across re-renders but isolated per request
  // (server) / per browser instance (client).
  const [client] = useState(
    () =>
      new QueryClient({
        defaultOptions: {
          queries: {
            // Bars / regime / sentiment update on different cadences; the
            // dashboard refetches every 30s and on focus for a market feel.
            refetchInterval: 30_000,
            refetchOnWindowFocus: true,
            staleTime: 10_000,
            retry: 1,
          },
        },
      })
  );
  return (
    <QueryClientProvider client={client}>
      <StreamProvider>{children}</StreamProvider>
    </QueryClientProvider>
  );
}
