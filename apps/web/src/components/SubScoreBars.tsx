'use client';

import type { SubScores } from '@/lib/types';

const ENGINES: Array<{ key: keyof SubScores; label: string }> = [
  { key: 'tech',  label: 'TECH'  },
  { key: 'quant', label: 'QUANT' },
  { key: 'news',  label: 'NEWS'  },
  { key: 'sent',  label: 'SENT'  },
  { key: 'macro', label: 'MACRO' },
  { key: 'opt',   label: 'OPT'   },
  { key: 'liq',   label: 'LIQ'   },
  { key: 'risk',  label: 'RISK'  },
];

export function SubScoreBars({ subs, composite }: { subs: SubScores; composite?: number }) {
  return (
    <div className="space-y-1.5 font-mono text-xs">
      {composite !== undefined && (
        <Row
          label="COMPOSITE"
          score={composite}
          emphasize
        />
      )}
      <div className="h-px bg-line" />
      {ENGINES.map((e) => (
        <Row key={e.key} label={e.label} score={subs[e.key]} />
      ))}
    </div>
  );
}

function Row({ label, score, emphasize = false }: { label: string; score: number; emphasize?: boolean }) {
  // Score in [-100, +100]; map to bar width [0, 50%] from the center line.
  const pct = Math.min(50, (Math.abs(score) / 100) * 50);
  const isBull = score >= 0;
  const colorBg = isBull ? 'bg-bull-soft' : 'bg-bear-soft';
  const colorBorder = isBull ? 'border-bull' : 'border-bear';
  const colorText = isBull ? 'text-bull' : 'text-bear';

  return (
    <div className={`flex items-center gap-3 ${emphasize ? 'font-semibold' : ''}`}>
      <span className="w-20 text-ink-muted">{label}</span>
      <div className="relative h-3 flex-1 bar-track rounded-sm overflow-hidden">
        <div
          className={`absolute top-0 h-full ${colorBg} border-y border-r ${colorBorder}`}
          style={
            isBull
              ? { left: '50%', width: `${pct}%` }
              : { right: '50%', width: `${pct}%` }
          }
        />
      </div>
      <span className={`w-12 text-right ${colorText}`}>
        {score >= 0 ? '+' : ''}
        {score.toFixed(0)}
      </span>
    </div>
  );
}
