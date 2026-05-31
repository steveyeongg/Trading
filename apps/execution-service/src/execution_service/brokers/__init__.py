"""Brokers."""

from execution_service.brokers.alpaca import AlpacaBroker
from execution_service.brokers.base import Broker, Fill, OrderRequest
from execution_service.brokers.paper import PaperBroker


def build_broker() -> Broker:
    """Pick the broker. Alpaca when credentials exist, else the in-process
    paper broker (always available, fills at the requested/last price)."""
    alpaca = AlpacaBroker()
    return alpaca if alpaca.available else PaperBroker()


__all__ = ["AlpacaBroker", "Broker", "Fill", "OrderRequest", "PaperBroker", "build_broker"]
