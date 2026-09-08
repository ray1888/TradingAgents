"""QuantLab research-task service: frozen-bundle analysis and structured reports."""

from tradingagents.research.protocol import (
    MAX_CANDIDATES,
    PROTOCOL_VERSION,
    SCHEMA_VERSION,
)

__all__ = [
    "MAX_CANDIDATES",
    "PROTOCOL_VERSION",
    "SCHEMA_VERSION",
    "create_app",
]


def __getattr__(name: str):
    if name == "create_app":
        from tradingagents.research.service import create_app

        return create_app
    raise AttributeError(name)

