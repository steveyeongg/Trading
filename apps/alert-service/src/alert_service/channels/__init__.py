"""Alert delivery channels."""

from alert_service.channels.base import Channel, DeliveryResult
from alert_service.channels.email import EmailChannel
from alert_service.channels.log import LogChannel
from alert_service.channels.telegram import TelegramChannel
from alert_service.channels.webhook import WebhookChannel


def build_channels() -> dict[str, Channel]:
    """Instantiate the channel registry. Channels are fail-soft: ones lacking
    credentials report unavailable and the engine skips them."""
    return {
        "log": LogChannel(),
        "webhook": WebhookChannel(),
        "telegram": TelegramChannel(),
        "email": EmailChannel(),
    }


__all__ = [
    "Channel",
    "DeliveryResult",
    "EmailChannel",
    "LogChannel",
    "TelegramChannel",
    "WebhookChannel",
    "build_channels",
]
