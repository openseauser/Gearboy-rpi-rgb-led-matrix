"""Buy/sell round-trip simulation against the v2 router, without spending funds.

Tier 1: eth_simulateV1 (Geth >= 1.14.12 / recent Nitro) — simulates a full
        approve -> buy -> approve -> sell bundle from a fake trader whose quote
        balance is injected via state override. Measures real buy and sell tax.
Tier 2: plain eth_call with state overrides — can only answer "does the buy /
        sell call revert", taxes stay unknown.
Tier 3: nothing supported — everything reported unknown.

A token where the buy succeeds but every sell attempt reverts is flagged as a
suspected honeypot. Sell taxes are measured against the router's own
getAmountsOut quote taken at post-buy reserves, so fee-on-transfer shows up
as tax percentage.
"""

from eth_abi import decode as abi_decode
from eth_abi import encode as abi_encode

from .chain import (
    MAX_UINT,
    PROBE,
    RpcError,
    eth_call_raw,
    find_allowance_slot,
    find_balance_slot,
    rpc,
    selector,
    storage_key_for_allowance,
    storage_key_for_balance,
)

DEADLINE = 2**31
GAS = hex(6_000_000)

SIG_SWAP_FOT = (
    "swapExactTokensForTokensSupportingFeeOnTransferTokens(uint256,uint256,address[],address,uint256)"
)
SIG_AMOUNTS_OUT = "getAmountsOut(uint256,address[])"
SIG_APPROVE = "approve(address,uint256)"
SIG_BALANCE_OF = "balanceOf(address)"


def _calldata(sig, types, args):
    return "0x" + (selector(sig) + abi_encode(types, args)).hex()


def _call(to, data, value=0):
    c = {"from": PROBE, "to": to, "data": data, "gas": GAS}
    if value:
        c["value"] = hex(value)
    return c


def quote_amounts_out(w3, router, amount_in, path):
    data = _calldata(SIG_AMOUNTS_OUT, ["uint256", "address[]"], [amount_in, path])
    out = eth_call_raw(w3, router, data)
    return list(abi_decode(["uint256[]"], bytes.fromhex(out[2:]))[0])


def _pad32(value):
    return "0x" + hex(value)[2:].rjust(64, "0")


class SlotCache:
    """Balance/allowance slot discovery is a few dozen RPC calls per token, so
    cache per-token results for the lifetime of the process."""

    def __init__(self):
        self._bal = {}
        self._alw = {}

    def balance(self, w3, token):
        if token not in self._bal:
            self._bal[token] = find_balance_slot(w3, token)
        return self._bal[token]

    def allowance(self, w3, token, spender):
        key = (token, spender)
        if key not in self._alw:
            self._alw[key] = find_allowance_slot(w3, token, spender)
        return self._alw[key]


def _simulate_v1(w3, state_overrides, calls):
    """Returns list of {status:int, returnData:str, error:str|None} per call,
    or raises RpcError (including 'unsupported')."""
    payload = [
        {"blockStateCalls": [{"stateOverrides": state_overrides, "calls": calls}],
         "validation": False},
        "latest",
    ]
    result = rpc(w3, "eth_simulateV1", payload, retries=1)
    block = (result or [{}])[0]
    if len(block.get("calls", [])) != len(calls):
        raise RpcError("eth_simulateV1 returned an unexpected shape; treating as unsupported")
    out = []
    for c in block.get("calls", []):
        status = int(c.get("status", "0x0"), 16)
        err = None
        if status != 1:
            e = c.get("error") or {}
            err = e.get("message") if isinstance(e, dict) else str(e)
        out.append({"status": status, "returnData": c.get("returnData", "0x"), "error": err})
    return out


def _decode_uint(ret):
    if not ret or ret == "0x":
        return None
    return int(ret, 16)


def simulate_round_trip(w3, cfg, token, quote, quote_reserve, slots: SlotCache):
    """Simulate quote->token->quote. Returns a dict of findings."""
    res = {
        "method": None,
        "can_buy": None,
        "can_sell": None,
        "buy_tax_pct": None,
        "sell_tax_pct": None,
        "honeypot_suspected": None,
        "notes": [],
    }
    router = cfg.v2_router
    if not router:
        res["notes"].append("no v2_router configured; simulation skipped")
        return res

    buy_amt = max(10**6, int(quote_reserve * float(cfg.probe_quote_fraction)))
    try:
        expected_buy = quote_amounts_out(w3, router, buy_amt, [quote, token])[-1]
    except RpcError as e:
        res["notes"].append(f"getAmountsOut failed ({e}); pool may have no liquidity yet")
        expected_buy = None

    q_slot, q_layout = slots.balance(w3, quote)
    if q_slot is None:
        res["notes"].append("could not locate quote-token balance slot; simulation skipped")
        return res

    overrides = {
        PROBE: {"balance": hex(10**19)},
        quote: {"stateDiff": {storage_key_for_balance(PROBE, q_slot, q_layout): _pad32(buy_amt)}},
    }

    approve_q = _call(quote, _calldata(SIG_APPROVE, ["address", "uint256"], [router, MAX_UINT]))
    buy = _call(
        router,
        _calldata(
            SIG_SWAP_FOT,
            ["uint256", "uint256", "address[]", "address", "uint256"],
            [buy_amt, 0, [quote, token], PROBE, DEADLINE],
        ),
    )
    bal_t = _call(token, _calldata(SIG_BALANCE_OF, ["address"], [PROBE]))

    # ---- Tier 1: eth_simulateV1 ------------------------------------------
    try:
        phase_a = _simulate_v1(w3, overrides, [approve_q, buy, bal_t])
        res["method"] = "eth_simulateV1"
    except RpcError as e:
        phase_a = None
        if "not found" in str(e).lower() or "not supported" in str(e).lower() or "-32601" in str(e):
            res["notes"].append("RPC lacks eth_simulateV1; falling back to eth_call overrides")
        else:
            res["notes"].append(f"eth_simulateV1 error: {e}")

    if phase_a is not None:
        if phase_a[1]["status"] != 1:
            res["can_buy"] = False
            res["notes"].append(f"buy reverted: {phase_a[1]['error'] or 'no reason'}")
            return res
        res["can_buy"] = True
        received = _decode_uint(phase_a[2]["returnData"]) or 0
        if received == 0:
            res["can_sell"] = False
            res["honeypot_suspected"] = True
            res["notes"].append("buy 'succeeded' but zero tokens received")
            return res
        if expected_buy:
            res["buy_tax_pct"] = round(max(0.0, 1 - received / expected_buy) * 100, 2)

        for frac, label in ((2, "half"), (10, "10%")):
            sell_amt = received // frac
            if sell_amt == 0:
                continue
            approve_t = _call(
                token, _calldata(SIG_APPROVE, ["address", "uint256"], [router, MAX_UINT])
            )
            quote_sell = _call(
                router,
                _calldata(SIG_AMOUNTS_OUT, ["uint256", "address[]"], [sell_amt, [token, quote]]),
            )
            sell = _call(
                router,
                _calldata(
                    SIG_SWAP_FOT,
                    ["uint256", "uint256", "address[]", "address", "uint256"],
                    [sell_amt, 0, [token, quote], PROBE, DEADLINE],
                ),
            )
            bal_q = _call(quote, _calldata(SIG_BALANCE_OF, ["address"], [PROBE]))
            try:
                phase_b = _simulate_v1(
                    w3, overrides, [approve_q, buy, approve_t, quote_sell, sell, bal_q]
                )
            except RpcError as e:
                res["notes"].append(f"sell simulation errored: {e}")
                break
            if phase_b[4]["status"] == 1:
                res["can_sell"] = True
                if label != "half":
                    res["notes"].append("large sells revert; only smaller sells go through (max-tx style limit?)")
                final_q = _decode_uint(phase_b[5]["returnData"]) or 0
                expected_sell = None
                if phase_b[3]["status"] == 1 and phase_b[3]["returnData"] not in (None, "0x"):
                    amounts = abi_decode(
                        ["uint256[]"], bytes.fromhex(phase_b[3]["returnData"][2:])
                    )[0]
                    expected_sell = amounts[-1]
                if expected_sell:
                    res["sell_tax_pct"] = round(max(0.0, 1 - final_q / expected_sell) * 100, 2)
                break
            else:
                res["notes"].append(
                    f"sell of {label} reverted: {phase_b[4]['error'] or 'no reason'}"
                )
        if res["can_sell"] is None:
            res["can_sell"] = False
        res["honeypot_suspected"] = bool(res["can_buy"]) and res["can_sell"] is False
        return res

    # ---- Tier 2: single eth_call with overrides --------------------------
    qa_slot, qa_layout = slots.allowance(w3, quote, router)
    if qa_slot is None:
        res["notes"].append("quote allowance slot not found; buy check limited")
        res["method"] = res["method"] or "eth_call_override"
    else:
        ov = {
            PROBE: {"balance": hex(10**19)},
            quote: {
                "stateDiff": {
                    storage_key_for_balance(PROBE, q_slot, q_layout): _pad32(buy_amt),
                    storage_key_for_allowance(PROBE, router, qa_slot, qa_layout): _pad32(MAX_UINT),
                }
            },
        }
        try:
            eth_call_raw(w3, router, buy["data"], state_override=ov, sender=PROBE)
            res["can_buy"] = True
        except RpcError as e:
            res["can_buy"] = False
            res["notes"].append(f"buy call reverted: {e}")
        res["method"] = "eth_call_override"

    t_slot, t_layout = slots.balance(w3, token)
    ta_slot, ta_layout = (None, None)
    if t_slot is not None:
        ta_slot, ta_layout = slots.allowance(w3, token, router)
    if t_slot is None or ta_slot is None:
        res["notes"].append("token balance/allowance slot not found; sell check unavailable")
    else:
        try:
            token_reserve = quote_amounts_out(w3, router, buy_amt, [quote, token])[-1] * 3
        except RpcError:
            token_reserve = 10**21
        sell_amt = max(1, token_reserve)
        ov = {
            PROBE: {"balance": hex(10**19)},
            token: {
                "stateDiff": {
                    storage_key_for_balance(PROBE, t_slot, t_layout): _pad32(sell_amt * 2),
                    storage_key_for_allowance(PROBE, router, ta_slot, ta_layout): _pad32(MAX_UINT),
                }
            },
        }
        sell_data = _calldata(
            SIG_SWAP_FOT,
            ["uint256", "uint256", "address[]", "address", "uint256"],
            [sell_amt, 0, [token, quote], PROBE, DEADLINE],
        )
        try:
            eth_call_raw(w3, router, sell_data, state_override=ov, sender=PROBE)
            res["can_sell"] = True
        except RpcError as e:
            res["can_sell"] = False
            res["notes"].append(f"sell call reverted: {e}")
        res["notes"].append("taxes not measurable without eth_simulateV1")

    if res["can_buy"] is not None or res["can_sell"] is not None:
        res["honeypot_suspected"] = bool(res["can_buy"]) and res["can_sell"] is False
    return res
