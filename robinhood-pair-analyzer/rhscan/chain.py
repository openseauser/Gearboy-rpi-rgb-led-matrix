"""Low-level chain access: RPC helpers, minimal ABIs, log decoding, bytecode
inspection, and storage-slot discovery for balance/allowance overrides.

Everything here is plain EVM + Uniswap-v2/v3 conventions, so it works on any
EVM chain; Robinhood Chain (Arbitrum Orbit, chain id 4663) is just the default.
"""

import time

from eth_abi import decode as abi_decode
from eth_abi import encode as abi_encode
from web3 import Web3

# --------------------------------------------------------------------------
# Signatures / topics
# --------------------------------------------------------------------------

SIG_PAIR_CREATED = "PairCreated(address,address,address,uint256)"  # Uniswap v2
SIG_POOL_CREATED = "PoolCreated(address,address,uint24,int24,address)"  # Uniswap v3
SIG_SWAP_V2 = "Swap(address,uint256,uint256,uint256,uint256,address)"
SIG_TRANSFER = "Transfer(address,address,uint256)"
SIG_MINT_V2 = "Mint(address,uint256,uint256)"

ZERO = "0x0000000000000000000000000000000000000000"
DEAD = "0x000000000000000000000000000000000000dEaD"
PROBE = "0x1111111111111111111111111111111111111111"  # fake trader for simulations

MAX_UINT = 2**256 - 1


def topic(sig):
    return "0x" + Web3.keccak(text=sig).hex().replace("0x", "")


def selector(sig):
    return Web3.keccak(text=sig)[:4]


TOPIC_PAIR_CREATED = topic(SIG_PAIR_CREATED)
TOPIC_POOL_CREATED = topic(SIG_POOL_CREATED)
TOPIC_SWAP_V2 = topic(SIG_SWAP_V2)
TOPIC_TRANSFER = topic(SIG_TRANSFER)
TOPIC_MINT_V2 = topic(SIG_MINT_V2)

# Function selectors we scan deployed bytecode for. Presence of a selector in
# code is a strong (not perfect) hint the function exists in the dispatcher.
RISKY_SELECTOR_GROUPS = {
    "mint": ["mint(address,uint256)", "mint(uint256)"],
    "pause": ["pause()", "setPaused(bool)"],
    "blacklist": [
        "blacklist(address)",
        "blacklist(address,bool)",
        "setBlacklist(address,bool)",
        "addBlacklist(address)",
        "blacklistAddress(address,bool)",
        "setBots(address[],bool)",
        "addBotToBlacklist(address)",
    ],
    "fee_setter": [
        "setTaxes(uint256,uint256)",
        "setFee(uint256)",
        "setFees(uint256,uint256)",
        "setBuyTax(uint256)",
        "setSellTax(uint256)",
        "updateFees(uint256,uint256)",
        "setTaxFeePercent(uint256)",
    ],
    "trading_toggle": [
        "enableTrading()",
        "openTrading()",
        "setTradingEnabled(bool)",
        "setSwapEnabled(bool)",
    ],
    "tx_limits": [
        "setMaxTxAmount(uint256)",
        "setMaxWalletSize(uint256)",
        "setMaxWallet(uint256)",
        "removeLimits()",
    ],
}

# --------------------------------------------------------------------------
# RPC plumbing
# --------------------------------------------------------------------------


class RpcError(RuntimeError):
    pass


def get_w3(cfg):
    w3 = Web3(Web3.HTTPProvider(cfg.rpc_url, request_kwargs={"timeout": 30}))
    return w3


def rpc(w3, method, params, retries=3):
    """Raw JSON-RPC with small retry/backoff. Raises RpcError on node errors."""
    delay = 1.0
    for attempt in range(retries + 1):
        try:
            resp = w3.provider.make_request(method, params)
        except Exception as e:  # transport error
            if attempt == retries:
                raise RpcError(f"{method}: transport error: {e}")
            time.sleep(delay)
            delay *= 2
            continue
        if "error" in resp and resp["error"]:
            err = resp["error"]
            msg = err.get("message", str(err)) if isinstance(err, dict) else str(err)
            # rate limiting is worth retrying; anything else is not
            if attempt < retries and ("429" in msg or "rate" in msg.lower()):
                time.sleep(delay)
                delay *= 2
                continue
            raise RpcError(f"{method}: {msg}")
        return resp.get("result")
    raise RpcError(f"{method}: retries exhausted")


def eth_call_raw(w3, to, data, block="latest", state_override=None, sender=None, value=0):
    """eth_call via raw request so state overrides work uniformly across
    web3.py versions. Returns hex return-data or raises RpcError (reverts
    surface as RpcError)."""
    call = {"to": to, "data": data}
    if sender:
        call["from"] = sender
    if value:
        call["value"] = hex(value)
    params = [call, block if isinstance(block, str) else hex(block)]
    if state_override:
        params.append(state_override)
    return rpc(w3, "eth_call", params, retries=1)


def call_fn(w3, to, sig, args=(), ret_types=(), block="latest", state_override=None, sender=None):
    """Encode sig(args), eth_call, decode ret_types. Returns tuple of decoded
    values ((),) sized to ret_types, or single value if one ret type."""
    arg_types = sig[sig.index("(") + 1 : sig.rindex(")")]
    types = [t for t in arg_types.split(",") if t]
    data = "0x" + (selector(sig) + abi_encode(types, list(args))).hex()
    out = eth_call_raw(w3, to, data, block=block, state_override=state_override, sender=sender)
    if not ret_types:
        return None
    raw = bytes.fromhex(out[2:]) if out and out != "0x" else b""
    if not raw:
        raise RpcError(f"{sig} on {to}: empty return data")
    vals = abi_decode(list(ret_types), raw)
    return vals[0] if len(ret_types) == 1 else vals


def hexint(h):
    return int(h, 16) if isinstance(h, str) else int(h)


def _hb(x):
    """topics/data from web3 come as HexBytes or hex str depending on version."""
    if isinstance(x, (bytes, bytearray)):
        return bytes(x)
    return bytes.fromhex(str(x).replace("0x", ""))


def topic_addr(t):
    return Web3.to_checksum_address("0x" + _hb(t)[-20:].hex())


# --------------------------------------------------------------------------
# Logs
# --------------------------------------------------------------------------


def safe_get_logs(w3, address, topics, from_block, to_block, chunk=4000, delay=0.05):
    """Chunked eth_getLogs that halves the window on 'too many results' style
    errors. `address` may be None (topic-only scan), a str, or a list."""
    logs = []
    start = from_block
    while start <= to_block:
        end = min(start + chunk - 1, to_block)
        params = {"fromBlock": hex(start), "toBlock": hex(end), "topics": topics}
        if address:
            params["address"] = address
        try:
            batch = rpc(w3, "eth_getLogs", [params], retries=2)
        except RpcError as e:
            if chunk > 128:
                chunk = max(128, chunk // 4)
                continue
            raise RpcError(f"eth_getLogs failed even at chunk=128: {e}")
        logs.extend(batch or [])
        start = end + 1
        if delay:
            time.sleep(delay)
    return logs


def decode_pair_created(log):
    """Uniswap v2 PairCreated(token0 idx, token1 idx, pair, uint)."""
    data = _hb(log["data"])
    pair, _n = abi_decode(["address", "uint256"], data)
    return {
        "dex": "v2",
        "factory": Web3.to_checksum_address(log["address"]),
        "token0": topic_addr(log["topics"][1]),
        "token1": topic_addr(log["topics"][2]),
        "pair": Web3.to_checksum_address(pair),
        "block": hexint(log["blockNumber"]),
        "tx": log["transactionHash"] if isinstance(log["transactionHash"], str) else log["transactionHash"].hex(),
    }


def decode_pool_created(log):
    """Uniswap v3 PoolCreated(token0 idx, token1 idx, fee idx, tickSpacing, pool)."""
    data = _hb(log["data"])
    _tick, pool = abi_decode(["int24", "address"], data)
    return {
        "dex": "v3",
        "factory": Web3.to_checksum_address(log["address"]),
        "token0": topic_addr(log["topics"][1]),
        "token1": topic_addr(log["topics"][2]),
        "fee": int.from_bytes(_hb(log["topics"][3])[-4:], "big"),
        "pair": Web3.to_checksum_address(pool),
        "block": hexint(log["blockNumber"]),
        "tx": log["transactionHash"] if isinstance(log["transactionHash"], str) else log["transactionHash"].hex(),
    }


# --------------------------------------------------------------------------
# Token / pair reads
# --------------------------------------------------------------------------


def erc20_info(w3, token, block="latest"):
    info = {"address": token, "name": "?", "symbol": "?", "decimals": 18, "total_supply": None,
            "decimals_assumed": False}
    for key, sig, types in (
        ("name", "name()", ["string"]),
        ("symbol", "symbol()", ["string"]),
    ):
        try:
            info[key] = call_fn(w3, token, sig, ret_types=types, block=block)
        except Exception:
            # some old tokens return bytes32
            try:
                v = call_fn(w3, token, sig, ret_types=["bytes32"], block=block)
                info[key] = v.rstrip(b"\x00").decode("utf-8", "replace")
            except Exception:
                pass
    try:
        info["decimals"] = int(call_fn(w3, token, "decimals()", ret_types=["uint8"], block=block))
    except Exception:
        info["decimals_assumed"] = True
    try:
        info["total_supply"] = int(
            call_fn(w3, token, "totalSupply()", ret_types=["uint256"], block=block)
        )
    except Exception:
        pass
    return info


def balance_of(w3, token, holder, block="latest"):
    return int(call_fn(w3, token, "balanceOf(address)", [holder], ["uint256"], block=block))


def get_reserves(w3, pair, block="latest"):
    r0, r1, ts = call_fn(
        w3, pair, "getReserves()", ret_types=["uint112", "uint112", "uint32"], block=block
    )
    return int(r0), int(r1), int(ts)


def token_owner(w3, token):
    """Returns (has_owner_fn, owner_address_or_None, renounced)."""
    for sig in ("owner()", "getOwner()"):
        try:
            o = call_fn(w3, token, sig, ret_types=["address"])
            o = Web3.to_checksum_address(o)
            renounced = o in (ZERO, Web3.to_checksum_address(DEAD))
            return True, o, renounced
        except Exception:
            continue
    return False, None, True  # no owner function at all ~ nothing to abuse


# --------------------------------------------------------------------------
# Bytecode inspection
# --------------------------------------------------------------------------


def get_code(w3, addr, block="latest"):
    out = rpc(w3, "eth_getCode", [addr, block if isinstance(block, str) else hex(block)])
    return bytes.fromhex(out[2:]) if out and out != "0x" else b""


def scan_selectors(code, groups=RISKY_SELECTOR_GROUPS):
    """{group: [matched signatures]} by scanning for 4-byte selectors in code.
    Substring match — tiny false-positive chance from data sections, and
    obfuscated contracts can hide functions; treat as a hint, not proof."""
    found = {}
    for group, sigs in groups.items():
        hits = [s for s in sigs if selector(s) in code]
        if hits:
            found[group] = hits
    return found


EIP1967_IMPL_SLOT = hex(int.from_bytes(Web3.keccak(text="eip1967.proxy.implementation"), "big") - 1)


def proxy_check(w3, addr, code):
    """Returns dict(is_minimal_proxy, eip1967_impl, is_upgradeable_proxy)."""
    minimal = code.startswith(bytes.fromhex("363d3d373d3d3d363d73"))
    impl = None
    try:
        raw = rpc(w3, "eth_getStorageAt", [addr, EIP1967_IMPL_SLOT, "latest"])
        v = int(raw, 16)
        if v:
            impl = Web3.to_checksum_address("0x" + hex(v)[2:].rjust(40, "0")[-40:])
    except Exception:
        pass
    delegates = b"\xf4" in code  # DELEGATECALL opcode present (weak signal alone)
    return {
        "is_minimal_proxy": minimal,
        "eip1967_impl": impl,
        "is_upgradeable_proxy": impl is not None,
        "has_delegatecall": delegates,
    }


# --------------------------------------------------------------------------
# Storage slot discovery (for eth_call / eth_simulateV1 state overrides)
# --------------------------------------------------------------------------


def _slot_key_solidity(addr_key, slot):
    return Web3.keccak(abi_encode(["address", "uint256"], [addr_key, slot]))


def _slot_key_vyper(addr_key, slot):
    return Web3.keccak(abi_encode(["uint256", "address"], [slot, addr_key]))


_PROBE_VALUE = 0x0DE0B6B3A7640000_00000000_31337  # arbitrary distinctive value


def find_balance_slot(w3, token, max_slot=80):
    """Find balanceOf mapping slot by probing eth_call state overrides.
    Returns (slot, layout) with layout in {'solidity','vyper'} or (None, None)."""
    data = "0x" + (selector("balanceOf(address)") + abi_encode(["address"], [PROBE])).hex()
    for layout, keyfn in (("solidity", _slot_key_solidity), ("vyper", _slot_key_vyper)):
        for slot in range(max_slot):
            key = "0x" + keyfn(PROBE, slot).hex().replace("0x", "")
            ov = {token: {"stateDiff": {key: "0x" + hex(_PROBE_VALUE)[2:].rjust(64, "0")}}}
            try:
                out = eth_call_raw(w3, token, data, state_override=ov)
            except RpcError:
                continue
            if out and int(out, 16) == _PROBE_VALUE:
                return slot, layout
    return None, None


def find_allowance_slot(w3, token, spender, max_slot=80):
    """Find allowance mapping slot (owner => spender => amount). Returns
    (slot, layout) or (None, None)."""
    data = "0x" + (
        selector("allowance(address,address)") + abi_encode(["address", "address"], [PROBE, spender])
    ).hex()
    for layout in ("solidity", "vyper"):
        for slot in range(max_slot):
            if layout == "solidity":
                inner = Web3.keccak(abi_encode(["address", "uint256"], [PROBE, slot]))
                key = Web3.keccak(abi_encode(["address", "bytes32"], [spender, inner]))
            else:
                inner = Web3.keccak(abi_encode(["uint256", "address"], [slot, PROBE]))
                key = Web3.keccak(abi_encode(["bytes32", "address"], [inner, spender]))
            ov = {
                token: {
                    "stateDiff": {
                        "0x" + key.hex().replace("0x", ""): "0x" + hex(_PROBE_VALUE)[2:].rjust(64, "0")
                    }
                }
            }
            try:
                out = eth_call_raw(w3, token, data, state_override=ov)
            except RpcError:
                continue
            if out and int(out, 16) == _PROBE_VALUE:
                return slot, layout
    return None, None


def storage_key_for_balance(holder, slot, layout):
    fn = _slot_key_solidity if layout == "solidity" else _slot_key_vyper
    return "0x" + fn(holder, slot).hex().replace("0x", "")


def storage_key_for_allowance(owner, spender, slot, layout):
    if layout == "solidity":
        inner = Web3.keccak(abi_encode(["address", "uint256"], [owner, slot]))
        key = Web3.keccak(abi_encode(["address", "bytes32"], [spender, inner]))
    else:
        inner = Web3.keccak(abi_encode(["uint256", "address"], [slot, owner]))
        key = Web3.keccak(abi_encode(["bytes32", "address"], [inner, spender]))
    return "0x" + key.hex().replace("0x", "")


# --------------------------------------------------------------------------
# Misc
# --------------------------------------------------------------------------


def tx_sender(w3, tx_hash):
    try:
        tx = w3.eth.get_transaction(tx_hash)
        return Web3.to_checksum_address(tx["from"])
    except Exception:
        return None


def creation_block_binary_search(w3, addr, latest=None):
    """Find first block where `addr` has code. Needs an archive-capable RPC for
    old blocks; returns None if historical state is unavailable."""
    latest = latest or w3.eth.block_number
    try:
        if not get_code(w3, addr, latest):
            return None
        lo, hi = 1, latest
        while lo < hi:
            mid = (lo + hi) // 2
            if get_code(w3, addr, mid):
                hi = mid
            else:
                lo = mid + 1
        return lo
    except RpcError:
        return None


def to_jsonable(x):
    if isinstance(x, dict):
        return {k: to_jsonable(v) for k, v in x.items()}
    if isinstance(x, (list, tuple)):
        return [to_jsonable(v) for v in x]
    if isinstance(x, (bytes, bytearray)):
        return "0x" + bytes(x).hex()
    if isinstance(x, float) and x != x:  # NaN
        return None
    return x
