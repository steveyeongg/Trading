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
