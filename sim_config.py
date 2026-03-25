"""
Sim Config
----------
User-configurable settings for the simulation agent.
Persisted to sim_config.json so changes survive restarts.

Balance is denominated in ETH. USD equivalents are calculated
at current ETH price each cycle.
"""

import json
import os
import logging

log = logging.getLogger(__name__)

CONFIG_FILE = "sim_config.json"

DEFAULTS = {
    "starting_balance_eth":  1.0,
    "trade_size_eth":        0.05,
    "take_profit_pct":       50.0,
    "stop_loss_pct":         20.0,
    "max_wallets":           5,
    "poll_interval_seconds": 120,
}


class SimConfig:
    def __init__(self, filepath: str = CONFIG_FILE):
        self.filepath = filepath
        self._data = self._load()

    def _load(self) -> dict:
        if os.path.exists(self.filepath):
            try:
                with open(self.filepath, "r") as f:
                    data = json.load(f)
                    for k, v in DEFAULTS.items():
                        if k not in data:
                            data[k] = v
                    return data
            except Exception as e:
                log.warning(f"Config load error: {e}")
        return dict(DEFAULTS)

    def _save(self):
        try:
            with open(self.filepath, "w") as f:
                json.dump(self._data, f, indent=2)
        except Exception as e:
            log.error(f"Config save error: {e}")

    def get(self, key: str):
        return self._data.get(key, DEFAULTS.get(key))

    def update(self, updates: dict):
        for k, v in updates.items():
            if k in DEFAULTS:
                if k == "max_wallets":
                    v = min(int(v), 5)
                self._data[k] = v
        self._save()
        log.info(f"Config updated: {updates}")

    def all(self) -> dict:
        return dict(self._data)
