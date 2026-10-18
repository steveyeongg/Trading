"""Trade-level backtest of the swing setups: trade list, statistics, baseline and portfolio.

``trades`` turns the point-in-time candidates into trades with the one fill and exit model,
``stats`` summarises closed trades, ``baseline`` is the random-entry benchmark a setup has
to beat before it is trusted, and ``portfolio`` plays the trades through one capacity-limited
account (and the SPY buy-and-hold it is compared with).
"""
