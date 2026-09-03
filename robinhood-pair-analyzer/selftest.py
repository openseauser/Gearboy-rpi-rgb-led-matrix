#!/usr/bin/env python3
"""Offline selftest — no RPC needed. Verifies event signatures against the
canonical Uniswap topic hashes, ABI encode/decode round-trips, storage-key
math, and scoring behavior on synthetic launches."""

import json
import sys

from eth_abi import encode as abi_encode
from web3 import Web3

from rhscan import chain
from rhscan.config import Config
from rhscan.features import checkpoint_delta
from rhscan.scoring import score_pair
from rhscan.simulate import _calldata

FAILURES = []


def check(name, cond, detail=""):
    status = "ok  " if cond else "FAIL"
    print(f"[{status}] {name}" + (f"  {detail}" if detail and not cond else ""))
    if not cond:
        FAILURES.append(name)


def main():
    # 1. event topics vs canonical constants used across every EVM chain
    check(
        "PairCreated topic matches Uniswap v2 canonical hash",
        chain.TOPIC_PAIR_CREATED
        == "0x0d3648bd0f6ba80134a33ba9275ac585d9d315f0ad8355cddefde31afa28d0e9",
        chain.TOPIC_PAIR_CREATED,
    )
    check(
        "Swap topic matches Uniswap v2 canonical hash",
        chain.TOPIC_SWAP_V2
        == "0xd78ad95fa46c994b6551d0da85fc275fe613ce37657fb8d5e3d130840159d822",
        chain.TOPIC_SWAP_V2,
    )
    check(
        "Transfer topic matches ERC-20 canonical hash",
        chain.TOPIC_TRANSFER
        == "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef",
        chain.TOPIC_TRANSFER,
    )
    check(
        "PoolCreated topic matches Uniswap v3 canonical hash",
        chain.TOPIC_POOL_CREATED
        == "0x783cca1c0412dd0d695e784568c96da2e9c22ff989357a2e8b1d9b2b4e6b7118",
        chain.TOPIC_POOL_CREATED,
    )
    check(
        "approve() selector is 0x095ea7b3",
        chain.selector("approve(address,uint256)").hex().replace("0x", "") == "095ea7b3",
    )
    check(
        "transfer() selector is 0xa9059cbb",
        chain.selector("transfer(address,uint256)").hex().replace("0x", "") == "a9059cbb",
    )

    # 2. PairCreated decode round-trip on a synthetic log
    t0 = "0x00000000000000000000000000000000000000A1"
    t1 = "0x00000000000000000000000000000000000000B2"
    pair = "0x00000000000000000000000000000000000000C3"
    log = {
        "address": "0x00000000000000000000000000000000000000f0",
        "topics": [
            chain.TOPIC_PAIR_CREATED,
            "0x" + abi_encode(["address"], [t0]).hex(),
            "0x" + abi_encode(["address"], [t1]).hex(),
        ],
        "data": "0x" + abi_encode(["address", "uint256"], [pair, 7]).hex(),
        "blockNumber": hex(1234),
        "transactionHash": "0x" + "ab" * 32,
    }
    ev = chain.decode_pair_created(log)
    check(
        "decode_pair_created round-trip",
        ev["token0"] == Web3.to_checksum_address(t0)
        and ev["token1"] == Web3.to_checksum_address(t1)
        and ev["pair"] == Web3.to_checksum_address(pair)
        and ev["block"] == 1234,
        str(ev),
    )

    # 3. storage-key math: abi_encode path == manual 32-byte concat
    addr = "0x1111111111111111111111111111111111111111"
    manual = Web3.keccak(
        bytes.fromhex(addr[2:]).rjust(32, b"\x00") + (5).to_bytes(32, "big")
    )
    check(
        "solidity mapping key = keccak(pad32(addr) ++ pad32(slot))",
        chain.storage_key_for_balance(addr, 5, "solidity")
        == "0x" + manual.hex().replace("0x", ""),
    )
    k = chain.storage_key_for_allowance(addr, addr, 3, "solidity")
    check("allowance key is 32 bytes hex", k.startswith("0x") and len(k) == 66)

    # 4. calldata builder
    cd = _calldata("approve(address,uint256)", ["address", "uint256"], [addr, 2**256 - 1])
    check(
        "approve calldata layout",
        cd.startswith("0x095ea7b3") and len(cd) == 2 + 8 + 64 + 64,
        cd[:20],
    )

    # 5. scoring on synthetic launches
    class FakeCfg:
        blacklist_is_hard_flag = True
        min_initial_liquidity_usd = 2000

    clean = {
        "lp_burned_pct": 100.0, "lp_locked_pct": 0.0, "lp_creator_pct": 0.0,
        "owner_renounced": True, "sel_mint": False, "sel_blacklist": False,
        "sel_pause": False, "sel_fee_setter": False, "is_upgradeable_proxy": False,
        "liquidity_usd": 25000, "pct_supply_in_pool": 95.0, "creator_token_pct": 1.0,
        "verified": True, "top10_holders_pct": 18.0, "owner": None,
    }
    clean_sim = {"can_buy": True, "can_sell": True, "buy_tax_pct": 1.0, "sell_tax_pct": 1.0}
    scam = {
        "lp_burned_pct": 0.0, "lp_locked_pct": 0.0, "lp_creator_pct": 100.0,
        "owner_renounced": False, "sel_mint": True, "sel_blacklist": True,
        "sel_pause": False, "sel_fee_setter": True, "is_upgradeable_proxy": False,
        "liquidity_usd": 600, "pct_supply_in_pool": 30.0, "creator_token_pct": 40.0,
        "verified": False, "top10_holders_pct": 80.0, "owner": "0x" + "9" * 40,
    }
    scam_sim = {"can_buy": True, "can_sell": False, "honeypot_suspected": True}

    rc = score_pair(clean, clean_sim, FakeCfg())
    rs = score_pair(scam, scam_sim, FakeCfg())
    check("clean launch scores high", rc["score"] >= 75 and rc["verdict"] == "PROMISING",
          json.dumps(rc))
    check("clean launch has no hard flags", not rc["hard_flags"], str(rc["hard_flags"]))
    check("scam scores AVOID with hard flags",
          rs["verdict"] == "AVOID" and rs["score"] <= 10 and len(rs["hard_flags"]) >= 3,
          json.dumps(rs))
    check("clean outranks scam", rc["score"] > rs["score"])

    # profile calibration changes thresholds
    profile = {"features": {"liquidity_usd": {"winner_p25": 90000, "winner_p10": 50000}}}
    rc_cal = score_pair(clean, clean_sim, FakeCfg(), profile)
    check("profile calibration tightens liquidity bar", rc_cal["score"] < rc["score"],
          f"{rc_cal['score']} vs {rc['score']}")

    # 5b. liquidity_watch: Mint decode + quote-side sizing
    from liquidity_watch import add_amounts, decode_mint

    mlog = {
        "address": "0x00000000000000000000000000000000000000C3",
        "topics": [chain.TOPIC_MINT_V2, "0x" + abi_encode(["address"], [addr]).hex()],
        "data": "0x" + abi_encode(["uint256", "uint256"], [5 * 10**18, 9 * 10**6]).hex(),
        "blockNumber": hex(777),
        "transactionHash": "0x" + "cd" * 32,
        "logIndex": "0x1",
    }
    mint = decode_mint(mlog)
    check(
        "decode_mint round-trip",
        mint["amount0"] == 5 * 10**18 and mint["amount1"] == 9 * 10**6
        and mint["block"] == 777 and mint["sender"] == Web3.to_checksum_address(addr),
        str(mint),
    )
    meta_q0 = {"quote_is_token0": True, "quote_decimals": 18}
    meta_q1 = {"quote_is_token0": False, "quote_decimals": 6}
    check(
        "add_amounts picks the quote side with its decimals",
        add_amounts(meta_q0, mint) == 5.0 and add_amounts(meta_q1, mint) == 9.0,
        f"{add_amounts(meta_q0, mint)} / {add_amounts(meta_q1, mint)}",
    )

    # 6. checkpoint delta / rug math
    d = checkpoint_delta({"liquidity_quote": 10.0, "creator_token_pct": 10.0},
                         {"liquidity_quote": 1.5, "creator_token_pct": 2.0})
    check("liquidity pull computed", d["liquidity_change_pct"] == -85.0, str(d))
    check("creator dump computed", d["creator_token_pct_delta"] == -8.0, str(d))

    # 7. config example parses and normalizes
    with open("config.example.json") as f:
        cfg = Config(json.load(f), "config.example.json")
    check("config.example.json loads", cfg.chain_id == 4663 and cfg.poll_interval_sec == 6)
    check("to_jsonable handles bytes", chain.to_jsonable({"a": b"\x01"})["a"] == "0x01")

    print()
    if FAILURES:
        print(f"{len(FAILURES)} FAILURE(S): {FAILURES}")
        return 1
    print("all selftests passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
