# Robinhood Chain pair analyzer

Watches Robinhood Chain (chain id 4663, Arbitrum Orbit, ETH gas) for new DEX
pairs, screens each launch for rug/scam mechanics, scores it 0–100, and keeps
watching it through its first hour for liquidity pulls and creator dumps. A
second workflow profiles past winners **against past rugs** and recalibrates
the scoring to what actually distinguishes them on this chain.

> **What this is and isn't.** This is a *risk screen*: a high score means "no
> obvious scam mechanics, launch shaped like past winners". It is **not** a
> price predictor and not financial advice. Most new memecoins go to zero
> regardless of how clean the launch looks, honeypot checks can be evaded by
> sufficiently devious contracts, and a creator with off-chain distribution
> (CEX-funded wallets, "community" multisends) can rug in ways no launch
> snapshot reveals. Size positions accordingly.

## Install

```bash
cd robinhood-pair-analyzer
pip install -r requirements.txt
cp config.example.json config.json
```

## 1. Verify the contract addresses

`config.example.json` ships with the Uniswap mainnet deployment on Robinhood
Chain (chain 4663), taken from Uniswap's official `sdk-core` address maps
(`V2_FACTORY_ADDRESSES`, `V2_ROUTER_ADDRESSES`, `WETH9` in
https://github.com/Uniswap/sdks — `sdks/sdk-core/src/addresses.ts`):

| contract | address |
|---|---|
| Uniswap v2 factory | `0x8bcEaA40B9AcdfAedF85AdF4FF01F5Ad6517937f` |
| Uniswap v2 Router02 | `0x89e5DB8B5aA49aA85AC63f691524311AEB649eba` |
| Uniswap v3 factory | `0x1f7d7550B1b028f7571E69A784071F0205FD2EfA` |
| WETH (wrapped native) | `0x0Bd7D308f8E1639FAb988df18A8011f41EAcAD73` |

Cross-check before trusting them with attention (addresses drift; other DEXes
may launch):

```bash
python monitor.py --discover
```

This scans recent blocks for `PairCreated` / `PoolCreated` events with **no
address filter** and prints every factory actually emitting pairs, plus the
most common pair-side token — the v2 factory and WETH above should top those
lists. If discover shows a busier factory, add it. Also worth a glance:
Robinhood's contract docs (https://docs.robinhood.com/chain/contracts) and
the explorer (https://robinhoodchain.blockscout.com — the router should be
verified as `UniswapV2Router02`).

Many launches on Robinhood Chain pair against tokenized stocks
(long.xyz / Bankr style); add those you care about to `quote_tokens` (with a
rough USD price, or 0 to skip USD figures), otherwise stock-paired launches
are reported as "unknown quote" and skipped.

If you use a locker service (Team Finance, UNCX, …), put its Robinhood Chain
contract addresses in `known_lockers` so locked LP counts as safe instead of
"creator-held".

## 2. Monitor

```bash
python monitor.py
```

For each new v2 pair against a known quote you get a report like:

```
[12:03:41] NEW PAIR  CCAT2/WETH  0xPair…
    token: Cash Cat 2 (CCAT2)  0xToken…
    liquidity: 6.4 WETH ($26,000)   FDV $88,000   supply in pool: 93.1%
    LP: burned 100.0% | locked 0.0% | creator 0.0%
    sim: buy ✓ tax 0.9%   sell ✓ tax 0.9%   [eth_simulateV1]
    score: 82/100  PROMISING
    + LP 100% burned/locked
    + ownership renounced / no owner
    ...
```

The pair is then re-checked at `watch_checkpoints_min` (default 2/5/15/30/60
minutes): liquidity change, FDV, buy/sell counts, unique buyers, creator
sells. A liquidity drop past `watch_liquidity_drop_alert_pct` or a creator
dump raises a 🚨 alert (console + optional `webhook_url`, Discord/Slack
compatible). Every snapshot appends to `data/pairs.jsonl` — that file becomes
your labeled dataset after a few days of running.

## 3. Learn from past winners (and rugs)

Your instinct — "look at a dozen or two tokens that hit multimillion caps and
find commonalities" — has a trap: **survivorship bias**. Nearly every rug also
launches with burned LP and renounced ownership, because scammers copy the
winning checklist. A trait common to winners is only a signal if it's *rare
among rugs*. So profile both groups:

```bash
# find candidates automatically: everything launched in the last 2 weeks,
# labeled by where it is now (>= $2M FDV vs drained pool)
python backfill.py --days 14

# review data/winners.txt and data/dead.txt by hand, then:
python profile_winners.py --winners data/winners.txt --rugs data/dead.txt
```

You can also hand-write `winners.txt` from a screener (one token or pair
address per line) — e.g. DEX Screener filtered to Robinhood Chain, sorted by
market cap. The profiler prints each feature's distribution for winners vs
rugs and a **separation ranking** — the features that genuinely discriminate.
It writes `data/profile.json`; point `profile_file` at it in `config.json`
and the monitor's numeric thresholds (liquidity, supply-in-pool, holder
concentration) recalibrate to this chain's actual winner percentiles.

`--at-launch` snapshots each pair at its creation block instead of today —
much better data, but needs an archive-capable RPC (public endpoints often
prune old state; it falls back to current state per pair and says so).

## Large early liquidity adds

```bash
python liquidity_watch.py                # live alerts, runs fine next to monitor.py
python liquidity_watch.py --days 7       # history: big early adds + how each turned out
```

Watches chain-wide v2 `Mint` events (every liquidity add emits one), keeps
only pairs belonging to your configured factories, and alerts when a single
add ≥ `--min-usd` (default $25k) lands in a pair ≤ `--max-age-hours` old
(default 12). Each alert shows the add's share of the pool and who received
the LP tokens (burned on arrival ✓ / known locker ✓ / a wallet that can pull
it). History mode ranks past early adds and fetches each pair's current
liquidity/FDV so you can see whether big-money launches on this chain
actually survive.

Mind the FDV trap this script exists to expose: a coin "starting at a few
million" is a few-million **FDV**, which needs almost no real money if most
supply sits outside the pool — alerts flag when FDV is >20× actual
liquidity. Add size measures the launcher's committed budget (or a
launchpad graduation), not safety; cross-check the pair in monitor.py
before acting.

## How scoring works

**Hard flags** force `AVOID` (score capped at 10):
sell simulation reverts (honeypot) · buy reverts · buy/sell tax > 25% ·
upgradeable proxy · `mint()` with active owner · blacklist function with
active owner (config: `blacklist_is_hard_flag`) · creator holds > 50% of LP
unburned/unlocked.

**Weighted signals** (defaults, recalibrated by a profile):

| signal | weight |
|---|---|
| LP burned/locked % | 25 |
| measured buy+sell tax ≤ 5% | 12 |
| initial liquidity (USD) | 12 |
| % of token supply in the pool | 10 |
| ownership renounced | 10 |
| no mint/blacklist/pause selectors | 10 |
| creator's token stake ≤ 2% | 8 |
| top-10 holder concentration | 8 |
| verified source | 5 |

Unknown values score zero and are listed as `? unknown:` so you can see the
coverage. Verdicts: ≥70 PROMISING · ≥45 WATCH · ≥25 CAUTION · else WEAK.

The buy/sell simulation funds a fake trader via `eth_call` state overrides
and runs approve→buy→approve→sell through the v2 router. With
`eth_simulateV1` (recent Geth/Nitro nodes) it measures real taxes; without
it, it can still answer "does the sell revert", and taxes stay unknown.

## Limitations worth knowing

- **Launchpad tokens appear late.** Bonding-curve launches (pump.fun-style)
  only emit `PairCreated` when they graduate to the DEX — the monitor sees
  them at graduation, not at curve start.
- **v3 pools get discovery only** (token + fee tier logged); deep analysis
  (LP custody, sim) is v2-only, which is where most raw launches happen.
- **Selectors can be hidden.** Bytecode scanning catches the common toolkit
  contracts, not deliberately obfuscated ones — that's what the sell
  simulation is for, and even that can be beaten by time/state-gated traps.
- **An active owner can change the rules later.** Clean taxes at launch mean
  nothing if fee-setters remain owned — that's why renounce carries weight.
- **Slow rugs are out of scope.** The watch window catches the first hour;
  a dev who dumps 2% a day for a month will not trip it.
- **Public RPC limits.** Big backfills are thousands of calls; tune
  `logs_chunk_size` / `rpc_delay_sec`, or use a dedicated RPC provider.

## Files

```
monitor.py          live monitor + --discover
liquidity_watch.py  alerts on large liquidity adds into young pairs (+ history mode)
backfill.py         historical launch scan → outcome-labeled dataset
profile_winners.py  winners-vs-rugs feature profile → profile.json
selftest.py         offline checks (run after any edit)
rhscan/             chain access, simulation, features, scoring, config
data/               state, pairs.jsonl, winners/dead lists, profile.json
```

## Sources

- Robinhood Chain mainnet announcement (Arbitrum): https://blog.arbitrum.io/robinhood-chain-mainnet/
- Uniswap on Robinhood Chain: https://blog.uniswap.org/robinhood-chain-is-live
- Network details (chain id 4663, RPC): https://www.quicknode.com/builders-guide/tools/robinhood-chain-public-rpc-by-robinhood-markets
- Memecoin scene overview: https://www.coingecko.com/learn/robinhood-chain-built-for-rwa-loved-for-memes
- Explorer: https://robinhoodchain.blockscout.com
