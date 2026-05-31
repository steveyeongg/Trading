'use client';

import { useQueryClient } from '@tanstack/react-query';
import {
  createContext,
  useContext,
  useEffect,
  useRef,
  useState,
  type ReactNode,
} from 'react';
import type { RegimeSnapshot, SignalDebug } from './types';

// WebSocket connects DIRECTLY to the backend (Next.js rewrites don't proxy
// WS cleanly). Configure via NEXT_PUBLIC_ATLAS_WS_URL; defaults to :8000.
const WS_URL =
  process.env.NEXT_PUBLIC_ATLAS_WS_URL || 'ws://localhost:8000/v1/stream';

type Status = 'connecting' | 'live' | 'offline';

interface StreamCtx {
  status: Status;
  subscribe: (subjects: string[]) => void;
}

const Ctx = createContext<StreamCtx>({ status: 'offline', subscribe: () => {} });

export function useStream(): StreamCtx {
  return useContext(Ctx);
}

/**
 * Subscribe to live subjects for the lifetime of the calling component.
 * The provider feeds messages straight into the React Query cache, so any
 * component reading the matching queryKey updates live — no prop drilling.
 */
export function useLiveSubjects(subjects: string[]): void {
  const { subscribe } = useStream();
  const key = subjects.join(',');
  useEffect(() => {
    if (subjects.length) subscribe(subjects);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [key]);
}

export function StreamProvider({ children }: { children: ReactNode }) {
  const queryClient = useQueryClient();
  const [status, setStatus] = useState<Status>('connecting');
  const wsRef = useRef<WebSocket | null>(null);
  const pendingSubs = useRef<Set<string>>(new Set());
  const reconnectRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  const flushSubs = () => {
    const ws = wsRef.current;
    if (ws && ws.readyState === WebSocket.OPEN && pendingSubs.current.size) {
      ws.send(JSON.stringify({ subscribe: Array.from(pendingSubs.current) }));
    }
  };

  const subscribe = (subjects: string[]) => {
    let changed = false;
    for (const s of subjects) {
      if (!pendingSubs.current.has(s)) {
        pendingSubs.current.add(s);
        changed = true;
      }
    }
    if (changed) flushSubs();
  };

  useEffect(() => {
    let closedByUs = false;

    const connect = () => {
      setStatus('connecting');
      let ws: WebSocket;
      try {
        ws = new WebSocket(WS_URL);
      } catch {
        setStatus('offline');
        scheduleReconnect();
        return;
      }
      wsRef.current = ws;

      ws.onopen = () => {
        setStatus('live');
        // Re-send all subscriptions on (re)connect.
        flushSubs();
      };

      ws.onmessage = (ev) => {
        try {
          const { subject, data } = JSON.parse(ev.data) as { subject: string; data: unknown };
          if (subject === 'regime.global') {
            queryClient.setQueryData<RegimeSnapshot>(['regime'], data as RegimeSnapshot);
          } else if (subject.startsWith('signals.')) {
            const sym = subject.split('.', 2)[1];
            // The stream pushes {signal, veto}; the debug query key carries the
            // same shape (minus snapshots). Merge so the watchlist updates live.
            queryClient.setQueryData<SignalDebug>(['debug', sym], (prev) => ({
              signal: (data as SignalDebug).signal,
              veto: (data as SignalDebug).veto,
              macro_snapshot: prev?.macro_snapshot ?? null,
              sentiment_snapshot: prev?.sentiment_snapshot ?? null,
            }));
          }
        } catch {
          /* ignore malformed frame */
        }
      };

      ws.onclose = () => {
        if (!closedByUs) {
          setStatus('offline');
          scheduleReconnect();
        }
      };

      ws.onerror = () => {
        ws.close();
      };
    };

    const scheduleReconnect = () => {
      if (reconnectRef.current) return;
      reconnectRef.current = setTimeout(() => {
        reconnectRef.current = null;
        connect();
      }, 3000);
    };

    connect();

    return () => {
      closedByUs = true;
      if (reconnectRef.current) clearTimeout(reconnectRef.current);
      wsRef.current?.close();
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  return <Ctx.Provider value={{ status, subscribe }}>{children}</Ctx.Provider>;
}
