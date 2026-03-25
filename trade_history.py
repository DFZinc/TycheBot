"""
Trade History
-------------
Persistent log of all closed positions.
Written once when a position closes, never modified.
"""

import json
import os
import logging
from datetime import datetime, timezone

log = logging.getLogger(__name__)

HISTORY_FILE = "sim_trades.json"


class TradeHistory:
    def __init__(self, filepath: str = HISTORY_FILE):
        self.filepath = filepath
        self._trades: list = self._load()

    def _load(self) -> list:
        if os.path.exists(self.filepath):
            try:
                with open(self.filepath, "r") as f:
                    return json.load(f)
            except Exception as e:
                log.warning(f"Trade history load error: {e}")
        return []

    def _save(self):
        try:
            with open(self.filepath, "w") as f:
                json.dump(self._trades, f, indent=2)
        except Exception as e:
            log.error(f"Trade history save error: {e}")

    def record(self, position: dict):
        """Record a closed position to trade history."""
        self._trades.append({
            "id":               position["id"],
            "wallet_address":   position["wallet_address"],
            "wallet_label":     position["wallet_label"],
            "token_address":    position["token_address"],
            "token_symbol":     position["token_symbol"],
            "entry_price_usd":  position["entry_price_usd"],
            "exit_price_usd":   position["current_price_usd"],
            "trade_size_usd":   position["trade_size_usd"],
            "realized_pnl_usd": position["realized_pnl_usd"],
            "pnl_pct":          position["pnl_pct"],
            "close_reason":     position["close_reason"],
            "opened_at":        position["opened_at"],
            "closed_at":        position["closed_at"],
        })
        self._save()

    def get_all(self) -> list[dict]:
        return list(reversed(self._trades))

    def get_recent(self, limit: int = 50) -> list[dict]:
        return list(reversed(self._trades))[:limit]
