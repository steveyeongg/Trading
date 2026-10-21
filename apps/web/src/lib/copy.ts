// Words and small text helpers that several pages share.

/** What a page shows instead of an empty area when the API can't be reached. */
export const API_DOWN_TEXT = "Can't reach the ATLAS API. Start it with scripts/atlas.sh up.";

/** The line at the foot of every page of ideas. */
export const DISCLAIMER_TEXT = 'Informational only, not financial advice. You place every order yourself.';

// The stock page's words for a stock it cannot show.
export const stockLoadingText = (symbol: string) => `Loading ${symbol}…`;
export const stockNotFoundText = (symbol: string) =>
  `No stock called ${symbol} in the US-listed universe.`;
export const STOCK_NOT_FOUND_HINT =
  'Check the ticker, then try another with the "Check a stock" box at the top of the page.';
/** What the stock page says when the address has no ticker in it (for example /stocks/%20). */
export const STOCK_EMPTY_SYMBOL_TEXT = 'Type a ticker in Check a stock.';
export const STOCK_WATCH_ONLY_TEXT = 'Watch-only (market in downtrend)';
export const STOCK_WATCH_ONLY_NOTE =
  'This is not a buy idea tonight, so no share count or risk is shown.';
/** Under the chart of a stock with no idea: the server then draws only an open position's levels. */
export const STOCK_POSITION_LINES_NOTE = 'The price lines are for your open position.';
export const CHART_NO_BARS_TEXT = 'No daily bars to draw for this stock yet.';
/** Above the checklist of a stock that has an idea: why a rule of the other setup may be red. */
export const otherSetupRulesText = (setupLabel: string) =>
  `Rules for both setups are listed; this idea only needs the ${setupLabel} rules.`;

/** `plural(1, 'trade')` is "trade", `plural(2, 'trade')` is "trades". */
export const plural = (n: number, one: string, many = `${one}s`) => (n === 1 ? one : many);

/**
 * The line above the checklist of a stock that has no idea tonight: how many of its `total` rules
 * it failed, or that it passes them all and still no setup triggered.
 */
export function checklistSummaryText(symbol: string, total: number, failed: number): string {
  if (total === 0) return `No rules could be checked for ${symbol} tonight.`;
  if (failed === 0) return `${symbol} passes the universe rules but no setup triggered tonight.`;
  return `${symbol} doesn't qualify tonight: ${failed} ${plural(failed, 'rule')} failed.`;
}

// --- Positions page ---------------------------------------------------------------------------

export const POSITIONS_LOADING_TEXT = 'Loading your positions…';
export const POSITIONS_INTRO_TEXT =
  'Record trades you placed yourself with your broker. Each night ATLAS checks them and tells you what to do next. It never places or changes an order.';
export const NO_OPEN_POSITIONS_TEXT =
  "No open positions. Record a trade from any idea's stock page, or use Add a position.";
export const NO_CLOSED_POSITIONS_TEXT = 'No closed trades yet.';
/** A position the nightly check has not looked at yet (the Latest action cell would be empty). */
export const NO_ACTION_YET_TEXT = 'Not checked yet: ATLAS checks it after the next scan.';

/** By the stop input on an open position. */
export const STOP_EDIT_HINT = 'ATLAS re-checks this position with your new stop right away.';
export const STOP_SAVED_TEXT = 'Stop updated.';

// The add form.
export const ADD_FORM_NOTE =
  'Enter the trade as you made it: your real fill price, shares and the stop you set.';
export const ideaLineText = (setupLabel: string, buyAbove: string) =>
  `From tonight's ${setupLabel} idea: buy above ${buyAbove}`;
export const fillPlaceholderText = (buyAbove: string | null) =>
  buyAbove === null ? 'your actual fill' : `your actual fill, e.g. ${buyAbove}`;
export const IDEA_LOADING_TEXT = "Loading tonight's idea…";
export const IDEA_MISMATCH_TEXT =
  "That idea isn't in tonight's scan any more, so enter your stop and shares yourself.";
export const IDEA_LOAD_FAILED_TEXT =
  "Couldn't load that idea, so enter your stop and shares yourself.";
export const riskPreviewText = (risk: string, below: string) =>
  `Risk ${risk} · stop ${below} below your fill`;
export const SHOW_EXISTING_POSITION_TEXT = 'Show that position';

// Field checks, before anything is sent.
export const SYMBOL_REQUIRED_TEXT = 'Enter the stock symbol';
export const SYMBOL_INVALID_TEXT = 'Use letters, numbers, "." or "-" only, up to 16 characters';
export const BOUGHT_DATE_REQUIRED_TEXT = 'Enter the date you bought';
export const FILL_INVALID_TEXT = 'Fill price must be more than 0';
export const SHARES_INVALID_TEXT = 'Shares must be more than 0';
export const STOP_INVALID_TEXT = 'Enter your stop price: it must be more than 0';
export const STOP_NOT_BELOW_FILL_TEXT = 'The stop must be below your fill price';
export const STOP_UNCHANGED_TEXT = "That's already your current stop";
export const SOLD_DATE_REQUIRED_TEXT = 'Enter the date you sold';
export const SOLD_BEFORE_BOUGHT_TEXT = 'Sold on must be on or after the buy date';
export const EXIT_INVALID_TEXT = 'Exit price must be more than 0';

// The close form.
export const CLOSE_FORM_NOTE =
  'Record the sale you made with your broker. ATLAS does not sell anything.';
export const closedResultText = (symbol: string, exitPrice: string) =>
  `Closed ${symbol} at ${exitPrice}`;

/** The exit reasons the close form offers (all optional). `value` is what is stored. */
export const EXIT_REASONS = [
  { value: 'stop', label: 'Stop hit' },
  { value: 'trail', label: 'Trail stop' },
  { value: 'max_hold', label: 'Max hold' },
  { value: 'took_profit', label: 'Took profit' },
  { value: 'other', label: 'Other' },
] as const;

/** A stored exit reason in words; a value this build does not know is shown as it was stored. */
export function exitReasonLabel(reason: string | null): string {
  if (reason === null || reason.trim() === '') return '—';
  return EXIT_REASONS.find((o) => o.value === reason)?.label ?? reason;
}
