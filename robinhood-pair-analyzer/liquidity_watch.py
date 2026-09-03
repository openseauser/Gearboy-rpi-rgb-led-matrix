#!/usr/bin/env python3
"""Watch for LARGE liquidity adds into young pairs.

    python liquidity_watch.py                 # live: alert on big adds to pairs < --max-age-hours old
    python liquidity_watch.py --days 7        # history: table of big early adds + how each turned out

Every v2 liquidity add emits Mint(sender, amount0, amount1) on the pair. This
script scans that topic chain-wide (no pair list needed), keeps only events
from pairs belonging to the configured factories, sizes the add in quote/USD
terms, and filters by how old the pair was when the money landed.

Reality check baked in: a token "starting at a few million" is a few million
FDV, which needs only pennies of liquidity if most supply sits outside the
pool. The signal worth watching is the actual quote amount committed — that's
what gets measured here. Reports include both so the distinction stays visible.

Runs fine alongside monitor.py (separate state + output files).
"""

import argparse
import json
import sys
import time
from datetime import datetime, timezone

from web3 import Web3

from monitor import fmt_usd, now_str, webhook
from rhscan.chain import (
    DEAD,
    TOPIC_MINT_V2,
    TOPIC_PAIR_CREATED,
    TOPIC_TRANSFER,
    ZERO,
    RpcError,
    _hb,
    call_fn,
    creation_block_binary_search,
    decode_pair_created,
    erc20_info,
    get_w3,
    rpc,
    safe_get_logs,
    to_jsonable,
    topic_addr,
)
from rhscan.config import load_config
from rhscan.features import (
    explorer_contract_creator,
    native_usd,
    quote_usd_price,
    snapshot_pair,
)

from eth_abi import decode as abi_decode

DEAD_CS = Web3.to_checksum_address(DEAD)


def decode_mint(log):
    a0, a1 = abi_decode(["uint256", "uint256"], _hb(log["data"]))
    return {
        "pair": Web3.to_checksum_address(log["address"]),
        "sender": topic_addr(log["topics"][1]),
        "amount0": a0,
        "amount1": a1,
        "block": int(log["blockNumber"], 16) if isinstance(log["blockNumber"], str) else int(log["blockNumber"]),
        "tx": log["transactionHash"],
        "log_index": log.get("logIndex"),
    }


class PairResolver:
    """Chain-wide Mint scans hit every contract emitting that signature, so
    each address must be validated as a pair of one of OUR factories (v2 pairs
    expose factory()). Verdicts and token metadata are cached."""

    def __init__(self, w3, cfg):
        self.w3, self.cfg = w3, cfg
        self.cache = {}
        self.quotes = cfg.quote_map()

    def resolve(self, pair):
        if pair in self.cache:
            return self.cache[pair]
        meta = None
        try:
            fac = Web3.to_checksum_address(
                call_fn(self.w3, pair, "factory()", ret_types=["address"])
            )
            if fac in self.cfg.v2_factories:
                t0 = Web3.to_checksum_address(call_fn(self.w3, pair, "token0()", ret_types=["address"]))
                t1 = Web3.to_checksum_address(call_fn(self.w3, pair, "token1()", ret_types=["address"]))
                q0, q1 = t0 in self.quotes, t1 in self.quotes
                if q0 != q1:  # exactly one known quote side
                    quote, token = (t0, t1) if q0 else (t1, t0)
                    tinfo = erc20_info(self.w3, token)
                    try:
                        q_dec = int(call_fn(self.w3, quote, "decimals()", ret_types=["uint8"]))
                    except Exception:
                        q_dec = 18
                    meta = {
                        "pair": pair, "token0": t0, "token1": t1,
                        "token": token, "quote": quote,
                        "quote_is_token0": q0, "quote_decimals": q_dec,
                        "token_symbol": tinfo["symbol"],
                        "quote_symbol": self.quotes[quote].get("symbol", "?"),
                        "creation_block": None,
                    }
        except Exception:
            meta = None
        self.cache[pair] = meta
        return meta


def add_amounts(meta, mint):
    if meta["quote_is_token0"]:
        q_raw = mint["amount0"]
    else:
        q_raw = mint["amount1"]
    return q_raw / 10 ** meta["quote_decimals"]


def usd_value(cfg, meta, quote_amt):
    q_usd = quote_usd_price(cfg, meta["quote"])
    if meta["quote"] == cfg.wrapped_native and not q_usd:
        q_usd = native_usd(cfg) or None
    return round(quote_amt * q_usd, 2) if q_usd else None


def lp_recipient(w3, tx_hash, pair):
    """Who received the LP tokens (Transfer from 0x0 on the pair in this tx).
    Straight-to-dead = burned on add; a locker address = locked on add."""
    try:
        rcpt = rpc(w3, "eth_getTransactionReceipt", [tx_hash], retries=1)
        zero_topic = "0x" + "0" * 64
        for lg in rcpt.get("logs", []):
            if (
                Web3.to_checksum_address(lg["address"]) == pair
                and lg["topics"]
                and lg["topics"][0].lower() == TOPIC_TRANSFER.lower()
                and lg["topics"][1].lower() == zero_topic
            ):
                to = topic_addr(lg["topics"][2])
                if to != ZERO:  # skip the MINIMUM_LIQUIDITY burn to 0x0
                    return to
    except Exception:
        pass
    return None


def pair_age_hours(w3, cfg, meta, at_block, spb):
    """Age of the pair at `at_block`, hours. Explorer first, then binary
    search; None if neither works."""
    cb = meta.get("creation_block")
    if cb is None:
        _, ctx = explorer_contract_creator(cfg, meta["pair"])
        if ctx:
            try:
                cb = w3.eth.get_transaction(ctx)["blockNumber"]
            except Exception:
                cb = None
        if cb is None:
            cb = creation_block_binary_search(w3, meta["pair"])
        meta["creation_block"] = cb
    if cb is None:
        return None
    return max(0.0, (at_block - cb) * spb / 3600.0)


def seconds_per_block(w3, span=50_000):
    latest = w3.eth.block_number
    probe = max(1, latest - span)
    t1 = w3.eth.get_block(latest)["timestamp"]
    t0 = w3.eth.get_block(probe)["timestamp"]
    return max(0.05, (t1 - t0) / max(1, latest - probe)), latest


def describe(cfg, meta, mint, quote_amt, usd, age_h, recipient, w3):
    sym = f"{meta['token_symbol']}/{meta['quote_symbol']}"
    age_s = f"{age_h:.1f}h old" if age_h is not None else "age unknown"
    lines = [
        f"[{now_str()}] 💰 LARGE LIQUIDITY ADD  {sym}  {meta['pair']}",
        f"    +{quote_amt:,.4g} {meta['quote_symbol']} ({fmt_usd(usd)}) added — pair {age_s}",
    ]
    try:
        snap = snapshot_pair(w3, cfg, meta, block="latest", deep=False)
        share = None
        if snap.get("reserve_quote"):
            share = 100.0 * quote_amt / snap["reserve_quote"]
        lines.append(
            f"    pool now: {snap.get('liquidity_quote', 0):,.4g} {meta['quote_symbol']} "
            f"({fmt_usd(snap.get('liquidity_usd'))})   FDV {fmt_usd(snap.get('fdv_usd'))}   "
            f"supply in pool {snap.get('pct_supply_in_pool','?')}%"
            + (f"   (this add ≈ {share:.0f}% of pool)" if share else "")
        )
        fdv, liq = snap.get("fdv_usd"), snap.get("liquidity_usd")
        if fdv and liq and fdv > 20 * liq:
            lines.append(
                f"    ⚠️ FDV is {fdv/liq:.0f}x liquidity — the 'few million' cap is mostly "
                "supply held outside the pool, not committed money"
            )
    except Exception:
        pass
    if recipient:
        if recipient == DEAD_CS:
            lines.append("    LP tokens sent straight to burn address ✓")
        elif recipient in (cfg.known_lockers or []):
            lines.append("    LP tokens sent straight to a known locker ✓")
        else:
            has_code = False
            try:
                has_code = rpc(w3, "eth_getCode", [recipient, "latest"]) not in (None, "0x")
            except Exception:
                pass
            kind = "contract (launchpad/locker?)" if has_code else "wallet — they can pull this"
            lines.append(f"    LP tokens held by {recipient[:14]}… {kind}")
    return "\n".join(lines)


# --------------------------------------------------------------------------
# Live mode
# --------------------------------------------------------------------------


def run_live(cfg, args):
    w3 = get_w3(cfg)
    resolver = PairResolver(w3, cfg)
    spb, _ = seconds_per_block(w3)
    state_file = cfg.data_path("liqwatch_state.json")
    out_path = cfg.data_path("liquidity_adds.jsonl")
    state = {"last_block": None}
    try:
        with open(state_file) as f:
            state = json.load(f)
    except Exception:
        pass
    seen = set()

    print(
        f"Watching chain-wide Mint events on {cfg.rpc_url}; alerting on adds ≥ "
        f"${args.min_usd:,.0f} (or ≥ {args.min_quote} quote units when USD is unknown) "
        f"into pairs ≤ {args.max_age_hours}h old. Ctrl-C to stop."
    )
    while True:
        try:
            head = w3.eth.block_number - cfg.confirmations
            last = state.get("last_block") or head - 1
            if head > last:
                logs = safe_get_logs(
                    w3, None, [[TOPIC_MINT_V2, TOPIC_PAIR_CREATED]], last + 1, head,
                    chunk=min(cfg.logs_chunk_size, 2000), delay=cfg.rpc_delay_sec,
                )
                for lg in logs:
                    t0 = lg["topics"][0].lower()
                    if t0 == TOPIC_PAIR_CREATED.lower():
                        # a pair born on our watch: remember its creation block
                        try:
                            ev = decode_pair_created(lg)
                            if ev["factory"] in cfg.v2_factories:
                                meta = resolver.resolve(ev["pair"])
                                if meta:
                                    meta["creation_block"] = ev["block"]
                        except Exception:
                            pass
                        continue
                    if t0 != TOPIC_MINT_V2.lower():
                        continue
                    try:
                        mint = decode_mint(lg)
                    except Exception:
                        continue
                    key = (mint["tx"], mint["log_index"])
                    if key in seen:
                        continue
                    if len(seen) > 100_000:
                        seen.clear()
                    seen.add(key)
                    meta = resolver.resolve(mint["pair"])
                    if not meta:
                        continue
                    quote_amt = add_amounts(meta, mint)
                    usd = usd_value(cfg, meta, quote_amt)
                    big = (usd is not None and usd >= args.min_usd) or (
                        usd is None and quote_amt >= args.min_quote
                    )
                    if not big:
                        continue
                    age_h = pair_age_hours(w3, cfg, meta, mint["block"], spb)
                    if age_h is not None and age_h > args.max_age_hours:
                        continue
                    recipient = lp_recipient(w3, mint["tx"], meta["pair"])
                    text = describe(cfg, meta, mint, quote_amt, usd, age_h, recipient, w3)
                    print("\n" + text)
                    webhook(cfg, text)
                    with open(out_path, "a") as f:
                        f.write(json.dumps(to_jsonable({
                            "type": "large_add", "ts": int(time.time()), **meta,
                            "block": mint["block"], "tx": mint["tx"],
                            "quote_added": quote_amt, "usd_added": usd,
                            "pair_age_hours": age_h, "lp_recipient": recipient,
                        })) + "\n")
                state["last_block"] = head
                with open(state_file, "w") as f:
                    json.dump(state, f)
        except RpcError as e:
            print(f"[{now_str()}] RPC trouble: {e} — retrying next poll")
        except KeyboardInterrupt:
            print("\nStopping.")
            return
        time.sleep(cfg.poll_interval_sec)


# --------------------------------------------------------------------------
# History mode
# --------------------------------------------------------------------------


def run_history(cfg, args):
    w3 = get_w3(cfg)
    spb, latest = seconds_per_block(w3)
    start = max(1, latest - int(args.days * 86400 / spb))
    print(f"~{spb:.2f}s/block; scanning blocks {start:,} → {latest:,} ({args.days} days)")

    print("Phase 1: pair launches from configured factories...")
    plogs = safe_get_logs(w3, cfg.v2_factories, [TOPIC_PAIR_CREATED], start, latest,
                          chunk=cfg.logs_chunk_size, delay=cfg.rpc_delay_sec)
    pairs = {}
    for lg in plogs:
        try:
            ev = decode_pair_created(lg)
            pairs[ev["pair"]] = ev
        except Exception:
            continue
    print(f"  {len(pairs)} pairs launched in window.")
    if not pairs:
        return

    print("Phase 2: Mint events on those pairs (chunks of 200 addresses)...")
    addr_list = list(pairs)
    mints = []
    for i in range(0, len(addr_list), 200):
        chunk = addr_list[i : i + 200]
        mints.extend(
            safe_get_logs(w3, chunk, [TOPIC_MINT_V2], start, latest,
                          chunk=cfg.logs_chunk_size, delay=cfg.rpc_delay_sec)
        )
        print(f"  {min(i + 200, len(addr_list))}/{len(addr_list)} pairs scanned "
              f"({len(mints)} adds so far)")

    resolver = PairResolver(w3, cfg)
    rows = []
    for lg in mints:
        try:
            mint = decode_mint(lg)
        except Exception:
            continue
        ev = pairs.get(mint["pair"])
        if not ev:
            continue
        age_h = (mint["block"] - ev["block"]) * spb / 3600.0
        if age_h > args.max_age_hours:
            continue
        meta = resolver.resolve(mint["pair"])
        if not meta:
            continue
        meta["creation_block"] = ev["block"]
        quote_amt = add_amounts(meta, mint)
        usd = usd_value(cfg, meta, quote_amt)
        if (usd is not None and usd >= args.min_usd) or (usd is None and quote_amt >= args.min_quote):
            rows.append({"meta": meta, "mint": mint, "quote_added": quote_amt,
                         "usd_added": usd, "age_h": age_h})

    rows.sort(key=lambda r: -(r["usd_added"] if r["usd_added"] is not None else r["quote_added"]))
    print(f"\n{len(rows)} adds ≥ threshold within {args.max_age_hours}h of pair creation.")

    out_path = cfg.data_path("liquidity_adds_history.jsonl")
    with open(out_path, "w") as f:
        for r in rows:
            f.write(json.dumps(to_jsonable({
                **r["meta"], "block": r["mint"]["block"], "tx": r["mint"]["tx"],
                "quote_added": r["quote_added"], "usd_added": r["usd_added"],
                "pair_age_hours": round(r["age_h"], 2),
            })) + "\n")

    top = rows[: args.top]
    print(f"\nTop {len(top)} (current outcome fetched live):")
    print(f"{'pair':<14} {'token':<12} {'added':>14} {'age@add':>8} {'liq now':>12} "
          f"{'FDV now':>12}  outcome")
    for r in top:
        m = r["meta"]
        try:
            snap = snapshot_pair(w3, cfg, m, deep=False)
            liq, fdv = snap.get("liquidity_usd"), snap.get("fdv_usd")
        except Exception:
            liq = fdv = None
        added = fmt_usd(r["usd_added"]) if r["usd_added"] is not None else \
            f"{r['quote_added']:,.4g} {m['quote_symbol']}"
        if liq is not None and r["usd_added"] is not None and liq < max(500, 0.02 * r["usd_added"]):
            outcome = "RUGGED/DRAINED"
        elif fdv is not None and fdv >= 2_000_000:
            outcome = "multi-M FDV"
        else:
            outcome = "alive"
        print(f"{m['pair'][:12]+'…':<14} {m['token_symbol'][:11]:<12} {added:>14} "
              f"{r['age_h']:>7.1f}h {fmt_usd(liq):>12} {fmt_usd(fdv):>12}  {outcome}")
        time.sleep(cfg.rpc_delay_sec)
    print(f"\nFull list written to {out_path}")
    print(
        "\nRead the outcome column honestly: if big early adds rug as often as they moon,\n"
        "add size alone is hype-budget, not safety — combine with monitor.py's LP-custody\n"
        "and honeypot checks before treating it as a buy signal."
    )


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", default="config.json")
    ap.add_argument("--min-usd", type=float, default=25_000,
                    help="alert threshold for one add, in USD (default 25000)")
    ap.add_argument("--min-quote", type=float, default=5,
                    help="fallback threshold in quote units when USD price unknown")
    ap.add_argument("--max-age-hours", type=float, default=12,
                    help="only adds into pairs at most this old (default 12)")
    ap.add_argument("--days", type=float, default=None,
                    help="history mode: scan this many past days instead of watching live")
    ap.add_argument("--top", type=int, default=40, help="history mode: rows in outcome table")
    args = ap.parse_args()

    cfg = load_config(args.config, require=("rpc_url", "v2_factories", "wrapped_native"))
    if args.days:
        run_history(cfg, args)
    else:
        run_live(cfg, args)


if __name__ == "__main__":
    sys.exit(main())
