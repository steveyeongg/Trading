"""The user's validated account settings for the swing product.

One model backs the settings page, the API and the nightly scan. :meth:`SwingSettings.risk` and
:meth:`SwingSettings.universe` turn it into the engine's own parameter objects, so the scan never
reads these fields directly. Only the two universe rules a user can reasonably tune (the price
floor and the dollar-volume floor) are exposed; every other universe rule stays at its code
default.

The setups were validated on the default universe. A user who changes either universe field is
scanning a market the validation did not cover, which :meth:`differs_from_default_universe`
reports so the product can say so.
"""

from __future__ import annotations

import dataclasses

from pydantic import BaseModel, ConfigDict, Field

from swing_engine.config import RiskSettings, UniverseFilter


class SwingSettings(BaseModel):
    """Account size, risk limits and the two universe floors; every field is bounds-checked.

    Defaults are the engine defaults the validation ran with (:class:`RiskSettings` and
    :class:`UniverseFilter`). Infinity and NaN are rejected, and an assignment is validated too.
    """

    model_config = ConfigDict(validate_assignment=True)

    equity: float = Field(default=25_000.0, gt=0, allow_inf_nan=False)
    risk_pct: float = Field(default=0.01, gt=0, le=0.05, allow_inf_nan=False)
    max_position_pct: float = Field(default=0.20, gt=0, le=1, allow_inf_nan=False)
    max_positions: int = Field(default=8, ge=1, le=50)
    max_total_risk_pct: float = Field(default=0.06, gt=0, le=0.5, allow_inf_nan=False)
    min_price: float = Field(default=10.0, ge=1, allow_inf_nan=False)
    min_dollar_volume: float = Field(default=20_000_000.0, ge=1_000_000, allow_inf_nan=False)
    telegram_digest: bool = True

    def risk(self) -> RiskSettings:
        """The sizing and exposure limits (the overlap parameters stay at their defaults)."""
        return RiskSettings(
            equity=self.equity,
            risk_pct=self.risk_pct,
            max_position_pct=self.max_position_pct,
            max_positions=self.max_positions,
            max_total_risk_pct=self.max_total_risk_pct,
        )

    def universe(self) -> UniverseFilter:
        """The default universe with this user's price and dollar-volume floors."""
        return dataclasses.replace(
            UniverseFilter(),
            min_price=self.min_price,
            min_median_dollar_volume=self.min_dollar_volume,
        )

    def differs_from_default_universe(self) -> bool:
        """True when either universe floor is not the one the setups were validated on."""
        default = UniverseFilter()
        return (
            self.min_price != default.min_price
            or self.min_dollar_volume != default.min_median_dollar_volume
        )
