"""Configuration loading/validation."""

import json
import os
import sys

_DEFAULTS = {
    "rpc_url": "https://rpc.mainnet.chain.robinhood.com",
    "chain_id": 4663,
    "explorer_base": "https://robinhoodchain.blockscout.com",
    "wrapped_native": "",
    "native_symbol": "ETH",
    "native_usd_price": 0,
    "coingecko_native_id": "ethereum",
    "quote_tokens": {},
    "v2_factories": [],
    "v3_factories": [],
    "v2_router": "",
    "known_lockers": [],
    "poll_interval_sec": 6,
    "confirmations": 2,
    "logs_chunk_size": 4000,
    "rpc_delay_sec": 0.05,
    "watch_checkpoints_min": [2, 5, 15, 30, 60],
    "watch_liquidity_drop_alert_pct": 70,
    "probe_quote_fraction": 0.003,
    "blacklist_is_hard_flag": True,
    "min_initial_liquidity_usd": 2000,
    "alert_min_score": 65,
    "webhook_url": "",
    "data_dir": "data",
    "profile_file": "",
}


class Config:
    def __init__(self, raw, path="config.json"):
        self.path = path
        self._d = dict(_DEFAULTS)
        for k, v in raw.items():
            if not k.startswith("_"):
                self._d[k] = v

        from web3 import Web3

        def cs(a):
            return Web3.to_checksum_address(a) if a else ""

        self._d["wrapped_native"] = cs(self._d["wrapped_native"])
        self._d["v2_router"] = cs(self._d["v2_router"])
        self._d["v2_factories"] = [cs(a) for a in self._d["v2_factories"]]
        self._d["v3_factories"] = [cs(a) for a in self._d["v3_factories"]]
        self._d["known_lockers"] = [cs(a) for a in self._d["known_lockers"]]
        self._d["quote_tokens"] = {
            cs(a): v for a, v in self._d["quote_tokens"].items() if not a.startswith("_")
        }

    def __getattr__(self, name):
        try:
            return self._d[name]
        except KeyError:
            raise AttributeError(name)

    def quote_map(self):
        """All recognised quote tokens: {address: {"symbol", "usd"}}. Wrapped native included."""
        q = dict(self.quote_tokens)
        if self.wrapped_native:
            q.setdefault(
                self.wrapped_native,
                {"symbol": "W" + self.native_symbol, "usd": float(self.native_usd_price or 0)},
            )
            q[self.wrapped_native]["usd"] = float(self.native_usd_price or 0)
        return q

    def data_path(self, name):
        os.makedirs(self.data_dir, exist_ok=True)
        return os.path.join(self.data_dir, name)


def load_config(path="config.json", require=()):
    if not os.path.exists(path):
        sys.exit(
            f"Config file '{path}' not found. Copy config.example.json to config.json first."
        )
    with open(path) as f:
        raw = json.load(f)
    cfg = Config(raw, path)
    missing = [k for k in require if not cfg._d.get(k)]
    if missing:
        sys.exit(
            f"Config '{path}' is missing required value(s): {', '.join(missing)}.\n"
            "Run `python monitor.py --discover` to find factory/wrapped-native addresses, "
            "or check https://robinhoodchain.blockscout.com and the Uniswap deployments page."
        )
    return cfg
