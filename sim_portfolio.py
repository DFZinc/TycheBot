"""
Sim Portfolio
-------------
Tracks the simulated trader's financial state in ETH with USD equivalents.

  starting_balance_eth  — set once on first run
  current_balance_eth   — starting - allocated - closed losses + closed gains
  allocated_eth         — capital currently locked in open positions
  available_eth         — current_balance_eth - allocated_eth
  realized_pnl_eth      — total profit/loss from closed trades
  unrealized_pnl_eth    — current open position P&L
  gas_spent_eth         — total gas costs paid across all trades
"""

import json
import os
import logging
from datetime import datetime, timezone

log = logging.getLogger(__name__)

PORTFOLIO_FILE = "sim_portfolio.json"


class SimPortfolio:
    def __init__(self, starting_balance_eth: float, filepath: str = PORTFOLIO_FILE):
        self.filepath = filepath
        self._data = self._load(starting_balance_eth)

    def _load(self, starting_balance_eth: float) -> dict:
        if os.path.exists(self.filepath):
            try:
                with open(self.filepath, "r") as f:
                    data = json.load(f)
                # Validate it has the correct ETH-based keys
                if "current_balance_eth" not in data:
                    log.warning("Portfolio file is old USD format — resetting to ETH format")
                    os.remove(self.filepath)
                else:
                    # Ensure all required keys exist (fill missing with defaults)
                    defaults = {
                        "allocated_eth": 0.0, "gas_spent_eth": 0.0,
                        "eth_price_usd": 0.0, "unrealized_pnl_eth": 0.0,
                    }
                    for k, v in defaults.items():
                        if k not in data:
                            data[k] = v
                    return data
            except Exception as e:
                log.warning(f"Portfolio load error: {e}")
        data = {
            "starting_balance_eth": starting_balance_eth,
            "current_balance_eth":  starting_balance_eth,
            "allocated_eth":        0.0,
            "realized_pnl_eth":     0.0,
            "unrealized_pnl_eth":   0.0,
            "gas_spent_eth":        0.0,
            "total_trades":         0,
            "winning_trades":       0,
            "best_trade_eth":       0.0,
            "worst_trade_eth":      0.0,
            "peak_balance_eth":     starting_balance_eth,
            "max_drawdown_pct":     0.0,
            "eth_price_usd":        0.0,  # updated each cycle
            "created_at":           datetime.now(timezone.utc).isoformat(),
            "updated_at":           datetime.now(timezone.utc).isoformat(),
        }
        self._save(data)
        return data

    def _save(self, data: dict = None):
        if data is None:
            data = self._data
        data["updated_at"] = datetime.now(timezone.utc).isoformat()
        try:
            with open(self.filepath, "w") as f:
                json.dump(data, f, indent=2)
        except Exception as e:
            log.error(f"Portfolio save error: {e}")

    def update_eth_price(self, eth_price_usd: float):
        """Update current ETH/USD price for USD display calculations."""
        self._data["eth_price_usd"] = round(eth_price_usd, 2)
        self._save()

    def allocate(self, amount_eth: float, gas_eth: float = 0.0):
        """
        Deduct trade size + gas from available balance when opening a position.
        Returns True if sufficient balance, False if not.
        """
        available = self.available_eth
        total_cost = amount_eth + gas_eth
        if available < total_cost:
            return False
        self._data["allocated_eth"]   = round(self._data["allocated_eth"] + amount_eth, 8)
        self._data["gas_spent_eth"]   = round(self._data["gas_spent_eth"] + gas_eth, 8)
        self._data["current_balance_eth"] = round(
            self._data["current_balance_eth"] - gas_eth, 8
        )
        self._save()
        return True

    def deallocate(self, amount_eth: float):
        """Release allocated capital when a position closes."""
        self._data["allocated_eth"] = round(
            max(0.0, self._data["allocated_eth"] - amount_eth), 8
        )
        self._save()

    def update_unrealized(self, unrealized_pnl_eth: float):
        self._data["unrealized_pnl_eth"] = round(unrealized_pnl_eth, 8)
        self._save()

    def record_closed_trade(self, trade_size_eth: float, pnl_eth: float, gas_eth: float = 0.0):
        """
        Called when a position closes.
        Releases allocated capital, adds P&L to balance, records stats.
        """
        net_pnl = round(pnl_eth - gas_eth, 8)

        self.deallocate(trade_size_eth)
        self._data["realized_pnl_eth"]    = round(self._data["realized_pnl_eth"] + net_pnl, 8)
        self._data["current_balance_eth"] = round(
            self._data["starting_balance_eth"] + self._data["realized_pnl_eth"], 8
        )
        self._data["gas_spent_eth"] = round(self._data["gas_spent_eth"] + gas_eth, 8)
        self._data["total_trades"]  += 1
        if net_pnl > 0:
            self._data["winning_trades"] += 1
        if net_pnl > self._data["best_trade_eth"]:
            self._data["best_trade_eth"] = round(net_pnl, 8)
        if net_pnl < self._data["worst_trade_eth"]:
            self._data["worst_trade_eth"] = round(net_pnl, 8)

        peak = self._data["peak_balance_eth"]
        bal  = self._data["current_balance_eth"]
        if bal > peak:
            self._data["peak_balance_eth"] = round(bal, 8)
        if peak > 0:
            drawdown = (peak - bal) / peak * 100
            if drawdown > self._data["max_drawdown_pct"]:
                self._data["max_drawdown_pct"] = round(drawdown, 2)

        self._save()
        eth_price = self._data.get("eth_price_usd", 0)
        usd = net_pnl * eth_price if eth_price else 0
        log.info(
            f"  💰 Trade closed: P&L {net_pnl:+.6f} ETH"
            + (f" (${usd:+.2f})" if usd else "")
            + f" | Balance: {self._data['current_balance_eth']:.6f} ETH"
        )

    def reset(self, starting_balance_eth: float):
        if os.path.exists(self.filepath):
            os.remove(self.filepath)
        self._data = self._load(starting_balance_eth)
        log.info(f"Portfolio reset — starting balance: {starting_balance_eth} ETH")

    @property
    def available_eth(self) -> float:
        return round(self._data["current_balance_eth"] - self._data["allocated_eth"], 8)

    @property
    def win_rate(self) -> float:
        t = self._data["total_trades"]
        return round(self._data["winning_trades"] / t * 100, 1) if t > 0 else 0.0

    @property
    def total_pnl_eth(self) -> float:
        return round(
            self._data["realized_pnl_eth"] + self._data["unrealized_pnl_eth"], 8
        )

    def snapshot(self) -> dict:
        eth_price = self._data.get("eth_price_usd", 0)
        d = dict(self._data)
        d["available_eth"]      = self.available_eth
        d["win_rate_pct"]       = self.win_rate
        d["total_pnl_eth"]      = self.total_pnl_eth
        d["total_pnl_usd"]      = round(self.total_pnl_eth * eth_price, 2) if eth_price else 0
        d["current_balance_usd"] = round(d["current_balance_eth"] * eth_price, 2) if eth_price else 0
        d["available_usd"]      = round(self.available_eth * eth_price, 2) if eth_price else 0
        return d
