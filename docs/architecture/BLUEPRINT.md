# ATLAS — Simplified Trading Intelligence Blueprint

**Codename:** ATLAS — Adaptive Trading Logic & Allocation System  
**Owner:** Steve Yeong  
**Revision:** 2026-05-30  
**Status:** Architecture v2.0

---

## 0. Blunt Positioning

ATLAS should not try to become Bloomberg, TradingView, a hedge-fund OMS, a full multi-agent research lab, and an execution broker all at once.

The immediate winning product is:

> A low-cost stock screener + trading signal engine that scans a watchlist or manually entered symbols, calculates technical/quant/news/macro/sentiment/risk scores, and produces a clear trade plan with entry, stop loss, T1/T2/T3, rationale, dashboard view, and Telegram alerts.

# Table of Contents

1. **Stock screener first.**
2. **Manual symbol input must always work.**
3. **Free/low-cost APIs by default.**
4. **Technical + quant signals are the core.**
5. **News, macro, and sentiment enhance conviction, not replace price action.**
6. **DeepSeek explains and ranks signals; it must not invent prices or override the deterministic engine.**
7. **Dashboard and Telegram alerts remain first-class outputs.**

---

## 1. ATLAS Baseline

Components:

| Area | Capability |
|---|---------------------------------------------------------------------------------------|
| Market data ingest | `ingest-equities` with Polygon → Alpaca fallback and synthetic dev data               |
| Feature engine | 25 technical indicators                                                               |
| Quant engine | XGBoost trend model using triple-barrier labels, walk-forward CV, and calibration     |
| Scoring engine | Technical, quant, liquidity, macro, sentiment, options sub-scores into composite score |
| Risk engine | Kelly, volatility target, ATR risk, correlation caps, VaR/CVaR, veto logic            |
| Explanation engine | DeepSeek via OpenAI-compatible API with cached prompts and templated fallback         |
| Macro engine | FRED with synthetic fallback and regime scoring                                       |
| News ingest | RSS / NewsAPI / file replay                                                           |
| Sentiment engine | Lexicon scorer and optional FinBERT                                                   |
| Options analytics | Black-Scholes, IV, put/call, IV rank, GEX, max pain; currently synthetic chain        |
| Signal service | FastAPI, `/v1/scan`, `/v1/signals/{symbol}`, WebSocket stream, `/metrics`             |
| Dashboard | Next.js 14 dashboard with watchlist, signals, charting, portfolio, journal, settings  |
| Alerts | Log, webhook, Telegram, email                                                         |
| Execution | Paper broker and optional Alpaca paper execution                                      |
| Monitoring | Prometheus + Grafana                                                                  |

---

## 2. Target Product

### 2.1 Product Name

ATLAS Trading Intelligence Engine

### 2.2 Main User Flow

```
User enters symbols manually
        OR
System pulls symbols from screener universe
        ↓
ATLAS fetches latest OHLCV data from free / low-cost APIs
        ↓
Feature engine calculates indicators
        ↓
Quant model produces trend probability
        ↓
News, sentiment, macro, options, liquidity scores are attached
        ↓
Scoring engine ranks opportunities
        ↓
Risk engine builds trade plan
        ↓
DeepSeek explains the reasoning
        ↓
Dashboard displays signals
        ↓
Telegram alerts fire when rules are met
```

### 2.3 Core Output

Every accepted signal should return:

```json
{
  "symbol": "AAPL",
  "direction": "long",
  "signal_type": "swing",
  "composite_score": 78,
  "confidence": 0.72,
  "conviction": "high",
  "entry_price": 214.30,
  "stop_loss": 206.10,
  "targets": {
    "t1": 222.50,
    "t2": 230.00,
    "t3": 238.00
  },
  "risk_reward": 2.4,
  "position_size_pct": 2.1,
  "sub_scores": {
    "technical": 82,
    "quant": 71,
    "macro": 55,
    "sentiment": 64,
    "news": 61,
    "options": 50,
    "liquidity": 90,
    "risk": 76
  },
  "rationale": "Bullish trend continuation with EMA alignment, MACD momentum, acceptable ATR risk, and positive news sentiment.",
  "invalidations": [
    "Close below stop loss",
    "Macro regime turns risk-off",
    "Negative high-impact news"
  ]
}
```

---

## 3. Simplified Architecture

```
                ┌──────────────────────────────┐
                │ Manual Symbols / Screener UI │
                └──────────────┬───────────────┘
                               │
                               ▼
                ┌──────────────────────────────┐
                │ Market Data Providers         │
                │ Alpaca / Polygon / yfinance   │
                │ Alpha Vantage / Tiingo        │
                └──────────────┬───────────────┘
                               │
                               ▼
                ┌──────────────────────────────┐
                │ TimescaleDB bars              │
                └──────────────┬───────────────┘
                               │
                               ▼
┌────────────────────────────────────────────────────────────┐
│ Feature + Intelligence Layer                               │
│                                                            │
│  Technical indicators  → s_tech                            │
│  XGBoost quant model   → s_quant                           │
│  News + sentiment      → s_news / s_sent                    │
│  Macro regime          → s_macro                            │
│  Options analytics     → s_options                          │
│  Liquidity checks      → s_liq                              │
└──────────────────────────────┬─────────────────────────────┘
                               │
                               ▼
                ┌──────────────────────────────┐
                │ Scoring Engine                │
                │ Composite + ranking + gates   │
                └──────────────┬───────────────┘
                               │
                               ▼
                ┌──────────────────────────────┐
                │ Risk Engine                   │
                │ Entry / SL / T1 / T2 / T3     │
                │ Position size / veto          │
                └──────────────┬───────────────┘
                               │
                               ▼
                ┌──────────────────────────────┐
                │ DeepSeek Explanation Engine   │
                │ Rationale + summary + ranking │
                └──────────────┬───────────────┘
                               │
            ┌──────────────────┼──────────────────┐
            ▼                  ▼                  ▼
      Dashboard          Telegram Alerts      API / WebSocket
```

---

## 4. Stock Screener Design

### 4.1 Required Screener Modes

ATLAS must support two modes:

| Mode | Description | Priority |
|---|---|---|
| Manual symbols | User types symbols like `AAPL,NVDA,TSLA,MSFT` | Must-have |
| Screener universe | System screens symbols from predefined universe | Must-have |

### 4.2 Manual Symbol Input

Manual input must be available in:

1. Dashboard watchlist settings.
2. `/v1/watchlist` API.
3. `/v1/scan` request body.
4. CLI scan command.

Example API:

```http
POST /v1/scan
```

```json
{
  "symbols": ["AAPL", "NVDA", "TSLA", "MSFT"],
  "horizon": "swing",
  "min_score": 60,
  "explain": true
}
```

### 4.3 Screener Universe

Start with simple built-in universes:

| Universe | Source |
|---|---|
| Default watchlist | AAPL, MSFT, NVDA, TSLA, SPY, QQQ |
| US mega-cap | Static CSV first |
| S&P 500 | Static CSV first, later auto-refresh |
| NASDAQ 100 | Static CSV first |
| User custom | Dashboard and API |
| Crypto optional | BTC, ETH only until proper crypto pipeline is revisited |

### 4.4 Low-Cost API Provider Priority

Use this priority order:

| Purpose | Preferred Provider | Cost Strategy |
|---|---|---|
| Equities OHLCV | Alpaca IEX feed | Free with brokerage account |
| Equities fallback | Polygon | Free / low-cost tier where possible |
| Offline dev | Synthetic GBM bars | Free |
| Macro | FRED | Free |
| News | RSS first, NewsAPI optional | Free / low-cost |
| Sentiment | Internal lexicon first | Free |
| Fundamentals optional | Financial Modeling Prep / Finnhub | Add later |
| Options optional | Synthetic first, Polygon/Unusual Whales later | Add later |

Do **not** require expensive data providers for MVP.

---

## 5. Technical Analysis Indicators

### 5.1 Indicators

`feature-engine`

Group:

| Category | Indicators |
|---|---|
| Momentum | RSI, MACD, Stochastic |
| Trend | EMA stack, ADX, Ichimoku |
| Volatility | ATR, Bollinger Bands, realized volatility |
| Volume | VWAP, OBV |
| Structure | Smart Money Concepts BOS, divergences |
| Multi-timeframe | MTF confirmation |
| Liquidity / flow | VWAP, volume behaviour, realized volatility |

### 5.2 Add / Ensure Top Common Indicators

Ensure the following are explicitly present and surfaced in the dashboard/debug output:

| Indicator | Purpose |
|---|---|
| EMA 9 / 21 / 50 / 200 | Trend stack and trend strength |
| SMA 20 / 50 / 200 | Common institutional reference levels |
| RSI 14 | Momentum and overbought/oversold |
| MACD 12/26/9 | Momentum shift and trend confirmation |
| ATR 14 | Stop loss, risk, and volatility |
| Bollinger Bands 20/2 | Volatility expansion and mean reversion |
| VWAP | Intraday fair value / institutional anchor |
| ADX / DI+ / DI- | Trend strength and direction |
| OBV | Volume confirmation |
| Stochastic | Short-term momentum |
| Supertrend | Trend-following confirmation |
| Donchian Channel | Breakout detection |
| Keltner Channel | Volatility channel confirmation |
| Pivot Points | Support / resistance |
| Fibonacci retracement | Target / structure context |
| Relative Volume | Breakout confirmation |
| 52-week high/low distance | Trend context |
| Gap percentage | Risk and breakout context |

### 5.3 Technical Score Formula

Simplify technical scoring into readable weighted blocks:

```text
s_tech =
  25% trend score
+ 20% momentum score
+ 15% volatility score
+ 15% volume score
+ 15% structure score
+ 10% multi-timeframe confirmation
```

Suggested interpretation:

| Score Range | Meaning |
|---|---|
| +70 to +100 | Strong bullish technical setup |
| +40 to +69 | Bullish but needs confirmation |
| -39 to +39 | Neutral / noisy |
| -40 to -69 | Bearish but needs confirmation |
| -70 to -100 | Strong bearish technical setup |

---

## 6. Quant Engine

### 6.1 Quant Engine

1. XGBoost trend model.
2. Triple-barrier labelling.
3. Walk-forward validation.
4. Purged CV / anti-leakage discipline.
5. Isotonic probability calibration.
6. Joblib model registry.
7. `ATLAS_TREND_MODEL` environment variable.

### 6.2 Quant Output

Quant engine should produce:

```json
{
  "p_up": 0.71,
  "p_down": 0.29,
  "s_quant": 42,
  "model_version": "trend/v1",
  "calibrated": true,
  "feature_health": "ok"
}
```

Formula:

```text
s_quant = 100 * (2 * p_up - 1)
```

### 6.3 DeepSeek Must Not Replace Quant

DeepSeek should not decide whether `p_up` is 0.71.  
The quant model calculates probability.  
DeepSeek explains the model and risk context.

---

## 7. News, Macro, Sentiment

### 7.1 News

`news-ingest`

Priority sources:

| Tier | Source |
|---|---|
| Free | RSS feeds |
| Low-cost | NewsAPI |
| Later | Benzinga / MarketAux |
| Enterprise later | RavenPack |

News output:

```json
{
  "symbol": "NVDA",
  "headline_count_24h": 14,
  "positive_count": 8,
  "negative_count": 2,
  "neutral_count": 4,
  "s_news": 58,
  "top_headlines": [
    "Example headline 1",
    "Example headline 2"
  ]
}
```

### 7.2 Sentiment

1. Lexicon scorer as default.
2. Optional FinBERT.
3. Per-ticker aggregation.

Suggested sentiment score:

```text
s_sent =
  50% news sentiment
+ 30% social / retail sentiment
+ 20% analyst drift if available
```

For MVP, social sentiment can be delayed. News sentiment is enough.

### 7.3 Macro

1. FRED integration.
2. Synthetic fallback.
3. Regime detection.
4. `s_macro`.

Macro should affect weighting and veto logic:

| Regime | Behaviour |
|---|---|
| Risk-on | Allow more long signals |
| Risk-off | Increase score threshold and reduce size |
| High-volatility | Widen ATR stops, reduce position size |
| Unknown | Neutral macro score; do not block by default |

---

## 8. Scoring Engine

### 8.1 Core Sub-Scores

Use the following final score components:

| Sub-score | Meaning |
|---|---|
| `s_tech` | Technical indicator score |
| `s_quant` | ML trend probability score |
| `s_news` | News tone and event score |
| `s_sent` | Aggregated sentiment score |
| `s_macro` | Market regime score |
| `s_options` | Options analytics score |
| `s_liq` | Liquidity / tradability score |
| `s_risk` | Portfolio and trade acceptability score |

### 8.2 Default Composite Formula

For stock swing trading:

```text
composite =
  0.30 * s_tech
+ 0.25 * s_quant
+ 0.10 * s_news
+ 0.10 * s_sent
+ 0.10 * s_macro
+ 0.05 * s_options
+ 0.05 * s_liq
+ 0.05 * s_risk
```

### 8.3 Why These Weights

Bluntly: technical and quant should dominate.

News, macro, and sentiment are useful, but they are noisy. They should enhance conviction, not blindly generate trades.

### 8.4 Signal Thresholds

| Composite | Action |
|---|---|
| `>= 75` | Strong long candidate |
| `60 to 74` | Long candidate, needs confirmation |
| `-59 to +59` | No trade |
| `-60 to -74` | Short candidate, needs confirmation |
| `<= -75` | Strong short candidate |

### 8.5 Confirmation Gate

A signal should pass only if:

1. Absolute composite score is at least 60.
2. At least 3 sub-scores confirm the direction.
3. Liquidity score is acceptable.
4. Risk engine does not veto.
5. Latest bars are fresh.
6. No major blackout event is active.

---

## 9. Risk Engine and Trade Plan

### 9.1 Required Trade Plan Fields

Every signal must include:

| Field | Required |
|---|---|
| Direction | Yes |
| Entry price | Yes |
| Stop loss | Yes |
| T1 | Yes |
| T2 | Yes |
| T3 | Yes |
| Risk/reward | Yes |
| Position size % | Yes |
| Invalidation reason | Yes |
| Time stop | Recommended |
| Confidence | Yes |

### 9.2 Entry Price Logic

Simple MVP logic:

| Direction | Entry Logic |
|---|---|
| Long | Current close or pullback to EMA/VWAP support |
| Short | Current close or bounce into EMA/VWAP resistance |

Output both if possible:

```json
{
  "entry_type": "limit",
  "entry_price": 214.30,
  "alternate_entry": 211.80
}
```

### 9.3 Stop Loss Logic

Use the tighter but still realistic stop from:

1. ATR stop.
2. Structural swing low/high.
3. Risk cap stop.

For long:

```text
stop_loss = min(
  entry - 1.5 * ATR14,
  last_swing_low - buffer
)
```

For short:

```text
stop_loss = max(
  entry + 1.5 * ATR14,
  last_swing_high + buffer
)
```

### 9.4 Targets T1 / T2 / T3

Use R-multiple first, then adjust to nearby structure.

For long:

```text
risk_per_share = entry - stop_loss

T1 = entry + 1.0R
T2 = entry + 2.0R
T3 = entry + 3.0R
```

For short:

```text
risk_per_share = stop_loss - entry

T1 = entry - 1.0R
T2 = entry - 2.0R
T3 = entry - 3.0R
```

Default exit ladder:

| Target | Exit % |
|---|---:|
| T1 | 40% |
| T2 | 40% |
| T3 | 20% |

### 9.5 Position Sizing

Use the minimum of:

1. ATR risk sizing.
2. Kelly cap.
3. Volatility target.
4. Per-symbol cap.
5. Correlation cap.

MVP default:

```text
risk_per_trade = 0.5% of portfolio equity
max_position_size = 5% of equity
```

### 9.6 Risk Vetoes

Block signal if:

1. Not enough bars.
2. Spread / liquidity is poor.
3. ATR is too high relative to normal volatility.
4. Correlation to existing portfolio is too high.
5. Macro regime is hostile to the signal direction.
6. News event risk is extreme.
7. Entry to stop distance is unrealistic.
8. Risk/reward is below 1.5.

---

## 10. DeepSeek LLM Optimisation

### 10.1 Role of DeepSeek

DeepSeek should act as:

1. Signal explanation writer.
2. Signal ranking summariser.
3. Risk review assistant.
4. News/macro summariser.
5. Dashboard insight generator.

DeepSeek should **not** be the source of truth for:

1. Indicator values.
2. Quant probability.
3. Stop loss calculation.
4. Target price calculation.
5. Position sizing.
6. Backtest performance.

Those must come from deterministic code and models.

### 10.2 Recommended DeepSeek Models

| Use Case | Model |
|---|---|
| Fast dashboard rationale | `deepseek-chat` |
| More careful signal review | `deepseek-reasoner` |
| Batch screener summary | `deepseek-chat` |
| High-conviction trade explanation | `deepseek-reasoner` |

### 10.3 DeepSeek Prompt Contract

Send structured JSON into DeepSeek. Do not ask it to calculate from raw candles unless needed.

Input:

```json
{
  "system_role": "You are ATLAS, a trading signal explanation engine. You explain deterministic signals. You do not invent prices.",
  "symbol": "AAPL",
  "direction": "long",
  "composite_score": 78,
  "confidence": 0.72,
  "sub_scores": {
    "technical": 82,
    "quant": 71,
    "news": 58,
    "sentiment": 62,
    "macro": 55,
    "options": 50,
    "liquidity": 90,
    "risk": 76
  },
  "trade_plan": {
    "entry": 214.30,
    "stop_loss": 206.10,
    "t1": 222.50,
    "t2": 230.00,
    "t3": 238.00,
    "risk_reward": 2.4
  },
  "indicator_snapshot": {
    "rsi14": 57.2,
    "macd_hist": 1.13,
    "ema_stack": "bullish",
    "atr14": 4.55,
    "adx": 24.8
  },
  "news_summary": [
    "Positive product demand headline",
    "No major negative legal event"
  ],
  "macro_summary": "Risk-on regime, volatility moderate",
  "risk_vetoes": []
}
```

Output schema:

```json
{
  "summary": "One paragraph plain-English explanation.",
  "bull_case": ["Reason 1", "Reason 2", "Reason 3"],
  "bear_case": ["Risk 1", "Risk 2"],
  "why_entry": "Why this entry is valid.",
  "why_stop": "Why this stop loss is logical.",
  "target_logic": "Why T1/T2/T3 make sense.",
  "confidence_comment": "What drives confidence.",
  "final_view": "Actionable but non-advisory conclusion."
}
```

### 10.4 DeepSeek Safety Rules

DeepSeek must obey these rules:

1. Never say “guaranteed”.
2. Never promise profit.
3. Never override risk veto.
4. Never invent news.
5. Never invent missing indicator values.
6. Always mention invalidation.
7. Always include “informational only, not financial advice” in user-facing reports.
8. If data is stale, say signal is stale.

### 10.5 Caching and Cost Control

Cache by:

```text
symbol + horizon + composite bucket + indicator hash + news hash
```

Disable LLM explanations for large scans unless:

```json
{
  "explain": true,
  "top_n": 10
}
```

Use templated fallback when `DEEPSEEK_API_KEY` is missing.

---

## 11. Dashboard Requirements

### 11.1 Dashboard

Required pages:

| Page | Required |
|---|---|
| Watchlist | Yes |
| Scanner | Yes |
| Signal detail | Yes |
| Portfolio | Yes |
| Journal | Yes |
| Alerts | Yes |
| Settings | Yes |
| Backtest | Keep simple |
| Model diagnostics | Add later |

### 11.2 Dashboard Improvements

1. Manual symbol input box.
2. Screener universe selector.
3. Top-ranked signals table.
4. Composite score breakdown.
5. Entry / stop / T1 / T2 / T3 card.
6. Technical indicator panel.
7. News and sentiment panel.
8. Macro regime strip.
9. Risk veto explanation.
10. Telegram alert status.
11. DeepSeek explanation panel.
12. “No signal” explanation when gates fail.

### 11.3 Signal Table Columns

| Column |
|---|
| Symbol |
| Price |
| Direction |
| Composite |
| Confidence |
| Conviction |
| Entry |
| Stop |
| T1 |
| T2 |
| T3 |
| R:R |
| Technical |
| Quant |
| News |
| Macro |
| Risk |
| Last updated |

---

## 12. Telegram Alerts

### 12.1 Telegram Channel

Alert-service Telegram integration.

Environment variables:

```env
TELEGRAM_BOT_TOKEN=
TELEGRAM_CHAT_ID=
```

### 12.2 Alert Trigger

Send Telegram alert when:

1. Composite score crosses threshold.
2. Signal is new or upgraded.
3. Price reaches entry.
4. Price reaches T1/T2/T3.
5. Price hits stop loss.
6. Risk veto changes.
7. Macro regime changes.

### 12.3 Telegram Message Format

```text
🚨 ATLAS Signal: AAPL LONG

Score: 78 | Confidence: 72% | Conviction: HIGH
Entry: 214.30
SL: 206.10
T1: 222.50 | T2: 230.00 | T3: 238.00
R:R: 1:2.4

Why:
- EMA stack bullish
- MACD momentum expanding
- Quant model P(up)=71%
- Risk accepted

Invalidation:
Close below 206.10

Informational only. Not financial advice.
```

---

## 13. API Surface

### 13.1 API

FastAPI endpoints:

```text
GET    /healthz
GET    /readyz
GET    /metrics
GET    /v1/regime
GET    /v1/watchlist
PUT    /v1/watchlist
GET    /v1/symbols/{symbol}/bars
GET    /v1/symbols/{symbol}/options
GET    /v1/signals/{symbol}
GET    /v1/signals/{symbol}/debug
POST   /v1/scan
GET    /v1/alerts
POST   /v1/alerts
DELETE /v1/alerts
GET    /v1/alerts/deliveries
POST   /v1/backtests
GET    /v1/portfolios/{id}
GET    /v1/journal
POST   /v1/execute
GET    /v1/orders
WS     /v1/stream
POST /v1/screener/run
GET  /v1/screener/universes
POST /v1/signals/rank
POST /v1/explain/signal
GET  /v1/providers/status
GET  /v1/data/freshness
```

### 13.2 Screener Endpoint

```http
POST /v1/screener/run
```

```json
{
  "universe": "manual",
  "symbols": ["AAPL", "NVDA", "MSFT"],
  "horizon": "swing",
  "min_composite": 60,
  "include_explanation": true,
  "top_n": 10
}
```

Response:

```json
{
  "run_id": "scan_20260531_001",
  "universe": "manual",
  "results": [
    {
      "rank": 1,
      "symbol": "NVDA",
      "direction": "long",
      "composite": 82,
      "entry": 125.40,
      "stop_loss": 119.20,
      "t1": 131.60,
      "t2": 137.80,
      "t3": 144.00,
      "rationale": "Strong momentum and quant confirmation."
    }
  ]
}
```

---

## 14. Data Storage

| Store | Use |
|---|---|
| TimescaleDB | Bars, OHLCV, signal-related time-series |
| PostgreSQL | Watchlists, alerts, portfolios, orders, journal |
| Redis | Macro cache, live cache, WebSocket state |
| Joblib | Quant model registry |
| File / JSON seed | Local fallback data |
| ClickHouse | Defer until analytics/backtests become too slow |
| Qdrant | Defer until RAG is truly needed |
| Kafka | Defer until multi-replica fanout is necessary |

Blunt rule: **do not add Kafka, ClickHouse, Qdrant, Kubernetes, or full LangGraph until the current MVP is making users happy.**

---

## 15. Backtesting

### 15.1 Backtest

MVP backtest should answer:

1. Did the signal rules work historically?
2. What is the hit rate?
3. What is the average R multiple?
4. What is max drawdown?
5. What is profit factor?
6. Which indicators contributed most?

### 15.2 Required Backtest Metrics

| Metric | Required |
|---|---|
| Total trades | Yes |
| Win rate | Yes |
| Average win | Yes |
| Average loss | Yes |
| Profit factor | Yes |
| Expectancy | Yes |
| Max drawdown | Yes |
| Sharpe | Yes |
| Average R | Yes |
| Best / worst trade | Yes |
| Performance by regime | Later |
| Performance by indicator | Later |

---

## 16. Implementation Roadmap

### Phase 1 — Stabilise Engine

Goal: make ATLAS reliable locally.

Tasks:

1. Confirm all documented CLI commands work.
2. Confirm migrations 0001–0008 run cleanly.
3. Confirm synthetic data pipeline works.
4. Confirm `/v1/scan` returns ranked outputs.
5. Confirm dashboard reads watchlist and signal data.
6. Confirm Telegram test alert works.
7. Confirm DeepSeek fallback works when no API key exists.

Success criteria:

```text
Manual symbols → scan → signal → dashboard → Telegram alert
```

### Phase 2 — Screener MVP

Goal: make ATLAS useful for finding trade candidates.

Tasks:

1. Add screener universe config.
2. Add manual symbol input in dashboard.
3. Add `/v1/screener/run`.
4. Add provider status and data freshness checks.
5. Add ranked signal table.
6. Add no-signal reasons.
7. Add top 10 DeepSeek summaries.

Success criteria:

```text
User can scan 10–500 symbols and get ranked trade plans.
```

### Phase 3 — Signal Quality

Goal: reduce false positives.

Tasks:

1. Strengthen technical score.
2. Add EMA/SMA/Supertrend/Donchian/Keltner/relative volume if missing.
3. Improve multi-timeframe confirmation.
4. Improve risk/reward logic.
5. Improve ATR and structure-based stop loss.
6. Add stale-data veto.
7. Add news event veto.
8. Add calibration report.

Success criteria:

```text
Signals are fewer, cleaner, and easier to trust.
```

### Phase 4 — DeepSeek Optimisation

Goal: make explanations useful without making the LLM responsible for calculations.

Tasks:

1. Implement strict JSON input/output contract.
2. Cache explanations.
3. Use `deepseek-chat` for normal summaries.
4. Use `deepseek-reasoner` for high-conviction reviews.
5. Add hallucination guardrails.
6. Add templated fallback.
7. Add explanation quality tests.

Success criteria:

```text
DeepSeek gives clear explanations and never invents trade levels.
```

### Phase 5 — Real Data Live Mode

Goal: move from synthetic/local to low-cost real data.

Tasks:

1. Add Alpaca free IEX data path.
2. Keep Polygon optional.
3. Add RSS news path.
4. Add FRED macro refresh.
5. Add provider fail-soft behaviour.
6. Add clear dashboard warnings for missing feeds.
7. Add Telegram live alerts.

Success criteria:

```text
ATLAS works with free/low-cost data and degrades gracefully when keys are missing.
```

### Phase 6 — Product Polish

Goal: make it demo-ready.

Tasks:

1. Improve dashboard layout.
2. Add signal detail page.
3. Add export to CSV.
4. Add Telegram alert settings.
5. Add portfolio risk summary.
6. Add journal attribution.
7. Add disclaimer banners.
8. Add user-friendly setup docs.

Success criteria:

```text
A user can understand, run, and trust the system without reading the code.
```

---

## 17. Environment Variables

Fail-soft environment design.

```env
# Infra
POSTGRES_DSN=
REDIS_URL=

# Market data
POLYGON_API_KEY=
ALPACA_API_KEY=
ALPACA_API_SECRET=
ALPACA_FEED=iex

# Macro
FRED_API_KEY=

# News
NEWSAPI_KEY=

# LLM
DEEPSEEK_API_KEY=
ATLAS_EXPLAIN_MODEL=deepseek-chat

# Quant model
ATLAS_TREND_MODEL=ml/registry/trend/v1.joblib

# Alerts
TELEGRAM_BOT_TOKEN=
TELEGRAM_CHAT_ID=
ATLAS_WEBHOOK_URL=
ATLAS_WEBHOOK_SECRET=

# Runtime
ENV=dev
LOG_LEVEL=INFO
```

Rules:

1. Missing optional keys must not crash the system.
2. Missing market data should fall back or show a clear warning.
3. Missing DeepSeek should use templated explanations.
4. Missing Telegram should keep log alerts active.

---

## 18. Folder Structure

```text
atlas/
├── apps/
│   ├── ingest-equities/
│   ├── feature-engine/
│   ├── quant-engine/
│   ├── scoring-engine/
│   ├── risk-engine/
│   ├── explanation-engine/
│   ├── macro-engine/
│   ├── sentiment-engine/
│   ├── options-analytics/
│   ├── news-ingest/
│   ├── signal-service/
│   ├── alert-service/
│   ├── execution-service/
│   ├── portfolio-service/
│   ├── journal-service/
│   ├── backtest-service/
│   └── web/
├── packages/
│   └── shared-py/
├── infra/
│   ├── docker/
│   └── observability/
├── ml/
│   └── registry/
├── docs/
│   ├── architecture/
│   ├── runbooks/
│   └── adr/
└── tests/
```

Do not introduce new top-level architecture unless there is a real bottleneck.

---

## 19. What to Remove from the Old Blueprint for Now

Remove or defer these from the active blueprint:

| Item | Decision |
|---|---|
| Mobile app | Defer |
| Desktop app | Defer |
| Kafka / Redpanda | Defer |
| ClickHouse | Defer |
| Qdrant / vector DB | Defer |
| Neo4j graph DB | Defer |
| Full LangGraph multi-agent system | Defer |
| On-chain engine | Defer |
| Futures / forex | Defer |
| RL execution agent | Defer |
| Kubernetes / Helm / ArgoCD | Defer |
| Multi-region active-active | Defer |
| Marketplace | Defer |
| Enterprise SSO | Defer |
| Billing system | Defer |
| Public API SDKs | Defer |
| Hosted notebooks | Defer |
| Options flow paid providers | Defer |
| Fundamentals engine | Later, after signal MVP |

---

## 20. Quality Gates

Before calling ATLAS “working”, every run must pass these checks:

### 20.1 Data Quality

- Latest bar is fresh.
- No missing OHLCV fields.
- Enough historical bars for indicators.
- No impossible prices.
- No duplicate bars.
- Provider status is visible.

### 20.2 Indicator Quality

- Indicators are not NaN.
- ATR is positive.
- Volume-based indicators have volume.
- Multi-timeframe indicators agree with selected horizon.
- Debug endpoint shows raw indicator values.

### 20.3 Signal Quality

- Composite score is reproducible.
- Sub-score breakdown is available.
- Risk veto reason is clear.
- Entry, SL, T1/T2/T3 are mathematically consistent.
- Risk/reward is at least 1.5 unless manually overridden.
- No signal is returned when gates fail.

### 20.4 LLM Quality

- DeepSeek output follows schema.
- No invented prices.
- No invented news.
- Includes bull case and bear case.
- Includes invalidation.
- Includes disclaimer.

### 20.5 Alert Quality

- Telegram sends a test message.
- Cooldown works.
- Duplicate alerts are suppressed.
- Delivery failures are logged.

---

## 21. Final Build Definition

ATLAS v2 is successful when it can:

1. Accept manual stock symbols.
2. Pull low-cost / free market data.
3. Calculate a complete technical indicator stack.
4. Run quant trend scoring.
5. Attach news, macro, sentiment, liquidity, options context.
6. Produce a composite score.
7. Rank the best stocks to trade.
8. Generate entry, stop loss, T1, T2, T3.
9. Apply risk vetoes and position sizing.
10. Explain the signal using DeepSeek.
11. Show everything in the dashboard.
12. Send Telegram alerts.
13. Run even when optional API keys are missing.
14. Be understandable to a normal user.

---

## 22. Non-Negotiables

1. **No LLM-only trading signals.**
2. **No expensive provider dependency for MVP.**
3. **No hidden black-box score.**
4. **No signal without risk plan.**
5. **No alert without invalidation.**
6. **No auto-execution by default.**
7. **No enterprise architecture until there is real usage.**
8. **No fake confidence.**
9. **No stale data pretending to be live.**
10. **No promise of profit.**

---

## 23. User-Facing Disclaimer

Use this in dashboard, Telegram, and reports:

> ATLAS outputs are for informational and educational purposes only. They are not financial advice, investment advice, or a guarantee of performance. Trading involves risk, and you can lose money. Always verify signals and manage risk independently.

---

## 24. Immediate Next Engineering Checklist

Do these next:

1. Add `POST /v1/screener/run` if missing.
2. Add dashboard manual symbol input.
3. Add screener universe config file.
4. Add technical indicator visibility in signal debug.
5. Add score breakdown table.
6. Add trade plan object with `entry`, `stop_loss`, `t1`, `t2`, `t3`.
7. Add DeepSeek JSON contract.
8. Add Telegram signal template.
9. Add provider health endpoint.
10. Add stale-data veto.
11. Add no-signal reason output.
12. Add integration test: manual symbols → ranked signal → explanation → alert.

---

## 25. Closing Direction

> “ATLAS scans stocks, ranks the cleanest opportunities, gives a risk-managed trade plan, explains why, and alerts me when it matters.”

Everything else is secondary.
