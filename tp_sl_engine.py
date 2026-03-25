"""
TP/SL Engine
------------
Checks all open positions against take profit and stop loss thresholds.
Returns a list of positions that should be closed and the reason why.

Global thresholds from SimConfig:
  take_profit_pct — close when pnl_pct >= this value
  stop_loss_pct   — close when pnl_pct <= negative this value
"""

import logging

log = logging.getLogger(__name__)


class TpSlEngine:

    def check(
        self,
        positions: list[dict],
        take_profit_pct: float,
        stop_loss_pct: float,
    ) -> list[tuple[str, str]]:
        """
        Check all open positions against TP/SL thresholds.
        Positions less than 60 seconds old are exempt — prevents
        immediate close due to price feed discrepancy on first cycle.
        """
        from datetime import datetime, timezone
        now    = datetime.now(timezone.utc)
        to_close = []

        for pos in positions:
            pnl_pct = pos.get("pnl_pct", 0.0)
            pos_id  = pos["id"]
            symbol  = pos.get("token_symbol", "?")

            # Skip positions that just opened — give at least 60 seconds
            try:
                opened = datetime.fromisoformat(pos.get("opened_at", ""))
                if (now - opened).total_seconds() < 60:
                    continue
            except Exception:
                pass

            if pnl_pct >= take_profit_pct:
                log.info(
                    f"  🎯 Take profit hit: {symbol} | "
                    f"{pnl_pct:+.1f}% >= {take_profit_pct}% TP | "
                    f"ID: {pos_id}"
                )
                to_close.append((pos_id, "take_profit"))

            elif pnl_pct <= -stop_loss_pct:
                log.info(
                    f"  🛑 Stop loss hit: {symbol} | "
                    f"{pnl_pct:+.1f}% <= -{stop_loss_pct}% SL | "
                    f"ID: {pos_id}"
                )
                to_close.append((pos_id, "stop_loss"))

        return to_close
