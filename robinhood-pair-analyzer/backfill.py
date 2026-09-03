#!/usr/bin/env python3
"""Scan recent history for pair launches and snapshot where they ended up.

    python backfill.py --days 14

Writes data/backfill.jsonl with one row per launch (current liquidity/FDV),
plus candidate label lists:
    data/winners.txt  — tokens currently at FDV >= --winner-fdv with real liquidity
    data/dead.txt     — tokens whose pool is now effectively empty (rug/abandoned)

Review those lists by hand (remove tokens you know are fake-volume or
team-propped) and then feed them to profile_winners.py. The unlabeled middle
is ignored on purpose — clean contrast makes a better profile.
"""

import argparse
import json
import sys
import time

from rhscan.chain import (
    TOPIC_PAIR_CREATED,
    decode_pair_created,
    get_w3,
    safe_get_logs,
    to_jsonable,
)
from rhscan.config import load_config
from rhscan.features import snapshot_pair


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", default="config.json")
    ap.add_argument("--days", type=float, default=14, help="how far back to scan")
    ap.add_argument("--from-block", type=int, default=None, help="override start block")
    ap.add_argument("--max-pairs", type=int, default=1500)
    ap.add_argument("--winner-fdv", type=float, default=2_000_000)
    ap.add_argument("--winner-min-liq", type=float, default=25_000)
    ap.add_argument("--dead-liq", type=float, default=500)
    args = ap.parse_args()

    cfg = load_config(args.config, require=("rpc_url", "v2_factories", "wrapped_native"))
    w3 = get_w3(cfg)
    latest = w3.eth.block_number

    if args.from_block:
        start = args.from_block
    else:
        # estimate blocks/day from recent timestamps rather than assuming a block time
        probe = max(1, latest - 50_000)
        t_latest = w3.eth.get_block(latest)["timestamp"]
        t_probe = w3.eth.get_block(probe)["timestamp"]
        spb = max(0.05, (t_latest - t_probe) / (latest - probe))
        start = max(1, latest - int(args.days * 86400 / spb))
        print(f"~{spb:.2f}s/block → scanning {latest - start:,} blocks ({args.days} days)")

    print(f"Fetching PairCreated logs {start:,} → {latest:,} from {len(cfg.v2_factories)} factories...")
    logs = safe_get_logs(w3, cfg.v2_factories, [TOPIC_PAIR_CREATED], start, latest,
                         chunk=cfg.logs_chunk_size, delay=cfg.rpc_delay_sec)
    events = []
    for lg in logs:
        try:
            events.append(decode_pair_created(lg))
        except Exception:
            continue
    print(f"{len(events)} pair launches found.")
    if len(events) > args.max_pairs:
        print(f"Capping at the most recent {args.max_pairs} (raise --max-pairs to scan all).")
        events = events[-args.max_pairs :]

    out_path = cfg.data_path("backfill.jsonl")
    winners, dead = [], []
    n_quote = 0
    with open(out_path, "w") as out:
        for i, ev in enumerate(events, 1):
            try:
                snap = snapshot_pair(w3, cfg, ev, deep=False)
            except Exception as e:
                snap = {"error": str(e), "pair": ev["pair"]}
            row = {"type": "backfill", **ev, **snap}
            label = None
            if not snap.get("skip_reason") and "error" not in snap:
                n_quote += 1
                liq, fdv = snap.get("liquidity_usd"), snap.get("fdv_usd")
                if liq is not None and fdv is not None:
                    if fdv >= args.winner_fdv and liq >= args.winner_min_liq:
                        label = "winner"
                        winners.append((snap["token"], snap.get("token_symbol", "?"), fdv, liq))
                    elif liq <= args.dead_liq:
                        label = "dead"
                        dead.append((snap["token"], snap.get("token_symbol", "?"), fdv, liq))
            row["label"] = label
            out.write(json.dumps(to_jsonable(row)) + "\n")
            if i % 25 == 0:
                print(f"  {i}/{len(events)} snapshotted ({len(winners)} winners, {len(dead)} dead so far)")
            time.sleep(cfg.rpc_delay_sec)

    def write_list(name, rows, header):
        path = cfg.data_path(name)
        with open(path, "w") as f:
            f.write(header)
            for tok, sym, fdv, liq in sorted(rows, key=lambda r: -r[2]):
                f.write(f"{tok}   # {sym}  fdv=${fdv:,.0f} liq=${liq:,.0f}\n")
        return path

    wp = write_list("winners.txt", winners, "# candidate winners — REVIEW BY HAND before profiling\n")
    dp = write_list("dead.txt", dead, "# dead/rugged pools — REVIEW BY HAND before profiling\n")
    print(
        f"\nDone. {len(events)} launches, {n_quote} vs known quotes, "
        f"{len(winners)} winner candidates, {len(dead)} dead pools."
        f"\n  {out_path}\n  {wp}\n  {dp}"
        f"\nNext: edit those lists, then run:  python profile_winners.py --winners {wp} --rugs {dp}"
    )


if __name__ == "__main__":
    sys.exit(main())
