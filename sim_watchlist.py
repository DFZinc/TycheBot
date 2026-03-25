"""
Sim Watchlist
-------------
Manually managed list of wallets to copy-trade.
Hard cap of 5 wallets enforced on add.
"""

import json
import os
import logging
from datetime import datetime, timezone

log = logging.getLogger(__name__)

WATCHLIST_FILE = "sim_watchlist.json"
MAX_WALLETS    = 5


class SimWatchlist:
    def __init__(self, filepath: str = WATCHLIST_FILE):
        self.filepath = filepath
        self._data: dict = self._load()

    def _load(self) -> dict:
        if os.path.exists(self.filepath):
            try:
                with open(self.filepath, "r") as f:
                    return json.load(f)
            except Exception as e:
                log.warning(f"Sim watchlist load error: {e}")
        return {}

    def _save(self):
        try:
            with open(self.filepath, "w") as f:
                json.dump(self._data, f, indent=2)
        except Exception as e:
            log.error(f"Sim watchlist save error: {e}")

    def add(self, address: str, label: str = "") -> tuple[bool, str]:
        addr = address.lower().strip()
        if not addr.startswith("0x") or len(addr) != 42:
            return False, "Invalid address"
        if addr in self._data:
            return False, "Already in watchlist"
        if len(self._data) >= MAX_WALLETS:
            return False, f"Maximum {MAX_WALLETS} wallets allowed"
        self._data[addr] = {
            "address":    addr,
            "label":      label or addr[:10] + "...",
            "added_at":   datetime.now(timezone.utc).isoformat(),
            "last_seen_hash": "",  # Last tx hash processed
        }
        self._save()
        log.info(f"Sim watchlist: added {addr}")
        return True, "Added"

    def remove(self, address: str) -> bool:
        addr = address.lower()
        if addr in self._data:
            del self._data[addr]
            self._save()
            log.info(f"Sim watchlist: removed {addr}")
            return True
        return False

    def update_last_seen(self, address: str, tx_hash: str):
        addr = address.lower()
        if addr in self._data:
            self._data[addr]["last_seen_hash"] = tx_hash
            self._save()

    def get_all(self) -> list[dict]:
        return list(self._data.values())

    def count(self) -> int:
        return len(self._data)

    def contains(self, address: str) -> bool:
        return address.lower() in self._data
