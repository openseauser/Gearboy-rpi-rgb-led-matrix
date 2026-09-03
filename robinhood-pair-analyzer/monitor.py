#!/usr/bin/env python3
"""Live monitor for new DEX pairs on Robinhood Chain (or any EVM chain).

    python monitor.py --discover        # find live factory + wrapped-native addresses
    python monitor.py                   # watch for new pairs, score them, track them

Every new v2 pair whose one side is a configured quote token gets a full
snapshot (liquidity, LP custody, contract traits, holders) plus a buy/sell
simulation, and a 0-100 risk-screen score. Pairs are then re-checked at the
configured checkpoints; a liquidity pull or creator dump raises a rug alert.

All snapshots append to data/pairs.jsonl — after a few days that file is a
labeled-outcome dataset you can feed to profile_winners.py.

This screens for scam mechanics; it does NOT predict price. Assume any new
token can still go to zero.
"""

import argparse
import collections
import json
import os
import sys
import time
from datetime import datetime, timezone

import requests

from rhscan.chain import (
    TOPIC_PAIR_CREATED,
    TOPIC_POOL_CREATED,
    RpcError,
    decode_pair_created,
    decode_pool_created,
    get_w3,
    safe_get_logs,
    to_jsonable,
    tx_sender,
)
from rhscan.config import load_config
from rhscan.features import erc20_info, snapshot_pair, swap_stats, checkpoint_delta
from rhscan.scoring import explain, score_pair
from rhscan.simulate import SlotCache, simulate_round_trip


def now_str():
    return datetime.now(timezone.utc).strftime("%H:%M:%S")


def log(msg=""):
    print(msg, flush=True)


def webhook(cfg, text):
    if not cfg.webhook_url:
        return
    try:  # Discord/Slack-compatible minimal payload
        requests.post(cfg.webhook_url, json={"content": text, "text": text}, timeout=10)
    except Exception as e:
        log(f"  (webhook failed: {e})")


# --------------------------------------------------------------------------
# --discover: find factories + wrapped native without knowing any addresses
# --------------------------------------------------------------------------


def discover(cfg, blocks):
    w3 = get_w3(cfg)
    latest = w3.eth.block_number
    start = max(1, latest - blocks)
    log(f"Scanning blocks {start:,} → {latest:,} for PairCreated/PoolCreated events...")
    v2 = safe_get_logs(w3, None, [TOPIC_PAIR_CREATED], start, latest, chunk=2000,
                       delay=cfg.rpc_delay_sec)
    v3 = safe_get_logs(w3, None, [TOPIC_POOL_CREATED], start, latest, chunk=2000,
                       delay=cfg.rpc_delay_sec)

    def report(name, logs, decoder):
        fac = collections.Counter()
        token_freq = collections.Counter()
        for lg in logs:
            try:
                ev = decoder(lg)
            except Exception:
                continue
            fac[ev["factory"]] += 1
            token_freq[ev["token0"]] += 1
            token_freq[ev["token1"]] += 1
        log(f"\n{name} factories emitting in the window:")
        if not fac:
            log("  (none found — try a bigger --discover-blocks window)")
        for addr, n in fac.most_common(10):
            log(f"  {addr}   {n} new pairs")
        return fac, token_freq

    fac2, tf2 = report("Uniswap-v2-style", v2, decode_pair_created)
    fac3, tf3 = report("Uniswap-v3-style", v3, decode_pool_created)

    combined = tf2 + tf3
    if combined:
        log("\nMost common pair-side tokens (the top one is almost certainly the wrapped native / main quote):")
        for addr, n in combined.most_common(8):
            try:
                info = erc20_info(w3, addr)
                log(f"  {addr}   {info['symbol']:<8} in {n} pairs")
            except Exception:
                log(f"  {addr}   ?        in {n} pairs")
    log(
        "\nPaste the factory addresses into config.json (v2_factories / v3_factories), the top "
        "quote token into wrapped_native (verify its symbol is WETH!), and set v2_router "
        "(check the factory's verified source or the Uniswap deployments page / Blockscout)."
    )


# --------------------------------------------------------------------------
# Reporting
# --------------------------------------------------------------------------


def fmt_usd(v):
    return f"${v:,.0f}" if v is not None else "?USD"


def report_new_pair(snap, sim, result):
    tok = f"{snap.get('token_name', '?')} ({snap.get('token_symbol', '?')})"
    head = (
        f"[{now_str()}] NEW PAIR  {snap.get('token_symbol','?')}/{snap.get('quote_symbol','?')} "
        f" {snap['pair']}"
    )
    lines = [head]
    lines.append(f"    token: {tok}  {snap.get('token')}")
    lines.append(
        f"    liquidity: {snap.get('liquidity_quote', 0):,.4g} {snap.get('quote_symbol','?')} "
        f"({fmt_usd(snap.get('liquidity_usd'))})   FDV {fmt_usd(snap.get('fdv_usd'))}   "
        f"supply in pool: {snap.get('pct_supply_in_pool', '?')}%"
    )
    lines.append(
        f"    LP: burned {snap.get('lp_burned_pct', '?')}% | locked {snap.get('lp_locked_pct', '?')}% "
        f"| creator {snap.get('lp_creator_pct', '?')}%"
    )
    simline = "    sim: "
    if sim.get("can_buy") is None and sim.get("can_sell") is None:
        simline += "unavailable (" + "; ".join(sim.get("notes", []) or ["?"]) + ")"
    else:
        b = {True: "✓", False: "✗", None: "?"}[sim.get("can_buy")]
        s = {True: "✓", False: "✗", None: "?"}[sim.get("can_sell")]
        bt = f" tax {sim['buy_tax_pct']:.1f}%" if sim.get("buy_tax_pct") is not None else ""
        st = f" tax {sim['sell_tax_pct']:.1f}%" if sim.get("sell_tax_pct") is not None else ""
        simline += f"buy {b}{bt}   sell {s}{st}   [{sim.get('method')}]"
    lines.append(simline)
    lines.append(f"    score: {result['score']}/100  {result['verdict']}")
    lines.append(explain(result))
    return "\n".join(lines)


# --------------------------------------------------------------------------
# Main loop
# --------------------------------------------------------------------------


class Monitor:
    def __init__(self, cfg, from_block=None):
        self.cfg = cfg
        self.w3 = get_w3(cfg)
        self.slots = SlotCache()
        self.profile = None
        if cfg.profile_file and os.path.exists(cfg.profile_file):
            with open(cfg.profile_file) as f:
                self.profile = json.load(f)
            log(f"Loaded scoring profile from {cfg.profile_file}")
        self.state_file = cfg.data_path("state.json")
        self.jsonl = cfg.data_path("pairs.jsonl")
        self.state = {"last_block": None, "watch": {}}
        if os.path.exists(self.state_file):
            try:
                with open(self.state_file) as f:
                    self.state = json.load(f)
            except Exception:
                pass
        if from_block is not None:
            self.state["last_block"] = from_block - 1

    def save_state(self):
        with open(self.state_file, "w") as f:
            json.dump(to_jsonable(self.state), f)

    def append_jsonl(self, row):
        with open(self.jsonl, "a") as f:
            f.write(json.dumps(to_jsonable(row)) + "\n")

    # ---------------- new pair handling ----------------

    def handle_new_pair(self, ev):
        cfg, w3 = self.cfg, self.w3
        creator = tx_sender(w3, ev["tx"])
        if ev["dex"] != "v2":
            info0 = erc20_info(w3, ev["token0"])
            info1 = erc20_info(w3, ev["token1"])
            log(
                f"[{now_str()}] new v3 pool {info0['symbol']}/{info1['symbol']} {ev['pair']} "
                f"(fee {ev.get('fee', '?')}) — v3 pools get discovery only, no deep analysis"
            )
            self.append_jsonl({"type": "v3_pool", **ev, "creator": creator})
            return

        snap = snapshot_pair(w3, cfg, ev, creator=creator)
        if snap.get("skip_reason"):
            log(f"[{now_str()}] new pair {ev['pair']} skipped: {snap['skip_reason']}")
            self.append_jsonl({"type": "skipped", **ev, **snap})
            return

        sim = {}
        if snap.get("raw_reserve_quote"):
            try:
                sim = simulate_round_trip(
                    w3, cfg, snap["token"], snap["quote"], snap["raw_reserve_quote"], self.slots
                )
            except Exception as e:
                sim = {"notes": [f"simulation crashed: {e}"]}
        else:
            sim = {"notes": ["no liquidity yet at first sight; sim deferred to checkpoints"]}

        result = score_pair(snap, sim, cfg, self.profile)
        text = report_new_pair(snap, sim, result)
        log("\n" + text)
        if result["score"] >= cfg.alert_min_score and not result["hard_flags"]:
            webhook(cfg, text)

        self.append_jsonl({"type": "launch", **ev, **snap, "sim": sim, "scoring": result})
        self.state["watch"][ev["pair"]] = {
            "ev": ev,
            "creator": creator,
            "created_ts": time.time(),
            "created_block": ev["block"],
            "first": snap,
            "done": [],
            "last_score": result["score"],
        }

    # ---------------- checkpoints ----------------

    def run_checkpoints(self):
        cfg, w3 = self.cfg, self.w3
        finished = []
        for pair, wch in list(self.state["watch"].items()):
            age_min = (time.time() - wch["created_ts"]) / 60
            due = [m for m in cfg.watch_checkpoints_min if m <= age_min and m not in wch["done"]]
            if not due:
                if age_min > max(cfg.watch_checkpoints_min) + 1:
                    finished.append(pair)
                continue
            cp = max(due)
            wch["done"] = sorted(set(wch["done"]) | set(due))
            ev, first = wch["ev"], wch["first"]
            try:
                snap = snapshot_pair(w3, cfg, ev, creator=wch.get("creator"), deep=False)
                delta = checkpoint_delta(first, snap)
                latest = w3.eth.block_number
                stats = swap_stats(
                    w3, cfg, pair, ev["token0"] == first.get("token"),
                    wch["created_block"], latest, creator=wch.get("creator"),
                )
            except Exception as e:
                log(f"[{now_str()}] checkpoint {cp}m {pair}: failed ({e})")
                continue

            liq_chg = delta.get("liquidity_change_pct")
            sym = f"{first.get('token_symbol','?')}/{first.get('quote_symbol','?')}"
            msg = (
                f"[{now_str()}] t+{cp}m {sym} {pair}\n"
                f"    liq {snap.get('liquidity_quote', 0):,.4g} {first.get('quote_symbol','?')} "
                f"({'%+.1f' % liq_chg + '%' if liq_chg is not None else '?'})   "
                f"FDV {fmt_usd(snap.get('fdv_usd'))} "
                f"({'%+.1f' % delta['fdv_change_pct'] + '%' if delta.get('fdv_change_pct') is not None else '?'})\n"
                f"    swaps {stats['swaps']} (buys {stats['buys']} / sells {stats['sells']}), "
                f"unique buyers≈{stats['unique_buyers_sampled']}, creator sells {stats['creator_sells']}"
            )
            alerts = []
            if liq_chg is not None and liq_chg <= -cfg.watch_liquidity_drop_alert_pct:
                alerts.append(f"🚨 LIQUIDITY PULLED {liq_chg:+.0f}% on {sym} {pair} — rug in progress")
            if delta.get("creator_token_pct_delta") is not None and delta["creator_token_pct_delta"] < -5:
                alerts.append(f"⚠️ creator dumped {-delta['creator_token_pct_delta']:.1f}% of supply on {sym}")
            if stats["creator_sells"] > 0:
                alerts.append(f"⚠️ creator wallet made {stats['creator_sells']} sell(s) on {sym}")
            for a in alerts:
                msg += "\n    " + a
                webhook(cfg, a)
            log(msg)
            self.append_jsonl(
                {"type": "checkpoint", "minute": cp, "pair": pair, **snap,
                 "delta": delta, "swap_stats": stats}
            )
        for p in finished:
            del self.state["watch"][p]

    # ---------------- polling ----------------

    def poll_once(self):
        cfg, w3 = self.cfg, self.w3
        head = w3.eth.block_number - cfg.confirmations
        last = self.state.get("last_block")
        if last is None:
            last = head - 1
        if head <= last:
            return
        addresses = cfg.v2_factories + cfg.v3_factories
        logs = safe_get_logs(
            w3, addresses, [[TOPIC_PAIR_CREATED, TOPIC_POOL_CREATED]],
            last + 1, head, chunk=cfg.logs_chunk_size, delay=cfg.rpc_delay_sec,
        )
        for lg in logs:
            t0 = lg["topics"][0] if isinstance(lg["topics"][0], str) else lg["topics"][0].hex()
            if not t0.startswith("0x"):
                t0 = "0x" + t0
            try:
                if t0.lower() == TOPIC_PAIR_CREATED.lower():
                    ev = decode_pair_created(lg)
                elif t0.lower() == TOPIC_POOL_CREATED.lower():
                    ev = decode_pool_created(lg)
                else:
                    continue
                self.handle_new_pair(ev)
            except Exception as e:
                log(f"[{now_str()}] failed to process log in tx {lg.get('transactionHash')}: {e}")
        self.state["last_block"] = head

    def run(self, once=False):
        cfg = self.cfg
        log(
            f"Monitoring {len(cfg.v2_factories)} v2 + {len(cfg.v3_factories)} v3 factories on "
            f"{cfg.rpc_url} (chain {cfg.chain_id}); checkpoints at {cfg.watch_checkpoints_min} min."
        )
        log("Risk screen only — not financial advice; new tokens can go to zero regardless of score.\n")
        while True:
            try:
                self.poll_once()
                self.run_checkpoints()
                self.save_state()
            except RpcError as e:
                log(f"[{now_str()}] RPC trouble: {e} — retrying next poll")
            except KeyboardInterrupt:
                log("\nStopping; state saved.")
                self.save_state()
                return
            if once:
                return
            time.sleep(cfg.poll_interval_sec)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", default="config.json")
    ap.add_argument("--discover", action="store_true", help="scan recent blocks for factory addresses, then exit")
    ap.add_argument("--discover-blocks", type=int, default=60000)
    ap.add_argument("--from-block", type=int, default=None, help="start scanning from this block")
    ap.add_argument("--once", action="store_true", help="single poll cycle (for testing)")
    args = ap.parse_args()

    if args.discover:
        cfg = load_config(args.config)
        discover(cfg, args.discover_blocks)
        return

    cfg = load_config(args.config, require=("rpc_url", "v2_factories", "wrapped_native", "v2_router"))
    Monitor(cfg, from_block=args.from_block).run(once=args.once)


if __name__ == "__main__":
    sys.exit(main())
