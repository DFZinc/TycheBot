"""
Position Tracker
----------------
Manages open and closed simulated positions in ETH denomination.
"""

import json
import os
import logging
import uuid
from datetime import datetime, timezone

log = logging.getLogger(__name__)

POSITIONS_FILE = "sim_positions.json"


class PositionTracker:
    def __init__(self, filepath: str = POSITIONS_FILE):
        self.filepath = filepath
        self._positions: dict = self._load()

    def _load(self) -> dict:
        if os.path.exists(self.filepath):
            try:
                with open(self.filepath, "r") as f:
                    return json.load(f)
            except Exception as e:
                log.warning(f"Positions load error: {e}")
        return {}

    def _save(self):
        try:
            with open(self.filepath, "w") as f:
                json.dump(self._positions, f, indent=2)
        except Exception as e:
            log.error(f"Positions save error: {e}")

    def open_position(
        self,
        wallet_address: str,
        wallet_label: str,
        wallet_color: str,
        token_address: str,
        token_symbol: str,
        entry_price_usd: float,
        trade_size_eth: float,
        eth_price_usd: float,
        gas_eth: float = 0.0,
        pool_address: str = "",
    ) -> str | None:
        # Prevent duplicate positions for same wallet + token
        for pos in self._positions.values():
            if (pos["wallet_address"] == wallet_address.lower()
                    and pos["token_address"] == token_address.lower()
                    and pos["status"] == "open"):
                return None

        if entry_price_usd <= 0:
            return None
        if eth_price_usd <= 0:
            eth_price_usd = 2000.0  # fallback — corrected on next price fetch

        pos_id        = str(uuid.uuid4())[:8]
        trade_size_usd = round(trade_size_eth * eth_price_usd, 2)
        quantity      = trade_size_usd / entry_price_usd

        self._positions[pos_id] = {
            "id":                pos_id,
            "status":            "open",
            "wallet_address":    wallet_address.lower(),
            "wallet_label":      wallet_label,
            "wallet_color":      wallet_color,
            "token_address":     token_address.lower(),
            "token_symbol":      token_symbol,
            "pool_address":      pool_address,
            "entry_price_usd":   round(entry_price_usd, 18),
            "current_price_usd": round(entry_price_usd, 18),
            "eth_price_at_entry": round(eth_price_usd, 2),
            "trade_size_eth":    round(trade_size_eth, 8),
            "trade_size_usd":    trade_size_usd,
            "gas_eth":           round(gas_eth, 8),
            "token_quantity":    quantity,
            "unrealized_pnl_usd": 0.0,
            "unrealized_pnl_eth": 0.0,
            "pnl_pct":           0.0,
            "opened_at":         datetime.now(timezone.utc).isoformat(),
            "closed_at":         None,
            "close_reason":      None,
            "realized_pnl_usd":  None,
            "realized_pnl_eth":  None,
        }
        self._save()
        log.info(
            f"  📈 Position opened: {token_symbol} | "
            f"Entry: ${entry_price_usd:.6f} | "
            f"Size: {trade_size_eth:.4f} ETH (${trade_size_usd:.2f}) | "
            f"Gas: {gas_eth:.6f} ETH | "
            f"Copying: {wallet_label} | ID: {pos_id}"
        )
        return pos_id

    def update_price(self, pos_id: str, current_price_usd: float, eth_price_usd: float):
        pos = self._positions.get(pos_id)
        if not pos or pos["status"] != "open":
            return
        pos["current_price_usd"]   = round(current_price_usd, 18)
        current_value_usd          = pos["token_quantity"] * current_price_usd
        pnl_usd                    = current_value_usd - pos["trade_size_usd"]
        pos["unrealized_pnl_usd"]  = round(pnl_usd, 4)
        pos["unrealized_pnl_eth"]  = round(pnl_usd / eth_price_usd, 8) if eth_price_usd > 0 else 0.0
        entry = pos["entry_price_usd"]
        if entry > 0:
            pos["pnl_pct"] = round((current_price_usd - entry) / entry * 100, 2)
        self._save()

    def close_position(self, pos_id: str, close_reason: str) -> tuple[float, float] | None:
        """Returns (realized_pnl_usd, realized_pnl_eth) or None."""
        pos = self._positions.get(pos_id)
        if not pos or pos["status"] != "open":
            return None
        pos["status"]            = "closed"
        pos["close_reason"]      = close_reason
        pos["realized_pnl_usd"]  = pos["unrealized_pnl_usd"]
        pos["realized_pnl_eth"]  = pos["unrealized_pnl_eth"]
        pos["closed_at"]         = datetime.now(timezone.utc).isoformat()
        self._save()
        log.info(
            f"  📉 Position closed: {pos['token_symbol']} | "
            f"Reason: {close_reason} | "
            f"P&L: {pos['realized_pnl_eth']:+.6f} ETH (${pos['realized_pnl_usd']:+.2f}) | "
            f"({pos['pnl_pct']:+.1f}%) | ID: {pos_id}"
        )
        return pos["realized_pnl_usd"], pos["realized_pnl_eth"]

    def get_open_positions(self) -> list[dict]:
        return [p for p in self._positions.values() if p["status"] == "open"]

    def get_closed_positions(self) -> list[dict]:
        closed = [p for p in self._positions.values() if p["status"] == "closed"]
        return sorted(closed, key=lambda x: x["closed_at"], reverse=True)

    def get_open_for_token_wallet(self, token_address: str, wallet_address: str) -> dict | None:
        for pos in self._positions.values():
            if (pos["status"] == "open"
                    and pos["token_address"] == token_address.lower()
                    and pos["wallet_address"] == wallet_address.lower()):
                return pos
        return None

    def total_unrealized_pnl_eth(self) -> float:
        return round(sum(p["unrealized_pnl_eth"] for p in self.get_open_positions()), 8)

    def total_unrealized_pnl_usd(self) -> float:
        return round(sum(p["unrealized_pnl_usd"] for p in self.get_open_positions()), 2)
