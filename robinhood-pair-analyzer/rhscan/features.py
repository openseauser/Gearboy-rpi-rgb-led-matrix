"""Feature extraction for a pair + its token: liquidity, FDV, LP custody,
contract traits, deployer stake, early trading stats, optional explorer data."""

import time

import requests
from web3 import Web3

from .chain import (
    DEAD,
    TOPIC_MINT_V2,
    TOPIC_SWAP_V2,
    ZERO,
    RpcError,
    balance_of,
    call_fn,
    erc20_info,
    get_code,
    get_reserves,
    proxy_check,
    safe_get_logs,
    scan_selectors,
    token_owner,
    tx_sender,
    _hb,
    topic_addr,
)

DEAD_ADDRS = (Web3.to_checksum_address(DEAD), ZERO)


# --------------------------------------------------------------------------
# Pricing helpers
# --------------------------------------------------------------------------

_native_price_cache = {"ts": 0, "usd": 0.0}


def native_usd(cfg):
    """Native token USD price: config override, else CoinGecko (cached 10 min),
    else 0 (= unknown; USD figures reported as None)."""
    if float(cfg.native_usd_price or 0) > 0:
        return float(cfg.native_usd_price)
    now = time.time()
    if now - _native_price_cache["ts"] < 600 and _native_price_cache["usd"]:
        return _native_price_cache["usd"]
    try:
        r = requests.get(
            "https://api.coingecko.com/api/v3/simple/price",
            params={"ids": cfg.coingecko_native_id, "vs_currencies": "usd"},
            timeout=10,
        )
        usd = float(r.json()[cfg.coingecko_native_id]["usd"])
        _native_price_cache.update(ts=now, usd=usd)
        return usd
    except Exception:
        return _native_price_cache["usd"] or 0.0


def quote_usd_price(cfg, quote_addr):
    q = cfg.quote_map().get(quote_addr, {})
    usd = float(q.get("usd") or 0)
    if usd <= 0 and quote_addr == cfg.wrapped_native:
        usd = native_usd(cfg)
    return usd or None


# --------------------------------------------------------------------------
# Explorer (Blockscout) — all optional, all failures swallowed
# --------------------------------------------------------------------------


def _explorer_get(cfg, path, params=None, timeout=10):
    if not cfg.explorer_base:
        return None
    try:
        r = requests.get(cfg.explorer_base.rstrip("/") + path, params=params or {}, timeout=timeout)
        if r.status_code != 200:
            return None
        return r.json()
    except Exception:
        return None


def explorer_is_verified(cfg, token):
    j = _explorer_get(cfg, "/api", {"module": "contract", "action": "getsourcecode", "address": token})
    try:
        src = j["result"][0].get("SourceCode") or ""
        return bool(src.strip())
    except Exception:
        return None


def explorer_contract_creator(cfg, address):
    j = _explorer_get(
        cfg, "/api", {"module": "contract", "action": "getcontractcreation", "contractaddresses": address}
    )
    try:
        row = j["result"][0]
        return Web3.to_checksum_address(row["contractCreator"]), row.get("txHash")
    except Exception:
        return None, None


def explorer_top_holders(cfg, token, total_supply, exclude, top_n=10):
    """Returns (top_n_pct_of_supply, holder_count_seen) or (None, None)."""
    j = _explorer_get(cfg, f"/api/v2/tokens/{token}/holders", timeout=15)
    try:
        items = j.get("items", [])
    except Exception:
        return None, None
    if not items or not total_supply:
        return None, None
    excl = {a.lower() for a in exclude if a}
    vals = []
    for it in items:
        try:
            addr = (it.get("address") or {}).get("hash", "").lower()
            if addr in excl:
                continue
            vals.append(int(it.get("value", "0")))
        except Exception:
            continue
    top = sorted(vals, reverse=True)[:top_n]
    return round(100.0 * sum(top) / total_supply, 2), len(items)


# --------------------------------------------------------------------------
# Snapshot
# --------------------------------------------------------------------------


def identify_quote_side(cfg, token0, token1):
    """Returns (token, quote) or (None, None) if neither side is a known quote.
    If both sides are quotes (e.g. WETH/USDC) the pair is not a launch — skip."""
    quotes = cfg.quote_map()
    q0, q1 = token0 in quotes, token1 in quotes
    if q0 and not q1:
        return token1, token0
    if q1 and not q0:
        return token0, token1
    return None, None


def snapshot_pair(w3, cfg, pair_info, creator=None, block="latest", deep=True):
    """Feature snapshot of a v2 pair. `pair_info` from decode_pair_created or
    {"pair","token0","token1","dex"}. Returns a flat dict; unknown = None."""
    pair = pair_info["pair"]
    token, quote = identify_quote_side(cfg, pair_info["token0"], pair_info["token1"])
    snap = {
        "ts": int(time.time()),
        "block": None,
        "pair": pair,
        "dex": pair_info.get("dex", "v2"),
        "token": token,
        "quote": quote,
        "creator": creator,
    }
    if token is None:
        snap["skip_reason"] = "neither side is a configured quote token (meme/meme or unknown quote)"
        return snap
    if isinstance(block, int):
        snap["block"] = block

    qmeta = cfg.quote_map().get(quote, {})
    snap["quote_symbol"] = qmeta.get("symbol", "?")
    q_usd = quote_usd_price(cfg, quote)

    tinfo = erc20_info(w3, token, block=block)
    snap.update(
        token_name=tinfo["name"],
        token_symbol=tinfo["symbol"],
        token_decimals=tinfo["decimals"],
        token_total_supply=tinfo["total_supply"],
    )
    try:
        q_dec = int(call_fn(w3, quote, "decimals()", ret_types=["uint8"], block=block))
    except Exception:
        q_dec = 18

    # --- reserves / price / FDV ---
    try:
        r0, r1, _ = get_reserves(w3, pair, block=block)
        t0 = pair_info["token0"]
        r_token, r_quote = (r0, r1) if token == t0 else (r1, r0)
        snap["reserve_token"] = r_token / 10 ** tinfo["decimals"]
        snap["reserve_quote"] = r_quote / 10**q_dec
        snap["liquidity_quote"] = 2 * snap["reserve_quote"]
        snap["liquidity_usd"] = round(snap["liquidity_quote"] * q_usd, 2) if q_usd else None
        if r_token > 0 and tinfo["total_supply"]:
            price = snap["reserve_quote"] / snap["reserve_token"]
            supply = tinfo["total_supply"] / 10 ** tinfo["decimals"]
            snap["price_quote"] = price
            snap["fdv_quote"] = price * supply
            snap["fdv_usd"] = round(snap["fdv_quote"] * q_usd, 2) if q_usd else None
            snap["pct_supply_in_pool"] = round(100.0 * snap["reserve_token"] / supply, 2)
        snap["raw_reserve_quote"] = r_quote
    except RpcError as e:
        snap["error_reserves"] = str(e)

    # --- LP custody ---
    try:
        lp_total = int(call_fn(w3, pair, "totalSupply()", ret_types=["uint256"], block=block))
        if lp_total > 0:
            burned = sum(balance_of(w3, pair, a, block=block) for a in DEAD_ADDRS)
            snap["lp_burned_pct"] = round(100.0 * burned / lp_total, 2)
            locked = 0
            for lk in cfg.known_lockers:
                locked += balance_of(w3, pair, lk, block=block)
            snap["lp_locked_pct"] = round(100.0 * locked / lp_total, 2)
            if creator:
                dep = balance_of(w3, pair, creator, block=block)
                snap["lp_creator_pct"] = round(100.0 * dep / lp_total, 2)
    except Exception as e:
        snap["error_lp"] = str(e)

    if not deep:
        return snap

    # --- token contract traits ---
    try:
        code = get_code(w3, token)
        snap["code_size"] = len(code)
        sels = scan_selectors(code)
        snap["sel_mint"] = "mint" in sels
        snap["sel_pause"] = "pause" in sels
        snap["sel_blacklist"] = "blacklist" in sels
        snap["sel_fee_setter"] = "fee_setter" in sels
        snap["sel_trading_toggle"] = "trading_toggle" in sels
        snap["sel_tx_limits"] = "tx_limits" in sels
        px = proxy_check(w3, token, code)
        snap["is_upgradeable_proxy"] = px["is_upgradeable_proxy"] or px["is_minimal_proxy"]
        has_owner, owner, renounced = token_owner(w3, token)
        snap["owner"] = owner
        snap["owner_renounced"] = renounced
    except Exception as e:
        snap["error_traits"] = str(e)

    # --- creator stake / history ---
    if creator:
        try:
            snap["creator_is_contract"] = len(get_code(w3, creator)) > 0
            snap["creator_tx_count"] = w3.eth.get_transaction_count(creator)
            if tinfo["total_supply"]:
                cbal = balance_of(w3, token, creator, block=block)
                snap["creator_token_pct"] = round(100.0 * cbal / tinfo["total_supply"], 2)
        except Exception:
            pass
        owner = snap.get("owner")
        if owner and owner not in DEAD_ADDRS and owner != creator and tinfo["total_supply"]:
            try:
                obal = balance_of(w3, token, owner, block=block)
                snap["owner_token_pct"] = round(100.0 * obal / tinfo["total_supply"], 2)
            except Exception:
                pass

    # --- explorer extras ---
    snap["verified"] = explorer_is_verified(cfg, token)
    if tinfo["total_supply"]:
        exclude = [pair, *DEAD_ADDRS, *(cfg.known_lockers or [])]
        top10, holders_seen = explorer_top_holders(cfg, token, tinfo["total_supply"], exclude)
        snap["top10_holders_pct"] = top10
        snap["holders_seen"] = holders_seen
    return snap


def find_pair_creator(w3, cfg, pair, creation_block):
    """The address that minted the first LP is the de-facto launch wallet."""
    try:
        logs = safe_get_logs(
            w3, pair, [TOPIC_MINT_V2], creation_block, creation_block + 2000,
            chunk=cfg.logs_chunk_size, delay=cfg.rpc_delay_sec,
        )
        if logs:
            return tx_sender(w3, logs[0]["transactionHash"])
    except RpcError:
        pass
    return None


# --------------------------------------------------------------------------
# Early trading stats + deltas (watch window)
# --------------------------------------------------------------------------


def swap_stats(w3, cfg, pair, token_is_token0, from_block, to_block, creator=None, max_tx_lookups=40):
    """Counts buys/sells from v2 Swap logs. A buy = token flowing out of the
    pool (amount(token)Out > 0)."""
    stats = {"swaps": 0, "buys": 0, "sells": 0, "unique_recipients": 0,
             "creator_sells": 0, "unique_buyers_sampled": None}
    try:
        logs = safe_get_logs(
            w3, pair, [TOPIC_SWAP_V2], from_block, to_block,
            chunk=cfg.logs_chunk_size, delay=cfg.rpc_delay_sec,
        )
    except RpcError:
        return stats
    from eth_abi import decode as abi_decode

    recipients = set()
    buy_txs = []
    for lg in logs:
        a0i, a1i, a0o, a1o = abi_decode(
            ["uint256", "uint256", "uint256", "uint256"], _hb(lg["data"])
        )
        token_out = a0o if token_is_token0 else a1o
        token_in = a0i if token_is_token0 else a1i
        to = topic_addr(lg["topics"][2])
        recipients.add(to)
        stats["swaps"] += 1
        if token_out > 0 and token_in == 0:
            stats["buys"] += 1
            buy_txs.append(lg["transactionHash"])
        elif token_in > 0:
            stats["sells"] += 1
            if creator and to == creator:
                stats["creator_sells"] += 1
    stats["unique_recipients"] = len(recipients)
    senders = set()
    for txh in buy_txs[:max_tx_lookups]:
        s = tx_sender(w3, txh)
        if s:
            senders.add(s)
    if buy_txs:
        stats["unique_buyers_sampled"] = len(senders)
    return stats


def checkpoint_delta(first, now):
    """Rug-relevant changes between the launch snapshot and a later one."""
    d = {}

    def pct_change(key):
        a, b = first.get(key), now.get(key)
        if a and b is not None and a > 0:
            return round(100.0 * (b - a) / a, 1)
        return None

    d["liquidity_change_pct"] = pct_change("liquidity_quote")
    d["fdv_change_pct"] = pct_change("fdv_quote")
    for key in ("lp_burned_pct", "lp_locked_pct", "lp_creator_pct", "creator_token_pct"):
        a, b = first.get(key), now.get(key)
        if a is not None and b is not None:
            d[key + "_delta"] = round(b - a, 2)
    return d
