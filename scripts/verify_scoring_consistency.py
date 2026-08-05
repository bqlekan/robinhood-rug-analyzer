"""Live cross-check: does /scan agree with /analyze on the same token?

Runs the real pipeline against the real chain (no stubs), then re-analyzes the
top rows through the deep path and diffs every field both surfaces publish.

Surfaces in scope
  Dashboard + Ranked Scanner  -> both consume POST /scan (same RankedToken objects,
                                 different sort only), so scan IS both of them.
  Analyze Token               -> POST /analyze (deep path).
  Smart Wallets               -> renders symbol + address only, and clicking a token
                                 calls the same /analyze. No independent scoring path,
                                 so nothing to diff.

Score rules
  verified rows (scores_estimated=False) -> must match EXACTLY
  estimated rows (scores_estimated=True) -> may differ, but only conservatively
                                            (estimate risk >= deep risk)

Market-data rules
  Both surfaces read the same DexScreener pair, so name/symbol/price/liquidity/
  market cap/volume/age must match on BOTH row types. A mismatch there is a
  provenance bug, not an estimation gap.
"""

import asyncio
import sys

from app.services.rug_analyzer import analyze_token_contract, scan_and_rank

TOP_N = 3
# Market data is read from the same source by both paths -> must always agree.
FLOAT_TOL = 0.01


def _get(obj, *path):
    """Walk an attribute path, tolerating None at any level."""
    for p in path:
        if obj is None:
            return None
        obj = getattr(obj, p, None)
    return obj


def _close(a, b) -> bool:
    if a is None and b is None:
        return True
    if a is None or b is None:
        return False
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        if a == b:
            return True
        scale = max(abs(a), abs(b), 1.0)
        return abs(a - b) / scale < FLOAT_TOL
    return a == b


async def main() -> int:
    print("=== Stage 1: scan (Dashboard + Ranked Scanner source) ===", flush=True)
    scan = await scan_and_rank(page=1, page_size=10)
    rows = scan.ranked_tokens
    print(f"ranked={len(rows)} total_ranked={scan.total_ranked}")
    est = sum(1 for r in rows if r.scores_estimated)
    print(f"estimated={est} verified={len(rows) - est}")
    diag = scan.diagnostics
    if diag is not None:
        print(
            f"deep_analyzed={getattr(diag, 'deep_analyzed', '?')} "
            f"light_scored={getattr(diag, 'light_scored', '?')} "
            f"deep_duration_ms={getattr(diag, 'deep_analysis_duration_ms', '?')}"
        )
    print()

    for r in rows:
        mark = "*" if r.scores_estimated else " "
        print(
            f"  {mark} {(r.symbol or '?'):<8} risk={r.risk_score:<4}"
            f"({(r.risk_level or '?'):<8}) alpha={str(r.alpha_score):<5}"
            f" qual={(r.qualification_level or '?'):<12} {r.contract_address}"
        )

    print("\n=== Stage 2: re-analyze top rows through /analyze ===", flush=True)
    failures = []
    for r in rows[:TOP_N]:
        try:
            deep = await analyze_token_contract(r.contract_address, include_lore=False)
        except Exception as exc:  # noqa: BLE001 - report and keep sweeping
            failures.append(f"{r.symbol}: analyze raised {exc!r}")
            print(f"\n  {r.symbol}: ANALYZE FAILED: {exc!r}")
            continue

        md = deep.market_data
        d_risk = deep.analysis.risk_score

        print(f"\n  {r.symbol or '?'} ({r.contract_address})  estimated={r.scores_estimated}")

        # --- fields both surfaces publish: must agree regardless of tier ---
        pairs = [
            ("name",         r.name,            md.base_token_name if md else None),
            ("symbol",       r.symbol,          md.base_token_symbol if md else None),
            ("price_usd",    r.price_usd,       _get(md, "price_usd")),
            ("liquidity",    r.liquidity_usd,   _get(md, "liquidity", "usd")),
            ("market_cap",   r.market_cap,      _get(md, "market_cap")),
            ("fdv",          r.fdv,             _get(md, "fdv")),
            ("volume_h24",   r.volume_h24,      _get(md, "volume", "h24")),
            ("age_days",     r.age_days,        _get(deep, "token_age", "age_days")),
            ("holders",      r.holder_count,    _get(deep, "holders", "holder_count")),
        ]
        for label, scan_v, deep_v in pairs:
            ok = _close(scan_v, deep_v)
            flag = "  " if ok else "!!"
            print(f"    {flag} {label:<12} scan={str(scan_v):<22} deep={str(deep_v)}")
            if not ok:
                failures.append(
                    f"{r.symbol}: {label} differs — scan={scan_v!r} deep={deep_v!r}"
                )

        # --- analyze-only fields: no scan counterpart, print for eyeball ---
        print(f"       launchpad    {_get(deep, 'launchpad', 'name')} "
              f"(confidence={_get(deep, 'launchpad', 'confidence')})")
        print(f"       sellability  honeypot={_get(deep, 'honeypot', 'status')} "
              f"lock={_get(deep, 'liquidity_lock', 'status')}")

        # --- scores ---
        print(f"    -- risk  scan={r.risk_score} deep={d_risk}")
        print(f"    -- alpha scan={r.alpha_score} deep={deep.alpha_score}")

        if not r.scores_estimated:
            if r.risk_score != d_risk:
                failures.append(
                    f"{r.symbol}: VERIFIED row disagrees — scan risk {r.risk_score} != deep {d_risk}"
                )
            elif r.alpha_score != deep.alpha_score:
                failures.append(
                    f"{r.symbol}: VERIFIED row alpha differs — scan {r.alpha_score} != deep {deep.alpha_score}"
                )
            else:
                print("       -> OK: verified row matches exactly")
        else:
            # The NVDA symptom ran in the optimistic direction; that is the fatal one.
            if r.risk_score < d_risk:
                failures.append(
                    f"{r.symbol}: estimate OPTIMISTIC — scan risk {r.risk_score} < deep {d_risk}"
                )
            else:
                print("       -> OK: estimate is conservative (>= deep risk)")

        if r.risk_score == 0 and d_risk >= 70:
            failures.append(f"{r.symbol}: NVDA SYMPTOM — scan risk 0 while deep risk {d_risk}")

    print("\n=== Result ===")
    if failures:
        for f in failures:
            print(f"  FAIL: {f}")
        return 1
    print("  PASS: no surface disagreed")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
