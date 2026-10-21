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

/** What a section says under its heading when its list could not be loaded (the reason is above). */
export const SECTION_NOT_LOADED_TEXT = 'Not loaded: see the message above.';

/** By the stop input on an open position. */
export const STOP_EDIT_HINT = 'ATLAS re-checks this position with your new stop right away.';
export const newStopLabel = (currentStop: string) => `New stop (now ${currentStop})`;

// What the page announces (and moves focus to) after an action, so focus never drops to the body.
export const recordedStatusText = (symbol: string) => `Recorded ${symbol}`;
export const stopSavedStatusText = (symbol: string) => `Stop saved for ${symbol}`;
export const closedStatusText = (symbol: string, result: string) => `Closed ${symbol}: ${result}`;

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
export const BOUGHT_DATE_FUTURE_TEXT = "Bought on can't be after today (New York date)";
export const FILL_INVALID_TEXT = 'Fill price must be more than 0';
export const SHARES_INVALID_TEXT = 'Shares must be more than 0';
export const STOP_INVALID_TEXT = 'Enter your stop price: it must be more than 0';
export const STOP_NOT_BELOW_FILL_TEXT = 'The stop must be below your fill price';
export const STOP_UNCHANGED_TEXT = "That's already your current stop";
export const SOLD_DATE_REQUIRED_TEXT = 'Enter the date you sold';
export const SOLD_DATE_FUTURE_TEXT = "Sold on can't be after today (New York date)";
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

// --- Track record page ------------------------------------------------------------------------

export const TRACK_RECORD_LOADING_TEXT = 'Loading the track record…';
export const TRACK_RECORD_INTRO_TEXT =
  "Every setup is tested on 2016–2022 (in-sample), then once on 2023 onward (out-of-sample) under rules sealed before the test. Only setups that pass are Live. Live and reconstructed columns track tonight's ideas forward.";
/** The API answered with no setup families at all. */
export const TRACK_RECORD_EMPTY_TEXT = 'No setups to show yet.';

// The verdict badge, and what the verdict means for the ideas on Today.
export const VERDICT_LIVE_LABEL = 'Live';
/** Heads the list of every condition a Research-only setup failed. */
export const FAILED_HEADING = 'Failed:';
export const VERDICT_NOT_TESTED_LABEL = 'Not yet tested';
export const VERDICT_LIVE_NOTE = 'Passed testing: its ideas appear under Trade ideas on Today.';
export const VERDICT_UNPROVEN_NOTE =
  'Has not passed testing: its ideas appear on Today as Unproven and are left out of the Telegram digest.';

// The card's section headings.
export const SECTION_RESULTS_TEXT = 'Results';
export const SECTION_EQUITY_TEXT = 'Simulated account against SPY';
export const SECTION_VERSIONS_TEXT = 'Versions tested';

// The results table.
// "Results in R (risk units).": the R in between is a <Term> on the page.
export const RESULTS_CAPTION_PREFIX = 'Results in';
export const RESULTS_CAPTION_SUFFIX = '(risk units).';
export const RESULTS_CAPTION_NOTE =
  'In-sample and out-of-sample columns are simulated on historical data.';
export const COLUMN_SUBTITLES = {
  insample: '2016–2022, simulated',
  oos: '2023 onward, simulated',
  live: 'ideas tracked forward',
  reconstructed: 'rebuilt after a missed day',
} as const;
export const NOT_RUN_TEXT = 'Not run yet';
export const NO_LIVE_IDEAS_TEXT = 'No closed live ideas yet';
export const NO_RECONSTRUCTED_IDEAS_TEXT = 'No closed reconstructed ideas yet';
export const STATS_FOOTNOTE_TEXT =
  'Live = ideas published before the next session opened. Reconstructed = ideas rebuilt after a missed day; they never count as live.';
export const stillOpenText = (n: string) => `${n} still open`;
export const distinctNoteText = (trades: string, expectancy: string) =>
  `Distinct trades only: ${trades} trades, expectancy ${expectancy}`;

// The equity chart and the account simulation under it.
export const EQUITY_NONE_TEXT = 'No equity series in this report.';
export const EQUITY_STRATEGY_LABEL = 'Strategy (out-of-sample, simulated)';
export const EQUITY_SPY_LABEL = 'SPY';
export const equityCaptionText = (from: string, to: string) =>
  `Weekly account value, ${from} to ${to}. Historical and simulated: the setup's rules, sizing and position limits applied to past data. SPY is a buy-and-hold account of the same starting size.`;
export const equityDescriptionText = (parts: string[]) =>
  `Weekly account value, out-of-sample and simulated. ${parts.join(' ')}`;
export const PORTFOLIO_CAPTION_TEXT = 'Out-of-sample account simulation, strategy against SPY';
export const SPY_ROW_LABEL = 'SPY (buy and hold)';
export const COMBINED_ROW_LABEL = 'Combined';
export const combinedNoteText = (note: string) => `Combined portfolio: ${note}`;

// The random-entry comparison. The numbers arrive signed ("+0.09") and the copy adds the R.
export function randomEntryText(median: string, p5: string | null, p95: string | null): string {
  const range = p5 !== null && p95 !== null ? ` (5–95%: ${p5}R to ${p95}R)` : '';
  return `Random picks with the same exits: median ${median}R${range}`;
}
export const randomCompareText = (expectancy: string, above: boolean) =>
  `Out-of-sample expectancy ${expectancy} is ${above ? 'above' : 'not above'} the random-pick median.`;

// Versions: each setup version, failed ones included.
export const NO_VERSIONS_TEXT = 'No versions tested yet.';
export function versionText(
  version: string,
  runIndex: number | null,
  frozen: string | null,
  passed: boolean | null,
): string {
  const parts = [version];
  if (runIndex !== null) parts.push(`run ${runIndex}`);
  if (frozen !== null) parts.push(`frozen ${frozen}`);
  parts.push(passed ? 'passed' : passed === false ? 'failed' : 'not run');
  return parts.join(' · ');
}

// --- Settings page ----------------------------------------------------------------------------

export const SETTINGS_LOADING_TEXT = 'Loading your settings…';
export const SETTINGS_INTRO_TEXT =
  'These set how many shares each idea suggests and how much risk you allow. ATLAS only suggests trades; it never places or changes an order.';
export const LEGACY_SETTINGS_LINK_TEXT = 'Legacy alert and provider settings';

export const SETTINGS_ACCOUNT_HEADING = 'ACCOUNT AND RISK';
export const SETTINGS_UNIVERSE_HEADING = 'UNIVERSE FILTERS';
export const SETTINGS_TELEGRAM_HEADING = 'TELEGRAM';

/**
 * Each number field of the form: its label (with the unit), the bounds as helper text (the
 * server's, from `SwingSettings`), and what it says when the typed value is out of bounds.
 * Percent fields are typed as % and sent as a fraction; the dollar volume is typed in millions.
 */
export const SETTINGS_FIELD_TEXT = {
  equity: {
    label: 'Account equity ($)',
    hint: 'The account size used to work out share counts. Any amount above $0.',
    invalid: 'Account equity must be a number above $0',
  },
  risk_pct: {
    label: 'Risk per trade (%)',
    hint: "How much of your equity one trade may lose if it hits its stop. Above 0%, up to 5%.",
    invalid: 'Risk per trade must be above 0% and at most 5%',
  },
  max_position_pct: {
    label: 'Largest position (% of equity)',
    hint: 'The most one position may cost. Above 0%, up to 100%.',
    invalid: 'Largest position must be above 0% and at most 100%',
  },
  max_positions: {
    label: 'Most open positions',
    hint: 'How many positions can be open at once. A whole number from 1 to 50.',
    invalid: 'Most open positions must be a whole number from 1 to 50',
  },
  max_total_risk_pct: {
    label: 'Total open risk cap (% of equity)',
    hint: 'The most all open positions may lose together if every stop is hit. Above 0%, up to 50%.',
    invalid: 'Total open risk cap must be above 0% and at most 50%',
  },
  min_price: {
    label: 'Minimum stock price ($)',
    hint: 'Ideas skip stocks priced below this. At least $1.',
    invalid: 'Minimum stock price must be a number, at least $1',
  },
  min_dollar_volume: {
    label: 'Minimum daily dollar volume ($ million)',
    hint: 'Ideas skip stocks that trade less than this per day, in millions of dollars. At least 1 ($1 million).',
    invalid: 'Minimum daily dollar volume must be a number, at least 1 ($ million)',
  },
} as const;

/** Beside the universe fields when either differs from the default the setups were validated on. */
export const UNIVERSE_WARNING_TEXT =
  'Setups were validated with the default universe filters ($10, $20M). With different filters their track record may not apply.';

export const SETTINGS_SAVE_LABEL = 'Save settings';
export const SETTINGS_SAVING_LABEL = 'Saving…';
export const SETTINGS_SAVED_TEXT = 'Settings saved.';
export const SETTINGS_NO_CHANGES_TEXT = 'No unsaved changes.';
export const SETTINGS_FIX_FIELDS_TEXT = 'Fix the highlighted fields, then save again.';

export const TELEGRAM_DIGEST_LABEL = 'Send a nightly Telegram digest of validated ideas';
export const TELEGRAM_DIGEST_NOTE = 'Unproven ideas are never included. Saves as soon as you change it.';
export const TELEGRAM_TEST_LABEL = 'Send test message';
export const TELEGRAM_TESTING_LABEL = 'Sending…';
export const TELEGRAM_NOT_CONFIGURED_HINT = 'Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID in .env';
/** The test's answer, inline: "Sent: ..." or "Not sent: ...". */
export const telegramTestResultText = (ok: boolean, detail: string) =>
  detail.trim() === '' ? (ok ? 'Sent' : 'Not sent') : `${ok ? 'Sent' : 'Not sent'}: ${detail.trim()}`;

// --- System status panel ----------------------------------------------------------------------

export const STATUS_HEADING = 'SYSTEM STATUS';
export const STATUS_LOADING_TEXT = 'Loading system status…';
export const STATUS_NO_DATA_TEXT = 'No scan has finished yet';
export const statusDataAsOfText = (date: string) => `Data as of ${date} close`;
export const STATUS_NO_SCAN_TEXT = 'Last scan: none yet';
export const STATUS_STEPS_LABEL = 'Steps of the last scan';
export const STATUS_STEP_NOT_RUN_TEXT = 'not run';
export const STATUS_AUTO_EXECUTION_OFF_TEXT = 'Automatic order placement: off';
export const STATUS_AUTO_EXECUTION_ON_TEXT = 'Automatic order placement: ON (ATLAS_ENABLE_AUTO_EXECUTION=1)';
export const STATUS_CONFIGURED_TEXT = 'configured';
export const STATUS_NOT_SET_TEXT = 'not set';
export const RUN_SCAN_LABEL = 'Run scan now';

/** The six steps of a scan, in order, with what each one does. */
export const STATUS_STEPS = [
  { name: 'assets', label: 'Refresh the stock list' },
  { name: 'bars', label: 'Download daily bars' },
  { name: 'scan', label: 'Scan for ideas' },
  { name: 'tracking', label: 'Update tracked ideas' },
  { name: 'positions', label: 'Check your positions' },
  { name: 'digest', label: 'Send the Telegram digest' },
] as const;

const SCAN_KIND_LABELS: Record<string, string> = {
  nightly: 'nightly',
  catchup: 'catch-up',
  manual: 'manual',
};
const SCAN_STATUS_LABELS: Record<string, string> = {
  ok: 'completed',
  failed: 'failed',
  running: 'running',
};
const has = (table: Record<string, string>, key: string) => Object.prototype.hasOwnProperty.call(table, key);

/** "Last scan: nightly, completed": the kind and status as the job recorded them (an unknown value is shown as is). */
export function statusLastScanText(kind: string, status: string): string {
  const kindText = has(SCAN_KIND_LABELS, kind) ? SCAN_KIND_LABELS[kind] : kind;
  const statusText = has(SCAN_STATUS_LABELS, status) ? SCAN_STATUS_LABELS[status] : status;
  return `Last scan: ${kindText}, ${statusText}`;
}
export const statusScanTimesText = (started: string | null, finished: string | null) =>
  [started === null ? null : `started ${started}`, finished === null ? null : `finished ${finished}`]
    .filter((part) => part !== null)
    .join(' · ');
export const statusNextScanText = (when: string) => `Next scan: ${when}`;
export const STATUS_NEXT_SCAN_AFTER_RUN_TEXT = 'Next scan: scheduled once this scan ends';

/**
 * Why automatic scans are off. The server words its reasons as full sentences that start
 * "Automatic scans ..."; one that does not is prefixed, and none at all is a plain statement.
 */
export function schedulerOffText(reason: string | null): string {
  const text = (reason ?? '').trim();
  if (text === '') return 'Automatic scans are off.';
  return /^automatic scans\b/i.test(text) ? text : `Automatic scans are off: ${text}`;
}

/** "1,234 of 6,000 symbols (20%)": the download's progress. */
export const backfillLabelText = (done: string, total: string, percent: number) =>
  `${done} of ${total} symbols (${percent}%)`;
