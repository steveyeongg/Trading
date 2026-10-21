/**
 * The dashboard's palette as hex strings, for code that cannot use a Tailwind class (a canvas
 * chart). Each value is the one in `tailwind.config.ts` under the token named beside it; change
 * a token there and change it here too.
 */
export const CHART_COLORS = {
  surface: '#13171b', // bg-surface
  grid: '#1a1f25', // bg-subtle
  border: '#262d35', // line
  borderStrong: '#3a434e', // line-strong
  inkMuted: '#8a96a3', // ink-muted
  bull: '#22c55e', // bull
  bear: '#ef4444', // bear
  warn: '#f59e0b', // warn
  accent: '#5eead4', // accent
} as const;
