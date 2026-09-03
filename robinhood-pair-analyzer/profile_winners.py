#!/usr/bin/env python3
"""Profile what past winners have in common — and what actually separates them
from rugs — then emit a profile.json that recalibrates monitor scoring.

    python profile_winners.py --winners data/winners.txt --rugs data/dead.txt

Input files: one token or pair address per line (# comments allowed) — the
lists that backfill.py writes, hand-reviewed. Profiling winners alone is
survivorship bias: plenty of rugs ALSO launched with "renounced + burned LP",
so the useful output is the features where the two groups genuinely differ.

--at-launch tries to snapshot each pair at its creation block (needs an
archive-capable RPC; falls back to current state per pair with a warning).
"""

import argparse
import json
import sys

from rhscan.chain import RpcError, call_fn, get_w3, to_jsonable
from rhscan.config import load_config
from rhscan.features import explorer_contract_creator, find_pair_creator, snapshot_pair
from rhscan.scoring import BOOL_FEATURES, NUMERIC_FEATURES, lp_safe_pct
from rhscan.simulate import SlotCache, simulate_round_trip
from web3 import Web3


def read_list(path):
    out = []
    with open(path) as f:
        for line in f:
            line = line.split("#", 1)[0].strip()
            if line:
                out.append(Web3.to_checksum_address(line))
    return out


def resolve_pair(w3, cfg, addr):
    """Accepts a pair or a token address; returns pair_info dict or None."""
    try:  # is it a v2 pair?
        t0 = call_fn(w3, addr, "token0()", ret_types=["address"])
        t1 = call_fn(w3, addr, "token1()", ret_types=["address"])
        return {"pair": addr, "token0": Web3.to_checksum_address(t0),
                "token1": Web3.to_checksum_address(t1), "dex": "v2"}
    except Exception:
        pass
    for factory in cfg.v2_factories:  # token: look up its pair vs each quote
        for quote in cfg.quote_map():
            try:
                p = call_fn(w3, factory, "getPair(address,address)", [addr, quote], ["address"])
            except Exception:
                continue
            if p and int(p, 16) != 0:
                pair = Web3.to_checksum_address(p)
                try:
                    t0 = Web3.to_checksum_address(call_fn(w3, pair, "token0()", ret_types=["address"]))
                    t1 = Web3.to_checksum_address(call_fn(w3, pair, "token1()", ret_types=["address"]))
                except Exception:
                    continue
                return {"pair": pair, "token0": t0, "token1": t1, "dex": "v2"}
    return None


def quantile(vals, q):
    vals = sorted(v for v in vals if v is not None)
    if not vals:
        return None
    if len(vals) == 1:
        return vals[0]
    pos = q * (len(vals) - 1)
    lo = int(pos)
    hi = min(lo + 1, len(vals) - 1)
    return round(vals[lo] + (vals[hi] - vals[lo]) * (pos - lo), 4)


def extract(w3, cfg, addr, slots, at_launch=False, with_sim=True):
    info = resolve_pair(w3, cfg, addr)
    if not info:
        return None, f"{addr}: could not resolve to a v2 pair (token has no pool vs configured quotes?)"
    block = "latest"
    creator, note = None, ""
    creator_addr, ctx = explorer_contract_creator(cfg, info["pair"])
    creation_block = None
    if ctx:
        try:
            creation_block = w3.eth.get_transaction(ctx)["blockNumber"]
        except Exception:
            pass
    if creation_block:
        creator = find_pair_creator(w3, cfg, info["pair"], creation_block) or creator_addr
    if at_launch and creation_block:
        try:
            probe = snapshot_pair(w3, cfg, info, creator=creator, block=creation_block + 20)
            if "error_reserves" not in probe:
                block = creation_block + 20
            else:
                note = " (no archive state; using current)"
        except RpcError:
            note = " (no archive state; using current)"
    snap = snapshot_pair(w3, cfg, info, creator=creator, block=block)
    if snap.get("skip_reason"):
        return None, f"{addr}: {snap['skip_reason']}"
    sim = {}
    if with_sim and snap.get("raw_reserve_quote"):
        try:
            sim = simulate_round_trip(w3, cfg, snap["token"], snap["quote"],
                                      snap["raw_reserve_quote"], slots)
        except Exception:
            sim = {}
    feats = dict(snap)
    feats["lp_safe_pct"] = lp_safe_pct(snap)
    feats["buy_tax_pct"] = sim.get("buy_tax_pct")
    feats["sell_tax_pct"] = sim.get("sell_tax_pct")
    feats["can_sell"] = sim.get("can_sell")
    label = f"{snap.get('token_symbol','?'):<10} {addr[:10]}…{note}"
    return feats, label


def summarize(rows_by_group):
    numeric, bools = {}, {}
    for feat in NUMERIC_FEATURES:
        numeric[feat] = {}
        for group, rows in rows_by_group.items():
            vals = [r.get(feat) for r in rows if isinstance(r.get(feat), (int, float))]
            for q, name in ((0.10, "p10"), (0.25, "p25"), (0.50, "p50"), (0.75, "p75"), (0.90, "p90")):
                numeric[feat][f"{group}_{name}"] = quantile(vals, q)
            numeric[feat][f"{group}_n"] = len(vals)
    for feat in BOOL_FEATURES:
        bools[feat] = {}
        for group, rows in rows_by_group.items():
            vals = [r.get(feat) for r in rows if r.get(feat) is not None]
            bools[feat][f"{group}_pct_true"] = (
                round(100.0 * sum(1 for v in vals if v) / len(vals), 1) if vals else None
            )
            bools[feat][f"{group}_n"] = len(vals)
    return numeric, bools


def print_report(numeric, bools, have_rugs):
    def fmt(v):
        if v is None:
            return "—"
        return f"{v:,.4g}"

    print("\n===== numeric features: median [p25–p75] =====")
    header = f"{'feature':<24} {'winners':>26}"
    if have_rugs:
        header += f" {'rugs':>26}"
    print(header)
    ranked = []
    for feat, d in numeric.items():
        w = f"{fmt(d.get('winner_p50'))} [{fmt(d.get('winner_p25'))}–{fmt(d.get('winner_p75'))}]"
        line = f"{feat:<24} {w:>26}"
        if have_rugs:
            r = f"{fmt(d.get('rug_p50'))} [{fmt(d.get('rug_p25'))}–{fmt(d.get('rug_p75'))}]"
            line += f" {r:>26}"
            wm, rm = d.get("winner_p50"), d.get("rug_p50")
            iqr = (
                (d.get("winner_p75") or 0) - (d.get("winner_p25") or 0)
                + (d.get("rug_p75") or 0) - (d.get("rug_p25") or 0)
            )
            if wm is not None and rm is not None:
                ranked.append((abs(wm - rm) / (abs(iqr) + 1e-9), feat, "numeric"))
        print(line)

    print("\n===== boolean features: % true =====")
    for feat, d in bools.items():
        line = f"{feat:<24} winners {fmt(d.get('winner_pct_true')):>7}%"
        if have_rugs:
            line += f"   rugs {fmt(d.get('rug_pct_true')):>7}%"
            w, r = d.get("winner_pct_true"), d.get("rug_pct_true")
            if w is not None and r is not None:
                ranked.append((abs(w - r) / 100.0, feat, "bool"))
        print(line)

    if have_rugs and ranked:
        ranked.sort(reverse=True)
        print("\n===== features that most separate winners from rugs =====")
        for sep, feat, kind in ranked[:8]:
            print(f"  {feat:<24} separation {sep:.2f} ({kind})")
        print(
            "\nA feature common to winners but equally common in rugs is NOT a signal —"
            "\ntrust the separation ranking over raw winner commonalities."
        )


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", default="config.json")
    ap.add_argument("--winners", required=True)
    ap.add_argument("--rugs", default=None)
    ap.add_argument("--at-launch", action="store_true",
                    help="snapshot at pair creation block (archive RPC needed)")
    ap.add_argument("--no-sim", action="store_true",
                    help="skip buy/sell simulation (note: sim reflects CURRENT contract behavior)")
    ap.add_argument("--out", default=None, help="profile output path (default data/profile.json)")
    args = ap.parse_args()

    cfg = load_config(args.config, require=("rpc_url", "v2_factories", "wrapped_native"))
    w3 = get_w3(cfg)
    slots = SlotCache()

    groups = {"winner": read_list(args.winners)}
    if args.rugs:
        groups["rug"] = read_list(args.rugs)

    rows_by_group = {}
    for group, addrs in groups.items():
        rows = []
        print(f"\nExtracting features for {len(addrs)} {group}(s)...")
        for addr in addrs:
            try:
                feats, label = extract(w3, cfg, addr, slots,
                                       at_launch=args.at_launch, with_sim=not args.no_sim)
            except RpcError as e:
                feats, label = None, f"{addr}: RPC error {e}"
            if feats is None:
                print(f"  skip {label}")
                continue
            print(f"  ok   {label}")
            rows.append(feats)
        rows_by_group[group] = rows

    if not rows_by_group.get("winner"):
        sys.exit("No winner rows extracted — nothing to profile.")

    numeric, bools = summarize(rows_by_group)
    print_report(numeric, bools, have_rugs="rug" in rows_by_group)

    out = args.out or cfg.data_path("profile.json")
    profile = {
        "counts": {g: len(r) for g, r in rows_by_group.items()},
        "at_launch": args.at_launch,
        "features": numeric,
        "bools": bools,
    }
    with open(out, "w") as f:
        json.dump(to_jsonable(profile), f, indent=2)
    print(
        f"\nWrote {out} — set \"profile_file\": \"{out}\" in config.json so the monitor "
        "scores new pairs against these calibrated thresholds."
    )


if __name__ == "__main__":
    sys.exit(main())
