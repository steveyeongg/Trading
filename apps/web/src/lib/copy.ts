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

/** `plural(1, 'trade')` is "trade", `plural(2, 'trade')` is "trades". */
export const plural = (n: number, one: string, many = `${one}s`) => (n === 1 ? one : many);
