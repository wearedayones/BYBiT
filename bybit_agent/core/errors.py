"""Custom exception types — ports src/core/errors.ts."""

from __future__ import annotations


class BybitApiError(Exception):
    def __init__(self, ret_code: int, ret_msg: str, endpoint: str) -> None:
        self.ret_code = ret_code
        self.ret_msg = ret_msg
        self.endpoint = endpoint
        super().__init__(f"Bybit API error {ret_code} on {endpoint}: {ret_msg}")


class BotApiError(Exception):
    def __init__(self, status_code: int, debug_msg: str, endpoint: str) -> None:
        self.status_code = status_code
        self.debug_msg = debug_msg
        self.endpoint = endpoint
        super().__init__(f"Bot API error {status_code} on {endpoint}: {debug_msg}")


class RiskVetoError(Exception):
    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(f"Risk veto: {reason}")


class KillSwitchError(Exception):
    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(f"Kill switch engaged: {reason}")


class ConfigError(Exception):
    pass
