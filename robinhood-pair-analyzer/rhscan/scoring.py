"""Scoring: hard rug flags + weighted launch-quality signals.

The default thresholds are sensible heuristics; running profile_winners.py
produces a profile.json whose winner-percentiles recalibrate the numeric
thresholds (e.g. "winners on this chain launched with >= X liquidity").

This is a RISK SCREEN, not a price predictor: a high score means "no obvious
scam mechanics and a launch shaped like past winners", nothing more.
"""

NUMERIC_FEATURES = [
    "liquidity_usd",
    "fdv_usd",
    "pct_supply_in_pool",
    "lp_safe_pct",
    "creator_token_pct",
    "top10_holders_pct",
    "creator_tx_count",
    "buy_tax_pct",
    "sell_tax_pct",
]
BOOL_FEATURES = [
    "owner_renounced",
    "verified",
    "sel_mint",
    "sel_blacklist",
    "sel_pause",
    "sel_fee_setter",
    "sel_trading_toggle",
    "is_upgradeable_proxy",
    "creator_is_contract",
    "can_sell",
]


def lp_safe_pct(snap):
    b = snap.get("lp_burned_pct")
    l = snap.get("lp_locked_pct")
    if b is None and l is None:
        return None
    return round((b or 0) + (l or 0), 2)


def _profile_threshold(profile, feature, pct, default):
    try:
        v = profile["features"][feature][pct]
        return v if v is not None else default
    except (KeyError, TypeError):
        return default


def score_pair(snap, sim, cfg, profile=None):
    """Returns {"score", "verdict", "hard_flags", "positives", "negatives",
    "unknowns"}."""
    hard, pos, neg, unk = [], [], [], []
    sim = sim or {}
    renounced = snap.get("owner_renounced")
    safe_lp = lp_safe_pct(snap)

    # ---------------- hard flags ----------------
    if sim.get("can_buy") is False:
        hard.append("buy simulation reverts — token not tradeable")
    if sim.get("honeypot_suspected"):
        hard.append("sell simulation reverts — suspected HONEYPOT")
    for side in ("buy", "sell"):
        t = sim.get(f"{side}_tax_pct")
        if t is not None and t > 25:
            hard.append(f"{side} tax {t:.0f}% — extractive")
    if snap.get("is_upgradeable_proxy"):
        hard.append("token is an upgradeable proxy — logic can be swapped at any time")
    if snap.get("sel_mint") and renounced is False:
        hard.append("mint() present with active owner — supply can be inflated")
    if snap.get("sel_blacklist") and renounced is False and cfg.blacklist_is_hard_flag:
        hard.append("blacklist function with active owner — sells can be blocked per-wallet")
    lp_creator = snap.get("lp_creator_pct")
    if lp_creator is not None and lp_creator > 50 and (safe_lp or 0) < 30:
        hard.append(f"creator holds {lp_creator:.0f}% of LP unburned/unlocked — can pull liquidity")

    # ---------------- soft components ----------------
    score = 0.0

    # LP custody (25)
    if safe_lp is None:
        unk.append("LP custody")
    else:
        if safe_lp >= 95:
            score += 25
            pos.append(f"LP {safe_lp:.0f}% burned/locked")
        elif safe_lp >= 70:
            score += 18
            pos.append(f"LP {safe_lp:.0f}% burned/locked")
        elif safe_lp >= 40:
            score += 8
            neg.append(f"only {safe_lp:.0f}% of LP burned/locked")
        else:
            neg.append(f"LP unprotected ({safe_lp:.0f}% burned/locked)")
        if lp_creator is not None and lp_creator > 20 and safe_lp < 70:
            score = max(0, score - 10)
            neg.append(f"creator holds {lp_creator:.0f}% of LP")

    # ownership (10)
    if renounced is None:
        unk.append("ownership")
    elif renounced:
        score += 10
        pos.append("ownership renounced / no owner")
    else:
        neg.append(f"owner active ({(snap.get('owner') or '')[:10]}…)")

    # bytecode traits (10)
    if "error_traits" in snap:
        unk.append("bytecode traits")
    else:
        risky = [g for g in ("sel_mint", "sel_blacklist", "sel_pause") if snap.get(g)]
        if not risky:
            score += 10
            pos.append("no mint/blacklist/pause selectors")
        elif renounced:
            score += 7
            neg.append(f"risky selectors present but owner renounced: {', '.join(r[4:] for r in risky)}")
        else:
            neg.append(f"risky selectors with active owner: {', '.join(r[4:] for r in risky)}")
        if snap.get("sel_fee_setter") and not renounced:
            neg.append("fee-setter functions with active owner (taxes can change)")

    # taxes (12)
    bt, st = sim.get("buy_tax_pct"), sim.get("sell_tax_pct")
    if bt is None or st is None:
        if sim.get("can_sell") is True:
            score += 5
            pos.append("sell path executes (taxes unmeasured)")
        else:
            unk.append("buy/sell taxes")
    else:
        if bt <= 5 and st <= 5:
            score += 12
            pos.append(f"low taxes (buy {bt:.1f}% / sell {st:.1f}%)")
        elif bt <= 10 and st <= 10:
            score += 6
            neg.append(f"moderate taxes (buy {bt:.1f}% / sell {st:.1f}%)")
        else:
            neg.append(f"high taxes (buy {bt:.1f}% / sell {st:.1f}%)")

    # initial liquidity (12)
    liq = snap.get("liquidity_usd")
    liq_good = _profile_threshold(profile, "liquidity_usd", "winner_p25", cfg.min_initial_liquidity_usd * 5)
    liq_ok = _profile_threshold(profile, "liquidity_usd", "winner_p10", cfg.min_initial_liquidity_usd)
    if liq is None:
        unk.append("liquidity USD value")
    elif liq >= liq_good:
        score += 12
        pos.append(f"liquidity ${liq:,.0f}")
    elif liq >= liq_ok:
        score += 7
        pos.append(f"liquidity ${liq:,.0f} (modest)")
    else:
        neg.append(f"thin liquidity ${liq:,.0f}")

    # supply in pool (10)
    sip = snap.get("pct_supply_in_pool")
    sip_good = _profile_threshold(profile, "pct_supply_in_pool", "winner_p25", 85)
    if sip is None:
        unk.append("% supply in pool")
    elif sip >= sip_good:
        score += 10
        pos.append(f"{sip:.0f}% of supply in pool")
    elif sip >= 60:
        score += 6
    elif sip >= 40:
        score += 3
        neg.append(f"only {sip:.0f}% of supply in pool")
    else:
        neg.append(f"only {sip:.0f}% of supply in pool — big overhang outside the pool")

    # creator stake (8)
    cstake = snap.get("creator_token_pct")
    if cstake is None:
        unk.append("creator token stake")
    elif cstake <= 2:
        score += 8
        pos.append(f"creator holds {cstake:.1f}% of supply")
    elif cstake <= 5:
        score += 6
    elif cstake <= 10:
        score += 3
        neg.append(f"creator holds {cstake:.1f}% of supply")
    else:
        neg.append(f"creator holds {cstake:.1f}% of supply — dump risk")

    # verified source (5)
    v = snap.get("verified")
    if v is None:
        unk.append("source verification")
    elif v:
        score += 5
        pos.append("source verified")
    else:
        neg.append("source not verified")

    # holder concentration (8)
    top10 = snap.get("top10_holders_pct")
    top10_good = _profile_threshold(profile, "top10_holders_pct", "winner_p50", 25)
    if top10 is None:
        unk.append("holder concentration")
    elif top10 <= top10_good:
        score += 8
        pos.append(f"top-10 holders {top10:.0f}% of supply")
    elif top10 <= 40:
        score += 4
    else:
        neg.append(f"top-10 holders {top10:.0f}% of supply — concentrated")

    score = round(score)
    if hard:
        score = min(score, 10)
        verdict = "AVOID"
    elif score >= 70:
        verdict = "PROMISING"
    elif score >= 45:
        verdict = "WATCH"
    elif score >= 25:
        verdict = "CAUTION"
    else:
        verdict = "WEAK"

    return {
        "score": score,
        "verdict": verdict,
        "hard_flags": hard,
        "positives": pos,
        "negatives": neg,
        "unknowns": unk,
    }


def explain(result, indent="    "):
    lines = []
    for f in result["hard_flags"]:
        lines.append(f"{indent}✗✗ {f}")
    for p in result["positives"]:
        lines.append(f"{indent}+ {p}")
    for n in result["negatives"]:
        lines.append(f"{indent}- {n}")
    if result["unknowns"]:
        lines.append(f"{indent}? unknown: {', '.join(result['unknowns'])}")
    return "\n".join(lines)
