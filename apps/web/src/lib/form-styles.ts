// Classes the app's forms and buttons share (Positions, Settings, and the Run scan now button).

export const INPUT_CLASS =
  'w-full rounded border border-line bg-bg-subtle px-2 py-1.5 text-sm text-ink placeholder:text-ink-subtle focus:border-accent focus:outline-none aria-invalid:border-bear';
export const PRIMARY_BUTTON_CLASS =
  'rounded border border-accent-muted px-4 py-1.5 text-sm font-medium text-accent hover:bg-accent/10 focus-visible:outline focus-visible:outline-1 focus-visible:outline-accent disabled:cursor-not-allowed disabled:opacity-50';
export const SECONDARY_BUTTON_CLASS =
  'rounded border border-line-strong px-3 py-1.5 text-sm text-ink hover:bg-bg-subtle focus-visible:outline focus-visible:outline-1 focus-visible:outline-accent disabled:cursor-not-allowed disabled:opacity-50';
/** "Run scan now", on Today (RegimeBanner) and in the status panel on Settings. */
export const RUN_SCAN_BUTTON_CLASS =
  'rounded border border-accent-muted px-3 py-1.5 text-sm text-accent hover:bg-accent/10 focus-visible:outline focus-visible:outline-1 focus-visible:outline-accent disabled:cursor-not-allowed disabled:opacity-50 disabled:hover:bg-transparent';
