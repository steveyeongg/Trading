// Words and small text helpers that several pages share.

/** What a page shows instead of an empty area when the API can't be reached. */
export const API_DOWN_TEXT = "Can't reach the ATLAS API. Start it with scripts/atlas.sh up.";

/** `plural(1, 'trade')` is "trade", `plural(2, 'trade')` is "trades". */
export const plural = (n: number, one: string, many = `${one}s`) => (n === 1 ? one : many);
