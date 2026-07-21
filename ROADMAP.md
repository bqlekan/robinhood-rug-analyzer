# Robinhood Rug Analyzer — Implementation Roadmap

_Last updated: 2026-07-16. Reflects the codebase after Phases 1–3 (address validation,
frontend XSS hardening, shared HTTP client + scan efficiency, smart-wallet honesty fix)._

This roadmap groups all remaining work into dependency-ordered milestones, highest ROI
first. Effort is Small (<1 day), Medium (1–3 days), or Large (1 week+). Detection Δ is the
expected lift in catching real rugs / avoiding false calls.

> **Architecture reference:** for the system *as implemented* (subsystems, data flow,
> persistence, event pipeline, KOL Intelligence, and extension guides), see
> [`docs/ARCHITECTURE.md`](./docs/ARCHITECTURE.md) and its companions
> [`docs/ARCHITECTURE_DIAGRAMS.md`](./docs/ARCHITECTURE_DIAGRAMS.md) and
> [`docs/DATA_FLOW.md`](./docs/DATA_FLOW.md). Where this roadmap and the code diverge,
> the architecture doc is authoritative.

---

## Current Project Health

**Overall: stable, correct, and honest — but shallow.** The app reads metadata and history
well; it does not yet read contract *state* or simulate *behavior*, which is where most real
rugs live.

**Strengths**
- Clean layering: routes → orchestrator (`rug_analyzer`) → pure analyzers + typed models.
- Explainable scoring: every point traces to a named, categorized `RiskSignal`.
- Defensive I/O: every external call degrades to `None`/`[]` and never hard-fails analysis.
- Honest framing: heuristic/proxy limitations surfaced in models, API, and UI.
- Test suite green (47 passing), pure functions well covered.

**Recently resolved**
- Address validation at the `/analyze` boundary and the shared `analyze_token_contract` entry.
- Frontend XSS closed (`esc()` + `safeUrl()` across all renderers).
- One shared, connection-capped `httpx.AsyncClient`; duplicate transfer fetch eliminated.
- Smart-wallet dead path documented in UI + code instead of silently empty.

**Known weaknesses / debt still open**
- No behavior analysis: `rpc_url` is configured but never called — no honeypot sim, no
  privilege reads.
- Holder/cluster analysis uses a single ~50-row sampled page, not the full holder set.
- No caching layer; the scanner re-fetches identical data every run.
- `/scan` still runs the full per-token analysis (connection cap only; no lightweight tier).
- Dead/again-noted debt: contract-creation age branch is inert (`rug_analyzer.py:201/204`),
  unused `_member_key` (`analyzers.py:216`), duplicate `lp_addr` assignment
  (`rug_analyzer.py:215/267`), empty registry stubs, dead `*SCAN_API_KEY` env vars in
  `render.yaml`/`.env.example`.
- Smart-wallet feature inert by design until cross-token survival is wired in.

**Biggest single opportunity:** build the RPC layer once (honeypot simulation + privilege
reads); it is the difference between a metadata screen and a best-in-class rug detector.

## Milestones

Ordered by dependency and ROI. Enablers first — later milestones add requests, so caching
and scan tiering must land before the request-heavy detection work.

---

### M1 — HTTP response caching (TTL) ✅ COMPLETE

- **Goal:** Cache near-static external reads (token info, verified source, deployer history,
  DexScreener pairs) behind a small TTL layer.
- **Why it matters:** No caching exists today; the scanner re-fetches identical data every
  run. Every downstream detection milestone adds outbound calls — caching is the multiplier
  that keeps them within the free API budget.
- **Files/modules:** `app/services/http.py` (or new `app/services/cache.py`),
  `blockscout_client.py`, `dexscreener_client.py`.
- **Dependencies:** none. Builds on the shared client from Phase 2.
- **Effort:** Small–Medium · **Risk:** Low (stale data mitigated by short TTL).
- **Expected improvement:** Large cut in scan request volume; enables deeper per-token work.
  Detection Δ: indirect (High enabler).
- **Acceptance criteria:**
  - Repeated reads of the same address within TTL hit cache, not network.
  - TTL configurable; cache is per-process and bounded (no unbounded growth).
  - Cache never turns a failed fetch into a poisoned success.
- **Suggested tests:** cache hit/miss within/after TTL; error responses not cached;
  bounded-size eviction.
- **As built:** `app/services/cache.py` (`TTLCache` + `cached_call`), 300s TTL, config
  knobs in `config.py`. Cached ONLY `get_smart_contract` and `get_address_info`
  (immutable source + creation facts). Token info, DexScreener pairs, holders, and
  transfers are deliberately NOT cached — they feed live market/holder scoring signals.

**Blocker to solve first:** none — do this first.

---

### M2 — Scan tiering / lightweight pre-scan ✅ COMPLETE

- **Goal:** A cheap first-pass scorer (age + liquidity + concentration from data already
  fetched) that ranks all candidates, escalating to full analysis only on demand / top-N.
- **Why it matters:** `/scan` still runs the full ~40–60-call per-token analysis; the Phase 2
  connection cap throttles but does not reduce total work. Tiering makes scan-of-50 realistic
  and is a prerequisite for the request-heavy funder-graph and simulation milestones.
- **Files/modules:** `rug_analyzer.py` (`scan_and_rank`), possibly `scoring.py` (a
  `score_token_light` variant), `routes.py`, `models/token.py` (RankedToken already exists).
- **Dependencies:** M1 (caching) strongly recommended first.
- **Effort:** Medium · **Risk:** Medium (must not change single-token `/analyze` output).
- **Expected improvement:** Order-of-magnitude fewer calls per scan; scan latency down sharply.
  Detection Δ: indirect (scale/UX); light tier must stay conservative to avoid new FNs.
- **Acceptance criteria:**
  - Scan returns ranked list using the light scorer without per-token deep fetches.
  - Deep analysis still available per token and unchanged.
  - A global concurrency semaphore bounds in-flight deep analyses.
- **Suggested tests:** light scorer ranking on mocked token lists; semaphore caps concurrency;
  `/analyze` output byte-identical to pre-change.

**Blocker to solve first:** M1, so escalated deep analyses hit warm cache.

- **As built:** light tier uses only `holders_count` from `list_tokens` (no extra requests).
  Promote-on-uncertainty: a token is skipped only when confidently safe (known holder count
  ≥ `scan_established_holder_floor` AND light score < `scan_light_promote_threshold`);
  everything else escalates. `asyncio.Semaphore` bounds concurrent deep analyses.
  `score_token_light` in `scoring.py`; policy in `scan_and_rank`. Tests in
  `tests/test_scan_tiering.py`.

### M3 — Real token age from contract creation ✅ COMPLETE

> **As built:** new cached `blockscout_client.get_transaction_timestamp` reads the
> creation tx's immutable timestamp; `analyze_token_contract` calls it only when no
> DexScreener pair timestamp exists, so the pair path is unchanged and pre-liquidity
> tokens now get real age. Falls back to "unknown" when no creation tx / timestamp.

- **Goal:** Populate `contract_created_iso` from the creation-tx timestamp so age works when
  DexScreener has no pair.
- **Why it matters:** The contract-creation branch in `analyze_token_contract` is dead
  (`contract_created_iso` is set then forced to `None`), so any token without a DEX pair is
  always "unknown age" (+5). Pre-liquidity tokens are the highest-risk cohort — this is a
  systematic false-negative on exactly the tokens that matter most.
- **Files/modules:** `rug_analyzer.py`, `blockscout_client.py` (creation-tx lookup),
  `analyzers.py` (`analyze_age` already handles the ISO path).
- **Dependencies:** none.
- **Effort:** Small · **Risk:** Low.
- **Expected improvement:** Correct age for pre-DEX and just-launched tokens. Detection Δ: Med–High.
- **Acceptance criteria:**
  - Token with no pair but a known creation tx reports a real age and source
    `contract_creation`.
  - Existing pair-timestamp path unchanged and still preferred.
- **Suggested tests:** age from creation ISO only; pair timestamp still wins when both present;
  unknown only when truly absent.

**Blocker to solve first:** none.

---

### M4 — Contract-aware insider/holder filtering ✅ COMPLETE

> **As built:** `detect_insiders` and `profile_token_wallets` take a `known_contracts`
> skip-set; `analyze_token_contract` builds it from the LP pair address plus any sampled
> holder flagged `is_contract` (data already on hand — no extra API calls) so the AMM
> pair is never mislabeled "buyer #1". Backward compatible when the set is omitted.
> Tests in `tests/test_wallet_intel.py`.

- **Goal:** Exclude known contracts (LP pair, router, infra) from insider detection and
  holder-based "buyer #1" labeling.
- **Why it matters:** `detect_insiders` skips zero + creator but not contracts, so the first
  post-launch recipient (often the AMM pair or router) is mislabeled `early_buyer #1` on nearly
  every token — a recurring false positive.
- **Files/modules:** `wallet_intel.py` (`detect_insiders`), `launchpad_registry.py` (known
  infra addresses), `rug_analyzer.py` (pass LP/pair address through).
- **Dependencies:** none; benefits from M8 registry population.
- **Effort:** Small · **Risk:** Low.
- **Expected improvement:** Removes a per-token FP. Detection Δ: Med.
- **Acceptance criteria:** LP/router/contract addresses never appear as insiders;
  real EOAs unaffected.
- **Suggested tests:** insider list excludes a flagged contract recipient; EOA early buyers
  still detected.

---

### M5 — Union-find cluster link-type re-keying ✅ COMPLETE

> **As built:** `analyze_clusters` now records link types and funder attribution
> per-node (stable keys) and aggregates them to each component's final root at
> collection time, so a later mutual-transfer union that changes the root can no
> longer orphan a shared-funder link. Dead `_member_key` removed. Regression test
> `test_analyze_clusters_retains_link_type_after_root_change` in `tests/test_analyzers.py`.


- **Goal:** Re-key `link_types`/`funder_of` after all unions complete so clusters aren't
  dropped when a later mutual-transfer union changes a component root.
- **Why it matters:** Link-type maps are keyed by `uf.find(m)` at funder-loop time; a
  subsequent mutual-transfer union can move the root, after which final collection finds no
  types and skips a real shared-funder cluster — a coordinated-control false negative. Current
  tests only cover 2-member cases, so it passes CI.
- **Files/modules:** `analyzers.py` (`analyze_clusters`), remove dead `_member_key`.
- **Dependencies:** none.
- **Effort:** Small · **Risk:** Low.
- **Expected improvement:** No lost clusters in mixed-link graphs. Detection Δ: Med.
- **Acceptance criteria:** a graph where funder-linked members are later transfer-linked keeps
  both link types and is not dropped.
- **Suggested tests:** multi-member mixed-link graph retains cluster + `link_type == "mixed"`;
  regression test for the root-change case.

---

### M6 — Signal semantics: distinguish "smart hold" from "dump" ✅ COMPLETE

> **As built:** in `smart_wallet_proxy` the +30 signal now credits HOLDING (sent
> <50% of received) as smart and flags dumping (≥50%) as exit risk with no smart
> credit. Scoring hookup deliberately deferred to M10 — the proxy is inert in
> production (max 65 < threshold 70), so deep-analysis output is unchanged; this is
> a correctness fix for when M10 activates it. Tests in `tests/test_wallet_intel.py`.

- **Goal:** Split the +30 "distributed after accumulating" signal into "early + still holding"
  (smart) vs "early + dumped" (exit/insider risk).
- **Why it matters:** The current signal labels dumping behavior as "smart," pointing the
  wrong way on a rug-analysis tool.
- **Files/modules:** `wallet_intel.py` (`smart_wallet_proxy`), `scoring.py` if the dump case
  should feed risk.
- **Dependencies:** interacts with M10 (smart-wallet activation) — coordinate.
- **Effort:** Small · **Risk:** Low–Med (changes heuristic meaning; document clearly).
- **Expected improvement:** Correct signal direction. Detection Δ: Med (correctness).
- **Acceptance criteria:** a wallet that dumps ≥50% is no longer credited as smart; a holder is.
- **Suggested tests:** hold vs dump scenarios produce distinct signals/scores.

---

### M7 — Confidence / data-completeness scoring ✅ COMPLETE

> **As built:** `RugAnalysis` gains `confidence` (0–100) + `confidence_level`
> (low/medium/high), computed in `scoring.py` from which core inputs were present
> (market, holders, age, dev, liquidity_lock). Additive metadata only — does not
> affect `risk_score`/`risk_level`. `score_token_light` reports low confidence
> honestly. UI shows a "Data Confidence" card, escaped. Tests in `tests/test_scoring.py`.

- **Goal:** Surface a confidence indicator alongside the risk score reflecting which inputs
  were available (holders, source, pair, transfers).
- **Why it matters:** A flat score can't distinguish "clean" from "couldn't see." Users need
  to know when a low score means low data, not low risk.
- **Files/modules:** `scoring.py`, `models/token.py` (RugAnalysis field), `frontend/app.js`.
- **Dependencies:** none.
- **Effort:** Small · **Risk:** Low.
- **Expected improvement:** Trust/usability; fewer misread "safe" verdicts. Detection Δ: Med.
- **Acceptance criteria:** response carries a confidence value derived from present data
  sources; UI shows it; escaped like all other untrusted-adjacent output.
- **Suggested tests:** confidence drops when sources missing; full-data case reports high.

---

### M8 — Registry population (launchpads, lockers, infra) ✅ COMPLETE

> **As built:** `launchpad_registry.py` restructured into a documented, editable registry
> schema — `LAUNCHPADS` (name, factory_address, team_addresses, event_signatures, source,
> verified_date, enabled) and `LP_LOCKERS` (address, label, source, verified_date). Both
> kept **EMPTY in production** by design: no addresses were fabricated or scraped for an
> unverifiable new chain, so production detection still returns `Unknown` (no false-safe
> verdicts). `detect_launchpad` is now registry-driven: verified factory → HIGH, verified
> team wallet → LOW, name/tag substring → LOW (demoted from the old MEDIUM — a name is not
> an address). Verified-event MEDIUM tier deferred to M9 (needs receipt/log reads). Burn
> addresses stay chain-agnostic. Example addresses live in `tests/test_launchpad_registry.py`
> only. Liquidity-state expansion deferred to M13; bundle/sniper scoring to M14/M15.

- **Goal:** Populate `LAUNCHPAD_DEPLOYERS`, `LP_LOCKERS`, and known-infra addresses with
  confirmed Robinhood-Chain values.
- **Why it matters:** These dicts are empty stubs; detection currently leans on name-substring
  guesses. Real addresses upgrade launchpad, lock, and insider-filter signals from
  low/medium-confidence guesses to high-confidence exact matches.
- **Files/modules:** `launchpad_registry.py` (data only).
- **Dependencies:** none (data-gathering, not engineering); feeds M4 and lock analysis.
- **Effort:** Small per batch · **Risk:** Low.
- **Expected improvement:** Higher-confidence launchpad/lock detection. Detection Δ: Med–High.
- **Acceptance criteria:** confirmed addresses resolve to exact-match `high` confidence;
  entries are sourced/dated.
- **Suggested tests:** exact-match detection for seeded addresses; unknowns still degrade
  gracefully.

> **Deferred from the M8 build (structure shipped; these need fetch/RPC, so they land later):**
> creation-transaction launchpad detection → **M9**; event-log launchpad detection → **M9**;
> confidence upgrade from event evidence (the MEDIUM tier) → **M9**; LP owner verification,
> LP burn detection, LP locker verification, and lock-expiry detection → **M13**; bundling
> detection → **M14**; same-block / sniper analysis → **M15**. The registry schema now carries
> an `event_signatures` field reserved for the M9 MEDIUM tier.

### M9 — RPC layer (shared, bounded) — foundation for behavior analysis ⚠️ PARTIAL

> **As built:** the fetch-dependent **launchpad detection** half shipped, built on the
> existing **Blockscout** client (not raw JSON-RPC): new cached `get_transaction` /
> `get_transaction_logs`; `launchpad_registry.match_creation_evidence` (verified factory
> `to` → HIGH, verified factory event signature in creation logs → MEDIUM) +
> `has_enabled_launchpads` gate; `analyze_launchpad` prefers on-chain evidence over the
> creator/name heuristics. All fetches are gated on a non-empty registry, so with the
> empty production registry no extra calls fire and behavior is unchanged. Tests use
> example addresses/signatures only. **Deferred:** the raw `eth_call`/`eth_getStorageAt`
> JSON-RPC client (Part A) is **not** built — it has an unresolved RPC-probe blocker, is
> consumed only by M10/M11, and building it unused now would be speculative. It moves to
> **M10** (its first consumer), where the probe must be resolved first.

- **Goal:** Add a JSON-RPC client using the configured (currently unused) `rpc_url`, sharing
  the bounded pattern from `app/services/http.py`, with `eth_call`/`eth_getStorageAt` support.
- **Why it matters:** Today the analyzer reads only metadata and history — it never reads
  contract *state* or simulates behavior. Most real rugs execute through contract mechanics
  invisible to a metadata screen. This layer is the prerequisite for M10, M11, and M15.
- **Files/modules:** new `app/services/rpc_client.py`, `config.py` (rpc limits), `main.py`
  (shutdown close), possibly a web3/eth-abi dependency (pin exact version).
- **Dependencies:** M1 (caching) strongly recommended — RPC calls must be cached/bounded.
- **Effort:** Medium · **Risk:** Med (new dependency + external RPC reliability).
- **Expected improvement:** Unlocks the entire behavior-analysis tier. Detection Δ: enabler.
- **Acceptance criteria:**
  - One shared, bounded RPC client; closed on shutdown.
  - `eth_call` round-trips against a known contract; failures degrade to `None` like the
    Blockscout client.
- **Suggested tests:** encode/decode helpers pure-tested; client returns `None` on RPC error
  without raising.
- **Deferred from M8 (registry-driven launchpad detection) — implement here:** M8 built the
  registry + exact-match logic but left the fetch-dependent detection steps for this layer:
  - **Creation-transaction launchpad detection** — fetch a token's contract-creation tx and
    compare its `to`/factory against the registry (the address-fetch M8 could not do).
  - **Event-log launchpad detection** — parse known factory event signatures
    (`event_signatures` in the registry schema) from creation/receipt logs.
  - **Confidence upgrade from event evidence** — a verified factory *event* match resolves to
    `MEDIUM` (the tier M8 explicitly reserved; today only factory-address=HIGH and
    heuristics=LOW exist).

**Blocker to solve first:** probe the public RPC (`https://rpc.mainnet.chain.robinhood.com`)
for `eth_call` support and rate limits before committing — a flaky/limited RPC pushes M9–M11
and M15 toward their upper effort bounds and may require a fallback provider.

---

### M10 — Honeypot / sell-tax simulation (flagship) ✅ COMPLETE

- **Status:** ✅ **COMPLETE** (2026-07-17). All deliverables shipped and validated live on
  Robinhood Chain: **A** reusable JSON-RPC client, **B** honeypot/sell-tax simulation
  (v3 SwapRouter02, route-agnostic prober with WETH-direct + USDG quote-hop discovery),
  **C** M9 creation-tx routed through the RPC client with Blockscout fallback. 144 tests pass.
- **Goal:** Simulate a buy→sell round-trip via RPC static calls to detect unsellable tokens,
  extreme sell taxes, and blacklist-on-buy traps.
- **Why it matters:** This is the single strongest rug signal that exists and the biggest
  false-negative killer — none of these traps appear in holders/transfers/source-string data.
  Revives the intent of the deleted `honeypot_client.py`, done properly on the RPC layer.
- **Deliverables (in order):**
  - **A. Build the reusable JSON-RPC client** (prerequisite). A small client for raw RPC
    (`eth_call`, `eth_getTransactionByHash`, `eth_getTransactionReceipt`) against the public
    RPC, with the URL configurable and errors surfaced as explicit unknowns (never a crash or
    false "safe"). Consumed by the rest of M10 and later milestones needing raw RPC access
    (M11 privilege reads, M15). Nothing else in M10 can start until this exists.
  - **B. Honeypot / sell-tax simulation** — the flagship, built on top of the client from A.
    **Shipped and activated on Robinhood Chain.** `app/services/honeypot_sim.py`: pure ABI
    encode/decode + `classify()` (honeypot | high_tax | sellable | unknown) and a buy→sell
    round-trip run atomically in ONE state-override `eth_call` via an injected prober contract
    (two calls can't share state). Wired through `rug_analyzer` (reuses the fetched market
    pair, zero discovery calls) into a new additive `honeypot` scoring signal — critical/40
    for honeypot, high/20 for extreme tax; `sellable`/`unknown` add nothing and are
    deliberately kept out of `_CONFIDENCE_WEIGHTS` so a failed sim never distorts risk or
    confidence. Result cached per-token (one sim per analyze; `unknown` stays retryable).
    **As-built activation:** the chain runs Uniswap **v3** (not v2). Verified addresses
    (each cross-checked on-chain, pinned in `app/core/honeypot_artifact.py`): WETH
    `0x0Bd7…AD73` (Robinhood docs + Blockscout verified), USDG `0x5fc5…d168`, SwapRouter02
    `0xCaf6…5cb2` (its `WETH9()`/`factory()` read live), and v3 factory `0x1f7d…2efa`.
    Prober source in `contracts/HoneypotProber.sol`, compiled with solc 0.8.24 and pinned
    as runtime bytecode. **Gotcha fixed during activation:** state-override `code` injection
    does NOT run a constructor, so constructor-initialized storage reads as zero — routing
    data must be passed as calldata, never stored.
    **USDG quote-hop (done — completes B):** the prober is route-agnostic — it calls
    `exactInput` with `path` bytes built off-chain by a reusable
    `app/services/route_discovery.py`. Discovery reads the v3 factory `getPool` + pool
    reserves (`balanceOf`, deliberately NOT `liquidity()` — a concentrated-liquidity pool
    reads zero in-range liquidity while still holding swappable reserves) and picks, in
    configured preference order, the first quote asset with a funded path: WETH-direct,
    else a WETH→quote→token 2-hop. Quote assets and per-asset reserve floors (each in its
    own token decimals) are config (`honeypot_quote_assets`, `honeypot_min_quote_reserve`,
    `honeypot_fee_tiers`); adding a future quote asset is a config edit, no recompile.
    Verified live end-to-end: WETH-paired tokens (TSLA, CASHCAT) route direct → `sellable`;
    a USDG-only token (KARMA, dust WETH pool) routes WETH→USDG→token → `sellable`; a token
    with dust on both quote assets (AAPL) degrades to `unknown` — never a false "safe."
    Router-ABI variance for OTHER DEXes remains the documented risk. Reproduce with
    `python -m scripts.probe_honeypot_e2e`.
  - **C. Route M9 creation-tx retrieval through the client** (carried from M9): prefer RPC
    (`eth_getTransactionByHash` / `eth_getTransactionReceipt`) and **fall back to the current
    Blockscout path** (`blockscout_client.get_transaction` / `get_transaction_logs`) when RPC
    is unavailable or errors. M9's launchpad detection consumes whichever source succeeds; the
    registry-driven matching (`match_creation_evidence`) is source-agnostic and needs no
    change. See the marker at the M9 creation-evidence block in `rug_analyzer.py`.
- **Files/modules:** new JSON-RPC client module, new simulation module (`honeypot_sim.py`),
  pinned artifact (`app/core/honeypot_artifact.py`), prober source (`contracts/HoneypotProber.sol`),
  live E2E check (`scripts/probe_honeypot_e2e.py`), `rug_analyzer.py` (wire result),
  `scoring.py` (new signals), `models/token.py`, `frontend/app.js`.
- **Dependencies:** M1 (cache — one sim per analyze, cached). Builds its own RPC client (A);
  does not depend on M9, which shipped on Blockscout.
- **Effort:** Large · **Risk:** High (correctness of simulation; chain-specific router ABI).
- **Expected improvement:** Catches unsellable/high-tax rugs metadata can't see.
  Detection Δ: Very High.
- **Acceptance criteria:** ✅ met and verified live on Robinhood Chain.
  - Known-sellable token → sellable; simulated honeypot → flagged with a high-severity signal.
    ✅ WETH-paired (TSLA, CASHCAT) route direct and USDG-only (KARMA) routes WETH→USDG→token,
    all returning `sellable` end-to-end via the live RPC; a sell revert is caught as
    `soldBack=0` → honeypot. Verify with `python -m scripts.probe_honeypot_e2e`.
  - Simulation is bounded (one per analyze) and cached; RPC failure degrades to "could not
    simulate," never a crash or false "safe." ✅ Tokens with no funded WETH or quote-asset
    pool (AAPL — dust on both) degrade to `unknown`, not a false verdict.
- **Suggested tests:** sim result → signal mapping (pure); failure path yields explicit
  unknown, not a clean score.
- **RPC probe result (2026-07-16):** the public RPC is **Arbitrum Nitro** (`nitro/v3.11.3`,
  chain id `0x1237`=4663) and **supports `eth_call` state overrides** — verified by injecting
  runtime bytecode via the 3rd `eth_call` param and reading back `uint256(42)`. Methods tested:
  `eth_chainId`, `web3_clientVersion`, `eth_blockNumber`, `eth_call` (identity precompile
  baseline), `eth_call` with a `{addr:{code}}` state override. **Implication:** the safe
  buy→sell round-trip via state-override `eth_call` is viable; the standing "RPC reliability"
  blocker is cleared for override support. The remaining chain-specific unknown is the DEX
  router address/ABI family (still config-gated, inert until sourced).
- **Activation result (2026-07-16):** DEX router unknown **resolved** — the chain runs Uniswap
  **v3**, not v2. Probe/prober rewritten to v3 semantics (`SwapRouter02.exactInputSingle`,
  fee-tier sweep). Router `0xCaf6…5cb2` confirmed by reading its `WETH9()` (matches the docs
  WETH) and `factory()` live; the v3 factory `getPool()` and pool `liquidity()`/`slot0()`
  reads confirmed real USDG/WETH and TSLA/WETH pools. Prober (`contracts/HoneypotProber.sol`)
  compiled with **solc 0.8.24** (no bytecode hand-written) and pinned in
  `app/core/honeypot_artifact.py`; config now defaults the `uniswap` dexId to the verified
  router + WETH + prober, so the sim is **live in production** on this chain. End-to-end
  confirmed against the live RPC (TSLA/USDG → sellable). **No outstanding activation task.**
  One bug caught only by live verification: a `code` state override does not run a constructor,
  so constructor-initialized storage reads as zero — fee tiers moved to a function-local memory
  literal and re-pinned.

---

### M11 — Contract-privilege / authority reads ✅ COMPLETE

- **Status:** ✅ **COMPLETE** (2026-07-19). New `contract_privileges.py`: pure ABI-power
  detection (mint/pause/blacklist/fee mutators) + live `owner()`/`getOwner()`/`paused()`
  `eth_call` reads via the M10 RPC client. The verified `/smart-contracts` payload is now
  fetched once and shared with `contract_intel` (no extra Blockscout request). A confirmed
  renounce (owner == zero) is the only thing that silences retained-power signals; retained
  OR unconfirmed ownership keeps them flagged, and unverified/no-ABI degrades to
  `analyzed=False` (never a false clean). Additive bonus detector — not folded into
  confidence, mirroring the honeypot discipline. 379 tests pass.
- **Goal:** Read live contract powers: ownership renounced? mintable? pausable? blacklist?
  mutable fees? Combine ABI (already fetched) with `eth_call` for live owner/paused state.
- **Why it matters:** `contract_intel` currently sees *which library* was imported, not *what
  dangerous powers remain*. This turns source intel into "what can the dev still do to you"
  (infinite-mint, freeze, fee-flip) — the second-biggest behavior gap after honeypots.
- **Files/modules:** `contract_intel.py` (ABI parsing), new privilege module, `scoring.py`,
  `models/token.py`, `frontend/app.js`.
- **Dependencies:** M10 (RPC client for live state); ABI already available via `get_smart_contract`.
- **Effort:** Medium–Large · **Risk:** Med–High.
- **Expected improvement:** Detects retained-privilege rugs. Detection Δ: High.
- **Acceptance criteria:** renounced vs owner-retained distinguished; mint/pause/blacklist/fee
  powers surfaced as signals; unverified contracts degrade gracefully.
- **Suggested tests:** ABI-privilege detection (pure); live owner read mapped to signal;
  unverified → no false clean.

### M12 — Full holder set (paged) instead of one sampled page ✅ COMPLETE

- **Status:** ✅ **COMPLETE** (2026-07-19). `blockscout_client.get_token_holders_paged(address, pages)`
  follows `next_page_params` (bounded by the configurable `holder_scan_pages`, default 4 ≈ 200
  rows) exactly like the existing `get_token_transfers` pager; `/counters`
  (`token_holders_count`) is now fetched in the analyze gather batch for the true holder count,
  falling back to the token payload's
  `holders_count`. `analyze_holders` was already source-agnostic, so concentration/top1/top10/
  clusters/LP-exclusion now compute over the wider paged set with no logic change. 384 tests pass.
- **Goal:** Page the holders endpoint (bounded) and use `/tokens/{addr}/counters` for true
  holder count, so concentration/top1/top10/clusters reflect more than ~50 rows.
- **Why it matters:** A whale at rank 51 is currently invisible; a 40-holder token looks like
  a 40,000-holder one at the top. Reduces both false positives (broad tokens flagged
  concentrated) and false negatives (missed deep whales).
- **Files/modules:** `blockscout_client.py` (paging), `analyzers.py` (`analyze_holders`),
  `rug_analyzer.py`.
- **Dependencies:** M1 (cache) + M2 (budget) — paging multiplies requests.
- **Effort:** Medium · **Risk:** Med (request volume).
- **Expected improvement:** Materially better concentration accuracy. Detection Δ: High.
- **Acceptance criteria:** concentration computed over paged set; page count bounded and
  configurable; existing holder tests still pass.
- **Suggested tests:** multi-page aggregation (mocked); LP exclusion still holds across pages.

---

### M13 — LP lock duration & unlock schedule ✅ COMPLETE

- **Status:** ✅ **COMPLETE** (2026-07-19). `launchpad_registry.locker_unlock_spec(address)` returns
  an unlock-read spec (`selector` + `word_index`) for a verified locker that declares one;
  `analyzers.decode_unlock_timestamp` + `apply_unlock_schedule` fold the read unix time into a
  time-aware `LiquidityLock` (`unlock_timestamp`, `unlock_in_days`), downgrading an already-past
  unlock to `unlocked`. The analyze pipeline fires **one** `eth_call` only when a matched locker
  has a spec (reusing the M10 `rpc_client`), so it is **inert in production** (empty `LP_LOCKERS`)
  and for spec-less lockers/burn addresses — degrading to the prior presence-only verdict, never a
  false "safe". `scoring` adds a high-severity "LP lock expiring soon" signal when the unlock is
  within `lp_lock_near_term_days` (default 30); a long lock adds nothing (a lock's reassurance is
  the absence of this signal). 397 tests pass.
- **Goal:** Beyond burn/known-locker *presence*, read locker-contract state for the *unlock
  timestamp*, turning the binary lock signal into a time-aware one.
- **Why it matters:** A lock unlocking tomorrow is nearly as dangerous as no lock. Presence
  alone gives false confidence.
- **Deferred here from the M8 proposal (security-first liquidity states):** replace the current
  binary locked/unlocked verdict with distinct, evidence-gated states —
  **LP owner verification** (who owns the LP tokens / LP-NFT), **LP burn detection** (LP sent to
  a burn address → *Verified Burned*), **LP locker verification** (LP held by a registry-verified
  locker → *Verified Locked*), and **lock-expiry detection** (read the locker's unlock timestamp).
  A platform that normally auto-locks but is not yet confirmed must report *Expected Locked*, never
  a confident *locked*. When evidence is incomplete, return *Unknown* — never a false-safe verdict.
- **Files/modules:** `analyzers.py` (`analyze_liquidity_lock`), `launchpad_registry.py`
  (locker ABIs), `scoring.py`, new RPC reads.
- **Dependencies:** M10 (RPC client), M8 (populated locker registry).
- **Effort:** Medium–Large · **Risk:** Med–High (per-locker ABI knowledge).
- **Expected improvement:** Distinguishes real long locks from expiring ones. Detection Δ: High.
- **Acceptance criteria:** near-term unlock scored higher than long lock; unknown locker
  degrades to current behavior.
- **Suggested tests:** unlock-timestamp → severity mapping (pure).

---

### M14 — Funder-graph depth & bundler detection ✅ COMPLETE

- **Status:** ✅ **COMPLETE** (2026-07-19). `_trace_funders` now walks each holder's funding
  chain up to `funder_max_hops` (default 2, config-bounded) with per-wallet memoization, so cost
  is O(distinct wallets) not O(holders × hops); it returns both the legacy immediate-funder map
  and the per-holder chains. `analyze_clusters` gained a `funder_chains` param and unifies holders
  sharing a funder *anywhere* along their chain (single-hop callers are unchanged — a length-1
  chain reproduces the prior result). New pure `analyze_bundle` grades the largest shared-funder
  group into a 0–100 `BundleAnalysis` with Normal/Moderate/Heavy/Extreme classes (signals: bundle
  size, supply concentration, deployer-funds-the-bundle), surfaced as additive metadata on the
  response and a Heavy/Extreme-only signal in `scoring`. 407 tests pass.
- **Goal:** Trace funding 2–3 hops (not one), and detect bundlers (one funder → many fresh
  wallets → all buy same token/block).
- **Why it matters:** `_trace_funders` looks one hop back from a single tx page. The classic
  sybil-launch pattern (bundled fresh wallets) is missed by single-hop clustering.
- **Files/modules:** `rug_analyzer.py` (`_trace_funders`), `analyzers.py` (`analyze_clusters`),
  `blockscout_client.py`.
- **Dependencies:** M1, M2 (request-heavy); M5 (clustering correctness) first.
- **Effort:** Large · **Risk:** Med–High.
- **Expected improvement:** Catches bundled launches. Detection Δ: High.
- **Acceptance criteria:** multi-hop shared funder unifies wallets; bundler pattern raises a
  signal; hop depth bounded/configurable.
- **Suggested tests:** multi-hop union (mocked); bundler fixture flagged.

> **Deferred from the M8 proposal — bundling detection lands here:** the requested
> "bundle score (0–100)" with Normal/Moderate/Heavy/Extreme classes and its signals
> (repeated funding wallet, concentration in <5 wallets, clustered wallets, creator
> participation, percentage concentration) belong to M14. It must be **additive
> metadata**, not a replacement for existing scoring. The same-block/first-seconds
> and sniper-timing portion of that proposal is M15 (below).

---

### M15 — Same-block / coordinated-buy timing detection ✅ COMPLETE

- **Status:** ✅ **COMPLETE** (2026-07-19). `normalize_transfers` now captures each transfer's
  `block` (Blockscout `block_number`); new pure `analyzers.analyze_buy_timing` flags coordination
  from the transfers **already fetched** (no extra call): it takes each wallet's first acquisition
  (excluding mint/creator/LP/known contracts) and detects the largest same-block cohort plus the
  count of buyers landing within `coordinated_buy_window_seconds` of the first buy. A cohort of
  `coordinated_buy_min_cohort`+ distinct wallets (default 3) sets `coordinated=True`, which adds a
  medium `clusters` signal in `scoring`; single/low-buyer tokens return `coordinated=False` and add
  nothing. Surfaced as additive `buy_timing` metadata on the response + a UI card. 417 tests pass.
- **Goal:** Flag wallets buying in the same block or within seconds of launch as coordinated,
  independent of funding source.
- **Why it matters:** Strong coordinated-control signal on data largely already fetched
  (transfer timestamps), complementing funder-based clustering.
- **Files/modules:** `analyzers.py`, `wallet_intel.py`, `scoring.py`.
- **Dependencies:** transfer timestamps (already available); block data may need M9.
- **Effort:** Medium · **Risk:** Med.
- **Expected improvement:** Detection Δ: Med–High.
- **Acceptance criteria:** same-block cohort detected and scored; single-buyer tokens unaffected.
- **Suggested tests:** same-timestamp/block cohort fixture → cluster signal (pure).

> **Deferred from the M8 proposal — same-block/sniper analysis lands here:** the
> "first 20–50 buys," same-block buys, and buys-within-first-seconds portions of the
> proposed bundling workflow belong to M15. Bundle *scoring/classification* is M14;
> this milestone supplies the timing signals that feed it. Additive metadata only.

### M16 — Smart-wallet cross-token implementation (activate the dead path) ✅ COMPLETE

- **Status:** ✅ **COMPLETE** (2026-07-19). Blocker resolved: `/addresses/{addr}/tokens` is live on
  Robinhood Chain (probed), so no transfer reconstruction was needed. New
  `blockscout_client.get_address_token_holdings` + `wallet_intel._count_surviving_tokens` count each
  candidate's *other* surviving ERC-20 holdings and feed `surviving_tokens` into the existing
  `smart_wallet_proxy` (which already scored it, +up-to-35). `profile_token_wallets` pre-scores the
  on-token signals, then does a survival lookup only for near-threshold wallets, capped at
  `smart_wallet_survival_candidates` (default 10) strongest-first — so a wallet early on ≥2 surviving
  tokens can now clear the 70 threshold. Request volume is bounded (≤ cap lookups/analyze). Frontend
  empty-state reverted to a genuine "none found". M6 semantics (dumped ≠ smart) preserved unchanged.
  424 tests pass.

**Goal:** Compute `surviving_tokens` per candidate wallet so the smart-wallet proxy can actually clear its threshold, populating the "Smart Wallets" tab.

**Why it matters:** The scoring, model (`SmartWallet`), persistence, watchlist UI, and `watchlist_hits` cross-reference are all built and working — only the population input is missing. Max reachable score today is 65 vs. a threshold of 70 (documented `# ponytail:` comment at the call site). This is the postponed H3 fix; it unlocks "a wallet early on N surviving tokens is also in this token."

**Files/modules likely to change:** `app/services/wallet_intel.py`, `app/services/blockscout_client.py` (per-wallet token holdings), `app/core/config.py`, frontend empty-state text (revert the "not yet implemented" honesty message), `tests/test_wallet_intel.py`.

**Dependencies:** M1 (caching — this multiplies requests by candidate count), M2 (concurrency cap). **Blocker to resolve first:** confirm Blockscout exposes a per-wallet token-holdings endpoint for Robinhood Chain (`/addresses/{addr}/tokens` or equivalent). If absent, reconstruct from token-transfers, which pushes effort to Large.

**Effort:** Medium–Large · **Risk:** Medium (request amplification; heuristic re-tuning)

**Expected improvement:** Detection Δ Med–High. Activates a whole feature surface; strongest as a cross-token co-signal.

**Acceptance criteria:**
- At least some wallets reach `proxy_score >= threshold` on real data; smart list is no longer always empty.
- Request volume per analyze stays bounded (cap candidate wallets scored for survival).
- Frontend smart-wallet empty state reverts to a genuine "none found" message only once population works.
- Semantics fix from M6 is applied (smart ≠ dumped).

**Suggested tests:**
- `surviving_tokens` passed through end to end lifts score above threshold in a fixture.
- Candidate-cap respected (N wallets → at most N survival lookups).

---

### M17 — Persistent wallet reputation (cross-token memory) ✅ COMPLETE

- **Status:** ✅ **COMPLETE** (2026-07-19). No schema change was needed: the `wallet_activity`
  table (address → tokens) already recorded every token each flagged wallet touched, so
  cross-token memory was a query away. New `watchlist_store.prior_token_counts` runs one bounded
  `COUNT(DISTINCT token_address) GROUP BY wallet` (excluding the token under analysis), defensive to
  an empty/missing DB. `_watchlist_hits` now enriches each hit with `prior_tokens`, and
  `score_token` grows a reputation signal — "Repeat insider wallets present" (medium/high, escalates
  with count/history) and a lighter "Recurring smart wallets present" (low, informational) — gated by
  `wallet_reputation_min_prior_tokens` (default 2) so a wallet's first sighting never scores. The
  frontend hit row now shows "seen on N prior tokens". 434 tests pass (+10).

**Goal:** Remember insider/cluster/smart wallets across tokens so a wallet seen on one token is recognized on the next.

**Why it matters:** "This wallet was an insider on 3 prior rugs" is a top-tier signal and needs no external data — it compounds with usage on a single chain. Feeds `watchlist_hits` directly.

**Files/modules likely to change:** `app/services/watchlist_store.py` (schema: per-wallet token history, rug associations), `app/services/wallet_intel.py`, `app/services/rug_analyzer.py`, `app/models/token.py`, `tests/test_wallet_intel.py`, new store tests.

**Dependencies:** M16 overlaps (same persistence backbone); M1/M2 for scale. Builds on existing SQLite `watchlist_store`.

**Effort:** Medium · **Risk:** Medium (schema growth; store is a cache, not source of truth — keep it defensive)

**Expected improvement:** Detection Δ High, compounding over time.

**Acceptance criteria:**
- A wallet flagged on token A surfaces its prior-token history when it appears on token B.
- Reputation reads tolerate an empty/missing DB (existing invariant preserved).
- New signal contributes points only when history is non-trivial (avoid FP on first sighting).

**Suggested tests:**
- Upsert wallet with history on token A, assert it's recognized on token B.
- Empty-DB read returns no reputation, raises nothing.

---

### M18 — Persistent deployer reputation ✅ COMPLETE

- **Status:** ✅ **COMPLETE** (2026-07-19). New `deployers` table in `watchlist_store`
  (`upsert_deployer`/`get_deployer`) persists a deployer's classified launch history
  (reputation + counts + serialized `LaunchedToken` list). `_scan_creator_launches` now
  returns `(launched_tokens, from_cache)`: a fresh cache hit (within
  `deployer_reputation_ttl_hours`, default 24) rebuilds the launch history from the store and
  **skips the live creator scan entirely** (the expensive creator-tx scan + per-launch
  info/pairs fetch); a miss/stale entry runs the live scan and the caller persists the
  result. Reputation classification is unchanged — `analyze_dev` stays the single source of
  truth; the store only caches its input/output. Only non-empty, freshly-scanned results are
  cached (an empty scan is ambiguous, so it self-heals next analyze). 441 tests pass.

**Goal:** Persist deployer → launches → outcomes instead of recomputing live every analyze.

**Why it matters:** `_scan_creator_launches` recomputes a deployer's history on every call, bounded to ~10 tokens and paying the request cost each time. Persisting builds a growing reputation graph: a serial rugger flagged once stays flagged cheaply forever, and the live scan can be skipped on a cache hit.

**Files/modules likely to change:** `app/services/watchlist_store.py` (deployer table), `app/services/rug_analyzer.py` (`_scan_creator_launches`), `app/services/analyzers.py` (`analyze_dev`), `tests/`.

**Dependencies:** M1 (caching); reuses the watchlist DB pattern. Synergistic with M17.

**Effort:** Medium · **Risk:** Medium (cache staleness — a deployer's outcomes change over time; TTL/refresh needed).

**Expected improvement:** Detection Δ High; also a scan-cost reduction (fewer live creator scans).

**Acceptance criteria:**
- Second analyze of a token by a known deployer reuses stored history (no full re-scan) within TTL.
- Serial-rugger classification persists and is retrievable independent of a live scan.
- Stored outcomes refresh after TTL so a deployer's status can worsen.

**Suggested tests:**
- Store deployer with 3 rugged launches, assert reputation without a live scan.
- TTL expiry triggers a refresh path.

---

### M19 — Historical scan snapshots + trend detection ✅ COMPLETE

- **Status:** ✅ **COMPLETE** (2026-07-19). New `snapshot_store.py` (same sqlite discipline as
  `watchlist_store`) persists one compact row per analyze (risk_score + liquidity + top-10 % +
  holder count + timestamp), pruned to `snapshot_history_retain` (default 50) per token. Pure
  `analyzers.analyze_trend` diffs the prior snapshot against the current metrics — all already
  computed in-pipeline, no extra fetch — and flags a liquidity DROP (≥ `snapshot_liquidity_drop_pct`,
  default 40%) or top-10 concentration RISE (≥ `snapshot_concentration_rise_pct` points, default 15).
  `scoring` turns those into slow-rug signals (liquidity high/18, concentration medium/12). First-ever
  analyze has no prior → `has_prior=False`, no deltas, no signal. Trend is read before scoring (feeds
  the score) and the new snapshot is written after (captures the final risk_score). Added
  `GET /api/v1/token/{address}/history`. 454 tests pass.

**Goal:** Persist each analysis (score, signals, key metrics, timestamp) and derive time-series signals ("liquidity dropped 60% since yesterday," "top-holder concentration rising").

**Why it matters:** A single snapshot can't see a *slow rug* — liquidity bleeding out or the dev accumulating over days. Trend deltas catch what point-in-time scoring misses, and stored results enable instant re-serves and back-testing for score calibration (M7b).

**Files/modules likely to change:** `app/services/watchlist_store.py` or new `snapshot_store.py`, `app/services/rug_analyzer.py`, `app/models/token.py` (trend fields), `app/api/routes.py` (history endpoint), frontend (trend display), `tests/`.

**Dependencies:** M1 (caching), ideally M18/M17 (shared persistence). A blocker for the data-driven half of M7 (calibration needs labeled history).

**Effort:** Medium · **Risk:** Low–Medium (storage growth; needs pruning/retention policy).

**Expected improvement:** Detection Δ Med (new slow-rug signal class) + enables calibration.

**Acceptance criteria:**
- Re-analyzing a token stores a new snapshot and computes deltas vs. the prior one.
- A liquidity/concentration trend signal appears when a threshold delta is crossed.
- Retention policy bounds DB growth.

**Suggested tests:**
- Two snapshots with a liquidity drop produce a downward-trend signal.
- First-ever snapshot produces no trend (no prior), raises nothing.

---

### M20 — Technical-debt cleanup ✅ COMPLETE

- **Status:** ✅ **COMPLETE** (2026-07-19). Two live items removed: the unused
  `ETHERSCAN_API_KEY`/`BSCSCAN_API_KEY`/`POLYGONSCAN_API_KEY` env keys (from `render.yaml` +
  `.env.example`; never read in code) and the redundant `lp_addr` reassignment in
  `rug_analyzer.py` (already set once from `best_pair.get("pairAddress")`; the in-guard copy
  was identical). The other four listed items were **already satisfied** by later milestones
  and only needed verification, not rewriting: `_member_key` was removed in M5 (no matches);
  the `contract_created_iso = None` block is now **live M3 fallback code**, not dead; `data/`
  is already in `.gitignore`; and `rpc_url` is now **actively consumed** by M9/M10, so the
  proposed "currently inert" comment would be false and was intentionally not added. 454 tests
  pass; a grep confirms the removed symbols/keys are gone.

**Goal:** Remove leftovers from the deleted multi-chain design and dead code confirmed in the tree.

**Why it matters:** Low-effort hygiene that reduces confusion and shrinks surface area. None of it changes behavior.

**Files/modules likely to change:**
- `render.yaml`, `.env.example` — remove `ETHERSCAN_API_KEY`, `BSCSCAN_API_KEY`, `POLYGONSCAN_API_KEY` (declared, never read; leftovers from deleted `blockchain_detector.py` / `honeypot_client.py`).
- `.gitignore` — add `data/` (SQLite `data/watchlist.db` is currently untracked but not ignored).
- `app/services/analyzers.py` — remove unused `_member_key` (line ~216).
- `app/services/rug_analyzer.py` — remove the duplicate `lp_addr` assignment (lines ~215 and ~267) and the dead `contract_created_iso = None` block (superseded by M3).
- `app/core/config.py` — `rpc_url` is unused today; keep it (M9 consumes it) but comment that it's currently inert.

**Dependencies:** M3 supersedes the dead age block; do M20's age-related cleanup with or after M3.

**Effort:** Small · **Risk:** Low.

**Expected improvement:** None functional; maintainability only.

**Acceptance criteria:** No dead symbols; `data/` ignored; unused env keys gone; suite still green.

**Suggested tests:** Existing suite passes; a grep confirms removed symbols are gone.

---

### M21 — Watchlist improvements ✅ COMPLETE

- **Status:** ✅ **COMPLETE** (2026-07-19). `watchlist_store.get_watchlist(kind, sort)` now takes a
  whitelisted `sort` ("score" | "recency"; never raw SQL) alongside the existing `kind` filter, and
  every entry (list + `get_wallet` detail) is enriched with its cross-token `prior_tokens` count via a
  shared `_prior_token_counts_locked` helper (one grouped query, reused from M17 — no per-row lookups,
  no duplicated logic). `GET /watchlist?kind=&sort=` exposes both; new `POST /watchlist/refresh` is an
  on-request fallback that re-pulls a bounded batch via the existing `refresh_watchlisted`, for
  idle-prone hosts (Render free tier) where the background `_watchlist_refresh_loop` is suspended.
  Frontend adds kind/sort selects, a "Refresh from chain" button, and a "seen on N prior tokens" note.
  465 tests pass.

**Goal:** Make the watchlist more useful: filtering/sorting, richer per-wallet detail, and reliable refresh.

**Why it matters:** The watchlist is the home of cross-token intelligence (M16–M18). Today it lists wallets with recent buys but offers no filtering, and the background refresh loop won't run on a spun-down free host.

**Files/modules likely to change:** `app/api/routes.py`, `app/services/watchlist_store.py`, `frontend/app.js`, `frontend/index.html`, `tests/`.

**Dependencies:** M17/M18 (they add the data worth filtering). Refresh-reliability relates to the deploy environment (Render free tier idles).

**Effort:** Small–Medium · **Risk:** Low.

**Expected improvement:** User value / usability; no direct detection Δ.

**Acceptance criteria:**
- Watchlist can be filtered by kind and sorted by score/recency.
- Wallet detail shows cross-token history once M17 lands.
- Refresh strategy documented for idle-prone hosts (e.g. on-request refresh fallback).

**Suggested tests:** Store query with filter/sort returns expected order; endpoint contract test.

---

### M22 — Multi-chain architecture ✅ COMPLETE

- **Status:** ✅ **COMPLETE** (2026-07-21). Strictly architectural — **no behavioural change**, no new
  chain, no new API. New `app/core/chains.py` introduces a `ChainConfig` pydantic model (identity +
  endpoints + Uniswap-v3 DEX topology) and a slug-keyed registry with exactly one entry, Robinhood
  Chain (`settings.default_chain`, the default). `chains.active()` rebuilds the active `ChainConfig`
  **live from `settings`** on every call, so env overrides and test monkeypatches flow straight
  through and behaviour is byte-for-byte unchanged. Services now read chain identity/endpoints/DEX
  topology from `chains.active()` instead of hardcoded `settings.*`: `blockscout_client` (base URL),
  `dexscreener_client` (chain filter), `rpc_client` (RPC URL), `route_discovery` + `honeypot_sim`
  (WETH/factory/routers/quote-assets/fee-tiers/reserve-floors), `rug_analyzer` + `/chain` (chain
  name/id/explorer). Simulation *policy* (prober bytecode, buy amount, tax threshold) stays in
  `settings` — it is chain-agnostic. The RPC and Blockscout clients, scoring, all APIs, and the
  frontend are untouched. A second chain later = one more registry builder + a `default_chain`
  switch, with zero service changes. 472 tests pass (7 new in `tests/test_chains.py`).

**Goal:** (Deferred) Re-introduce multi-chain support if product direction changes.

**Why it matters:** The app was deliberately narrowed to single-chain (deleted `blockchain_detector.py`, dead `*SCAN_API_KEY` vars). Genuine multi-chain is a large re-architecture: chain-parameterized clients, per-chain registries, per-chain RPC. Depth on one chain (Tiers above) beats breadth for detection quality, so this is last.

**Files/modules likely to change:** Broad — `app/core/config.py` (chain registry), all clients (`blockscout_client`, `dexscreener_client`, RPC layer), `launchpad_registry` (per-chain), `rug_analyzer`, models, frontend chain selector.

**Dependencies:** Everything. Only start after v1.x depth features are stable. **Blocker:** an explicit product decision that breadth is wanted over further depth.

**Effort:** Large (weeks) · **Risk:** High (touches every layer; regresses single-chain focus if rushed).

**Expected improvement:** Breadth, not detection depth. Detection Δ Low per-chain.

**Acceptance criteria:** A second chain can be added via config without forking logic; single-chain behavior unchanged when only one chain is configured.

**Suggested tests:** Chain-parameterized fixtures; single-chain regression suite stays green.

---

### M23 — KOL Intelligence: X (Twitter) follow-graph monitoring → analysis pipeline

- **Goal:** Detect when tracked KOLs (key opinion leaders) follow **new** X accounts, extract any
  crypto project/contract references from those newly-followed accounts, run them through the
  existing rug/alpha pipeline, and emit structured alerts — including **cluster** events when
  several KOLs converge on the same project inside a time window. Turns "who the smart money is
  starting to watch" into an early, pre-liquidity alpha signal metadata can't see.
- **Why it matters:** Every existing signal is *on-chain and reactive* — it needs the token to
  already exist with holders/liquidity. KOL follow-graph movement is a *leading social* signal
  that often precedes a launch or a pump. Clustering (N KOLs following the same account within
  a window) is the strongest form and the whole reason to build the follow-graph, not just
  per-account alerts.
- **Deliverables (in order, incremental — each independently testable and shippable):**
  - **A. KOL watchlist (config-driven data).** ✅ **Done.** A declarative watchlist stored so KOLs are
    added/removed without touching logic: `display_name`, `x_username`, `tier` (1/2/3),
    `enabled`. Mirror the existing stdlib-`sqlite3` `watchlist_store.py` pattern (no new
    dependency for storage); seedable from config. Pure CRUD + a `WatchlistEntry`-style model.
    _As built:_ stdlib-`sqlite3` store `app/services/kol_store.py` (mirrors `watchlist_store.py`,
    lock-guarded, at `kol_db_path`) with the `kols` table (`(platform, handle)` key, `tier`,
    `enabled`) + pure CRUD (`upsert_kol` / `get_kol` / `list_kols` / `delete_kol`, `enabled` toggled
    via `upsert_kol`);
    `KolEntry` and the platform-neutral `SocialAccount` models in `app/models/kol.py`; a
    provider-registry seam (`app/services/social/registry.py` + `base.py`) so a new platform is a
    registered provider, not a logic change; orchestration in `app/services/kol_watchlist.py`,
    seedable from `settings.kol_watchlist_seed`, gated by `settings.kol_intel_enabled`. Covered by
    `tests/test_kol_watchlist.py`. Scope stops at watchlist CRUD + models — no scraping/diff/scoring.
  - **B. Free X monitoring via Playwright (the one new dependency).** ✅ **Done.** A client that drives
    a **persistent authenticated browser context** (reused cookies/session on disk), scrapes a
    KOL's Following list from the public web UI — **never the paid X API** — with randomized
    delays, human-like scrolling, and graceful recovery/back-off on transient failures or rate
    limits. Session and selectors isolated behind this one module so a UI change is a one-file
    fix. Errors surface as explicit "could not fetch," never a crash or a false "no new follows."
    _As built:_ three isolated modules under `app/services/social/` — `x_session.py` (Playwright
    `launch_persistent_context` rooted at `settings.x_user_data_dir` so cookies/auth persist across
    runs; an `async with XSession() as page` context manager with an **injectable `context_factory`**
    so tests exercise session/auth logic with zero Playwright dependency; **never bypasses auth**),
    `x_scraper.py` (`scroll_and_collect` drives X's virtualized infinite-scroll Following list,
    accumulating rendered handles per step, stopping on `x_scroll_stable_rounds` consecutive empty
    scrolls or the `x_scroll_max_rounds`/`x_following_max` caps — returning `ScrapeResult.complete=False`
    when a cap cut it short, so a truncated scrape is never mistaken for a full set), and
    `x_provider.py` (`XProvider.fetch_following` orchestrates session + scraper into a
    `FollowingSnapshot`, translating failures into typed `ProviderError` subclasses). `errors.py`
    adds `SessionExpiredError` / `AuthUnavailableError` (both `retryable=False` — a human must
    reauthenticate) over the Deliverable-A `ProviderError` (carrying `retryable` / `retry_after_seconds`
    for a future scheduler to back off on). Config: `x_user_data_dir`, `x_headless`, `x_scroll_pause_ms`,
    `x_scroll_max_rounds`, `x_scroll_stable_rounds`, `x_following_max`. Wired into
    `kol_watchlist.capture_following`; `playwright` added to `requirements.txt` (the one new dependency).
    Covered by `tests/test_x_provider.py`. Scope stops at fetching a Following snapshot — no diff
    (Deliverable C).
  - **C. Snapshot & diff engine.** ✅ **Done.** Periodically fetch each enabled KOL's Following set, persist a
    timestamped snapshot, diff against the prior snapshot, and emit **only newly-followed
    accounts** (first snapshot establishes a baseline, emits nothing). Snapshots stored via the
    same sqlite layer; retention configurable.
    _As built:_ pure diff engine `app/services/social/diff.py` (`diff_snapshots` → `SnapshotDiff`:
    new/unfollows/unchanged + profile changes, stable-id keyed so a handle rename is a
    `ProfileChange`, not churn); orchestration in `app/services/kol_monitor.py`
    (`process_snapshot` diffs against the last *complete* snapshot, persists snapshot → events →
    profile changes → per-account first/last-seen, and **skips incomplete captures** so an
    interrupted scrape never reads as a mass unfollow); store readers skip corrupted rows and
    fall back to the last intact baseline; retention via `settings.kol_snapshot_retain`
    (`kol_store._prune_snapshots`, always preserving the newest complete baseline). Wired into
    `kol_watchlist.capture_following`. Covered by `tests/test_kol_diff.py` (24 tests: pure diff,
    persistence, error recovery, retention, scope guard). Scope stops at persisted follow-change
    events — no alerting/scoring/clustering/crypto (Deliverables D–H).
  - **D. Crypto-account detection + rug/alpha integration (reuse, don't reinvent).** ✅ **Done.**
    For each newly-followed account, scan bio/name/links for crypto signals: contract addresses
    (`CA:` and bare) across Solana / Ethereum / Base / Robinhood address shapes, and Pump.fun /
    DexScreener / Birdeye / GMGN / GeckoTerminal / CoinGecko / official-project links. Reuse the
    existing address-extraction/validation helpers; obvious non-crypto accounts drop out early.
    Confident crypto projects feed their extracted contracts straight into
    `rug_analyzer.analyze_token_contract()` (which already composes contract detection,
    `honeypot_sim`, risk `scoring`, and alpha scoring) — no analysis logic is reimplemented.
    _As built:_ pure, provider-neutral analyzer stack — `app/services/social/contract_extract.py`
    (multi-chain address mining + validation via the existing `is_valid_address`; EVM/Robinhood
    marked analyzable, other chains recorded but `supported=False`, never dropped),
    `app/services/social/crypto_signals.py` (config-driven signal **registry**: add a weight in
    `settings.kol_crypto_signal_weights` + register a detector to extend detection with zero logic
    change; a signal weighted `<=0` is disabled), and `app/services/social/crypto_intel.py`
    (`classify_account` → `CryptoClassification`: account type + confidence band + weighted score +
    fired signals + structured `Evidence` + `ExtractedContract`s). Every threshold/weight is config
    (`kol_crypto_*`): score→confidence bands, `kol_crypto_min_score`, and a corroboration gate
    (`kol_crypto_min_signals` / `kol_crypto_strong_signals`) that enforces **never classify on a
    single weak signal** — one strong signal (a valid contract) can stand alone; weak signals must
    corroborate, else the verdict downgrades to `individual`/`unknown`. Orchestration in
    `app/services/kol_crypto_pipeline.py` (`process_new_follow(s)`): persists the classification +
    an append-only internal event log (`crypto_project_detected` / `contract_extracted` /
    `analysis_completed` / `analysis_failed`) and invokes the rug analyzer for supported contracts
    through a per-contract TTL cache (dedup across KOLs); analysis is best-effort — one contract's
    failure is logged as an event, never raised. New tables in `kol_store` (`crypto_classifications`
    upsert + `crypto_events` audit log, JSON payloads for forward-compat). Wired additively into
    `kol_watchlist.capture_following` on `diff.new_follows` only (a baseline triggers nothing),
    gated by `settings.kol_crypto_intel_enabled` and fully swallowing pipeline errors so a good
    capture never fails. Covered by `tests/test_kol_crypto_intel.py` (25 tests: extraction,
    config-driven signals, classification + the weak-signal gate, persistence, analyzer reuse +
    dedup + failure isolation, end-to-end capture hook, scope guard). Scope stops at classification
    + reusing the analyzer — no user alerts, KOL scoring, or clustering (Deliverables F–H).
  - **F. KOL Intelligence score (configurable component).** ✅ **Done** (delivers **G** too — the
    scorer consumes the cluster, so both shipped together). A pure scoring function combining
    **tier weighting**, **number of distinct KOLs**, **follow recency/timing**, and **cluster
    strength** into a KOL-intel sub-score. Kept out of the core rug/confidence weights so it
    augments — never distorts — existing risk/confidence math (same discipline as the M10
    `honeypot`/`unknown` handling). All weights configurable.
    _As built:_ pure, offline scorer + cluster detector in `app/services/social/kol_scoring.py`
    (`detect_cluster` → `ClusterInfo`; `score_project` → `(score, confidence, evidence)`). The
    0–100 **KOL Intelligence Score** is a capped, additive sum of config-weighted components
    (`kol_score_weights`): `kol_convergence` (per additional distinct KOL — the core alpha signal),
    `tier_quality` (summed config tier weights), `crypto_confidence` (reused classification band),
    `analysis_safety` (reused rug `risk_score`, **never recomputed**), `cluster_bonus`, `recency`
    (follow-window tightness), and an **optional** `alpha` hook (fires only if a future alpha scorer
    supplies one — inert today). Every component that fires emits one structured `Evidence`, so the
    evidence list reconstructs the score exactly — no opaque math. Correlation orchestration in
    `app/services/kol_intel_engine.py`: for each project a new follow touches, it correlates every
    watched KOL following it (cross-KOL read `kol_store.list_kols_following`, inverting the
    per-KOL follow graph), the best classification seen (`best_classification_for_account`), and the
    latest reused analysis (`latest_analysis_summary`, read from the Deliverable-D event log) into
    one self-contained `ProjectIntelligence` object (score + confidence + evidence + contributors +
    cluster + reused-analysis correlation + score timeline) — everything a future AI stage needs to
    explain a call without rescanning. Scoring is **incremental**: an input `fingerprint` skips
    rescoring/history/duplicate-events when nothing changed. New `kol_store` tables (`kol_intel_scores`
    upsert + `kol_intel_score_history` + `kol_cluster_history` + `kol_intel_events`, JSON payloads,
    per-project retention via `kol_intel_history_retain`). Wired additively into
    `kol_watchlist.capture_following` (only accounts the crypto pipeline judged **projects** are
    scored), gated by `settings.kol_score_enabled` and fully error-swallowing so a good capture never
    fails. All tiers/weights/windows/thresholds are config (`kol_tier_weights`, `kol_score_weights`,
    `kol_confidence_multipliers`, `kol_cluster_*`, `kol_momentum_*`) — no hardcoded tiers, timing, or
    KOL names. Covered by `tests/test_kol_intel_engine.py` (42 tests: pure scorer + evidence
    reconstruction, config-driven weighting, every cluster type, cross-KOL store reads, end-to-end
    correlation reusing rug analysis, incremental skip, momentum, history timelines, scope guard).
  - **G. Cluster detection.** ✅ **Done** (shipped with **F**, above). Detect ≥N KOLs following the
    same project/account within a configurable rolling time window; roll individual follows up into a
    single **KOL Cluster** event with aggregate tier/strength. Window, threshold, and dedupe all
    configurable. _As built:_ `kol_scoring.detect_cluster` de-dupes to distinct KOLs (earliest follow
    kept), measures the convergence span, and tags typed cluster kinds — `tier_1`, `mixed_tier`,
    `rapid`, `high_conviction` — all driven by `kol_cluster_*` config (min KOLs, main + rapid windows,
    Tier-1 minimum, conviction score); the engine persists a `kol_cluster_history` row per detection
    and emits internal `kol_cluster_detected` / `high_conviction_cluster` events (NOT user alerts —
    transports remain Deliverable H).
  - **H. Alert pipeline (transport-agnostic).** ✅ **Done.** Publish typed, serializable events
    through a thin publisher interface with a default log/in-memory sink. Structured so Telegram /
    Discord / Webhook / UI sinks are added later as adapters with no change to producers.
    _As built:_ `app/services/notifications.py` — a `NotificationProvider` ABC (`name` + `send`) with
    the two roadmap sinks: `LogNotificationProvider` (`"log"`, default) and
    `MemoryNotificationProvider` (`"memory"`, in-process buffer for a UI feed / tests). New transports
    register a factory in `_PROVIDER_FACTORIES` + a name in `settings.notify_providers` — **producers
    never change**. The layer **consumes** the Deliverable-F `kol_intel_events` the engine already
    persisted (it generates no intelligence — no scoring, no analysis, no event creation): the single
    call-in `dispatch_events(events, intel)` is invoked from `kol_intel_engine._persist_and_emit`
    right after the events are saved, filters them by fully config-driven forwarding rules
    (`notify_min_score`, `notify_min_confidence`, `notify_min_cluster_size`, `notify_event_types` — all
    AND-ed against the already-computed `ProjectIntelligence`), skips any (event, destination) pair
    already delivered, and records every attempt in the new `notification_deliveries` table
    (status/timestamp/destination/error; `UNIQUE(event_key, destination)` is the dedupe seam so a
    replayed event never double-delivers, while a prior `failed` attempt can be retried). Gated by
    `settings.notify_enabled` (**off by default** — the whole layer is inert out of the box, like the
    other KOL switches) and **fully failure-isolated**: a bad provider or store write is logged +
    recorded and the loop continues, so a delivery failure can NEVER interrupt the capture/analysis
    that produced the events. Covered by `tests/test_notifications.py` (24 tests: successful delivery +
    `sent` record, failed delivery isolation + `failed` record, one bad provider never blocks others,
    disabled/empty-provider/empty-type no-ops, every threshold rule incl. AND-ing, dedupe incl.
    per-destination + retry-after-failure, and the reuse discipline — dispatch writes no intelligence
    and an engine-path delivery failure never sinks a capture).
- **Files/modules (new, additive — no existing file is rewritten):** `app/services/kol_x_client.py`
  (Playwright driver + session), `app/services/kol_snapshot.py` (snapshot/diff engine),
  `app/services/kol_detect.py` (crypto-account detection, reusing address helpers),
  `app/services/kol_intel.py` (orchestration + KOL-intel scoring + cluster detection),
  `app/services/kol_store.py` (sqlite: watchlist, snapshots, follow-events — mirrors
  `watchlist_store.py`), `app/services/alerts.py` (event models + publisher interface),
  `app/models/` (KOL/event pydantic models). **Touched minimally & additively:**
  `app/core/config.py` (new `kol_*` settings block), `app/main.py` (register a KOL monitor
  background loop next to `_watchlist_refresh_loop`, guarded by an enable flag),
  `requirements.txt` (add `playwright`), docs. `rug_analyzer`/`scoring`/`honeypot_sim` are
  **consumed, not modified**.
- **Integration points:** (1) `rug_analyzer.analyze_token_contract()` — the single reuse seam for
  all contract analysis; (2) existing `cache` service for per-token result caching;
  (3) the `main.py` `asyncio.create_task` background-loop pattern for scheduling (no new scheduler
  dependency); (4) the `sqlite3` store pattern from `watchlist_store.py` for persistence;
  (5) pydantic `BaseSettings` for all config; (6) address-extraction/validation helpers reused by
  detection (D). The alert publisher (H) is the future seam for Telegram/Discord/Webhook/UI.
- **Dependencies:** M10 (honeypot sim + full analyze pipeline — done). New external dependency:
  **Playwright** (browser automation) + a one-time authenticated-session bootstrap. **Blocker:**
  X login/session must be provisioned out-of-band (persistent cookies); scraping the public UI is
  brittle by nature and must degrade to "unknown," never a false signal.
- **Effort:** Large (multi-part; Playwright scraping + scheduler + new scoring + clustering) ·
  **Risk:** Med–High (X UI changes/anti-bot fragility is the main risk; contained behind
  `kol_x_client` and explicit-unknown degradation so it never regresses on-chain analysis).
- **Expected improvement:** Adds a **leading social/alpha** dimension absent from every on-chain
  signal; cluster events are the flagship. Detection Δ: High for early/pre-liquidity discovery;
  additive-only to existing risk scoring.
- **Acceptance criteria:** watchlist add/remove via config only; a KOL following a new crypto
  account produces a `NewKolFollow` event with a full rug/alpha analysis attached (no duplicated
  analysis logic); N KOLs on one project within the window produce one `KolCluster` event; all
  intervals/thresholds/tiers/weights are config-driven; X-fetch failure degrades to explicit
  unknown with no crash and no false alert; **existing 144 tests stay green (zero regression)**.
- **Suggested tests:** snapshot-diff (pure: baseline emits nothing, new follow detected, unfollow
  ignored); crypto-account detection across each address shape + link type, non-crypto dropped;
  KOL-intel score and cluster-window logic (pure, table-driven); alert-event serialization;
  Playwright client behind an interface so the scraper is mocked in unit tests (no live network);
  a regression pass asserting `rug_analyzer`/`scoring` outputs are unchanged.
- **Documentation (deliverable):** an `docs/`-level write-up covering architecture (module
  responsibilities + the reuse seam), data flow (KOL → follow diff → detection → analyze → score
  → alert), scheduler model, sqlite schema (watchlist / snapshots / follow-events / cluster
  events), extension points (new alert sinks, new address shapes, new KOL tiers), and the future
  **migration path to official X APIs** should the free-scrape path become untenable.

### M24 — Token Watchlist & Monitoring Engine (continuous re-analysis → change detection) ✅ COMPLETE

- **Goal:** Keep a watchlist of contract addresses under continuous surveillance: on a schedule,
  re-run the **existing** intelligence pipeline against each token and record **only what changed** —
  risk score/level, honeypot status, pool liquidity, and (when a token is linked to a KOL project)
  the KOL Intelligence Score + cluster size. Turns the analyzer from a one-shot lookup into a
  standing watch that surfaces *movement* — a token going from safe to honeypot, liquidity draining,
  risk climbing — without a human re-running a scan.
- **Why it matters:** Every prior milestone answers "what is this token right now?" A rug is a
  *process*, not a snapshot: liquidity is pulled, sell-tax is flipped, risk climbs after launch. The
  signal is in the **delta over time**, which a single analysis can't see. This is the reactive
  on-chain counterpart to M23's leading social watch — same "gets smarter the more it's used" theme,
  applied to the contract itself.
- **Design rule (non-negotiable):** monitoring **reuses, never reimplements.** All contract analysis
  flows through the one shared entry point `rug_analyzer.analyze_token_contract()` (which already
  chains detection → route discovery → `honeypot_sim` → risk `scoring`); KOL signals come from a plain
  read of the `ProjectIntelligence` the M23 engine already computed. There is **no analysis logic** in
  this milestone — only orchestration (scheduling, concurrency, retry, timeout), change detection over
  the reused scalars, and persistence of the deltas. It also adds **no new notification/delivery
  logic** — emitting an internal `MonitorEvent` is where it stops (delivery is M23 Deliverable H's job).
- **Status:** ✅ **Done.**
  _As built:_ pydantic domain models in `app/models/monitor.py` (`TokenWatchEntry` + `MonitorOptions`
  with per-token noise thresholds and an optional KOL-project linkage; `MonitorSnapshot` whose every
  field is copied verbatim from an existing analyzer/KOL output; `MonitorEvent` / `MonitorHistoryEntry`
  / `MonitorResult` / `MonitorCycleReport`; string-"enum" vocabularies with `field_validator`s, matching
  `models/token.py`/`models/kol.py`). Persistence in `app/services/token_monitor_store.py` — a
  stdlib-`sqlite3`, lock-guarded store at `settings.token_monitor_db_path` (own DB file, decoupled from
  the wallet/KOL stores; mirrors `kol_store`/`watchlist_store`) with four tables: `token_watchlist`
  (CRUD + enable/disable + options), `monitor_latest` (the per-token diff baseline), append-only
  `monitor_history` (before/after rows written **only when something changed**, per-token retention via
  `token_monitor_history_retain`), and append-only `monitor_events`; plus `reset_for_tests`. The engine
  in `app/services/token_monitor.py`: thin watchlist management (`add_token`/`remove_token`/`set_enabled`/
  `update_options`, address-validated via the reused `is_valid_address`), `sync_from_config` for a
  declarative seed watchlist (adds/refreshes, never auto-deletes), `_build_snapshot` (the **single**
  reuse seam — one `analyze_token_contract` call + an optional `kol_store.get_project_intelligence`
  read, copying scalars, recomputing nothing), config-driven per-field change detection (`min_risk_delta`
  / `min_kol_delta` / `min_liquidity_change_pct`, with None↔value appearance always counting) raising the
  specific per-field events plus a `project_changed` umbrella, `monitor_once` (timeout + retry, fully
  failure-isolated — never raises, stamps `active`/`error` status), and `run_cycle` (bounded-concurrency
  sweep of enabled tokens, each isolated, guarded a second time so the cycle can't die). Scheduling reuses
  the `app/main.py` `asyncio.create_task` background-loop pattern next to `_watchlist_refresh_loop`
  (`_token_monitor_loop`, seeds from config once at startup then interval-gates `run_cycle`), gated by
  `settings.token_monitor_enabled` (**off by default**, like the other engines). All intervals/thresholds/
  retries/retention are config (`token_monitor_*` block in `config.py`). Covered by
  `tests/test_token_monitor.py` (29 tests: watchlist CRUD + idempotency + events, config seeding, the
  analyzer-reuse seam incl. `include_lore` flow + verbatim-copy + KOL linkage/no-linkage, per-field change
  detection + thresholds + first-sighting baseline + no-change dedupe, persistence + retention pruning,
  and failure isolation — analysis error retried, timeout-as-failure, transient recovery, one bad token
  never sinking the cycle, outer-guard, empty-watchlist no-op).
- **Files/modules (new, additive):** `app/models/monitor.py`, `app/services/token_monitor_store.py`
  (sqlite: watchlist / latest-baseline / history / events — mirrors `kol_store`),
  `app/services/token_monitor.py` (management + scheduler + change detection, reusing the analyzer),
  `tests/test_token_monitor.py`. **Touched minimally & additively:** `app/core/config.py` (new
  `token_monitor_*` settings block), `app/main.py` (register `_token_monitor_loop` next to the existing
  refresh loop, guarded by the enable flag). `rug_analyzer`/`kol_store` are **consumed, not modified**.
- **Integration points:** (1) `rug_analyzer.analyze_token_contract()` — the single reuse seam for all
  contract analysis; (2) `kol_store.get_project_intelligence()` — the reuse seam for KOL signals;
  (3) the `main.py` `asyncio.create_task` background-loop pattern (no new scheduler dependency);
  (4) the `sqlite3` store pattern for persistence; (5) pydantic `BaseSettings` for all config.
- **Dependencies:** M10 (full analyze pipeline) + M23-F (KOL `ProjectIntelligence`, optional). **No new
  external dependency.**
- **Effort:** Medium (orchestration + persistence + change detection; zero new analysis) · **Risk:** Low
  (additive; off by default; every reuse seam already ships and stays unmodified).
- **Expected improvement:** Adds a **temporal** dimension absent from every prior signal — detection of
  *change* (safe→honeypot, liquidity drain, risk climb) rather than a point-in-time verdict. Additive-only.
- **Acceptance criteria:** watchlist add/remove/toggle + config seeding; a monitored token whose reused
  analysis moves produces a history row + typed change events (no duplicated analysis logic); a no-change
  cycle writes nothing new; first sighting establishes a baseline silently; all intervals/thresholds/
  retention config-driven; one token's analysis failure/timeout is isolated and retried, never sinking the
  cycle or the scheduler loop; **existing tests stay green (zero regression — 332 → 361).**

---

### M25 — KOL Intelligence Automation (scheduler that drives the M23 pipeline) ✅ COMPLETE

- **Goal:** Turn the wired-but-manual M23 KOL pipeline into a continuously running engine: on a
  schedule, capture the following list of every **enabled** KOL and let the existing pipeline
  (snapshot → diff → crypto detection → scoring → clustering → events) run over what changed —
  with no human invoking `capture_following` per KOL. This is the missing driver the docs flagged
  as "the natural next `lifespan` task."
- **Why it matters:** Every M23 deliverable (A–H) shipped, but nothing called `capture_following`
  on a cadence, so the leading social signal only moved when someone triggered it by hand. A
  follow-graph signal is only "leading" if it's captured continuously; automation is what makes the
  convergence/cluster events actually fire in time to matter.
- **Design rule (non-negotiable):** the scheduler **orchestrates, never reimplements.** A cycle is
  just "list enabled KOLs → `kol_watchlist.capture_following` each", which already chains the whole
  pipeline. There is **no** capture, diff, detection, scoring, clustering, or event logic in this
  milestone — only orchestration (interval, bounded concurrency, per-KOL timeout + retry/backoff,
  failure isolation, duplicate-run prevention, progress logging, graceful shutdown). Resume-after-
  restart is **free**: all state (snapshots, `sync_meta`, followed accounts) already lives in
  `kol_store`, so a fresh process just resumes iterating the persisted roster and diffs against the
  last persisted snapshot.
- **Status:** ✅ **COMPLETE** (2026-07-21).
  _As built:_ new `app/services/kol_scheduler.py` — `capture_one(entry)` wraps a single
  `capture_following` call with a per-KOL timeout (`asyncio.wait_for`) and a retry/backoff loop that
  honours `ProviderError.retryable` (a non-retryable error — suspended/private — stops early; a
  missing/incapable provider is a permanent `skipped`, not a burned retry; an incomplete/partial pull
  is a retryable non-success that preserves the prior baseline). `run_cycle()` sweeps the
  **enabled-only** roster (`kol_watchlist.list_kols(enabled_only=True)`) under an
  `asyncio.Semaphore(kol_scheduler_concurrency)`, each KOL isolated (one failure/hang affects only its
  own result), and is guarded by a process-level `asyncio.Lock` so a cycle that overruns the interval
  **declines the next tick** rather than overlapping it (returns `skipped_cycle=True`). Both return
  plain dataclass reports (`KolCaptureResult` / `KolCycleReport`) — no new persisted model, since the
  pipeline already persists everything. Scheduling reuses the `app/main.py` `asyncio.create_task`
  background-loop pattern next to `_token_monitor_loop` (`_kol_scheduler_loop`, `kol_watchlist.sync_from_config`
  once at startup then interval-gates `run_cycle`), gated by `settings.kol_scheduler_enabled`
  (**off by default**). Graceful shutdown reuses the existing lifespan cancel/suppress path; the
  scheduler re-raises `CancelledError` so a shutdown mid-capture cancels cleanly. All cadence/
  concurrency/timeout/retry knobs are config (`kol_scheduler_*` block). Covered by
  `tests/test_kol_scheduler.py` (12 tests: enabled-only + tier order, bounded concurrency actually
  caps parallelism, one KOL failing/hanging never sinks the cycle, retry-then-succeed + backoff,
  non-retryable stops early, incomplete-vs-failed outcome, missing/incapable provider skipped without
  retry, duplicate-run lock declines the overlapping tick, empty roster no-op, graceful cancellation).
- **Files/modules (new, additive):** `app/services/kol_scheduler.py`, `tests/test_kol_scheduler.py`.
  **Touched minimally & additively:** `app/core/config.py` (new `kol_scheduler_*` settings block),
  `app/main.py` (register `_kol_scheduler_loop` next to the existing loops, guarded by the enable flag).
  `kol_watchlist`, the X provider, `kol_monitor`, the crypto pipeline, `kol_intel_engine`, scoring,
  the event pipeline, all APIs, and the frontend are **consumed/untouched, not modified.**
- **Integration points:** (1) `kol_watchlist.capture_following()` — the single reuse seam for the whole
  KOL pipeline; (2) `kol_watchlist.list_kols(enabled_only=True)` — the roster read; (3) the
  `SocialGraphProvider` registry (`get_provider`) for the capture-capability skip check;
  (4) the `main.py` `asyncio.create_task` background-loop + lifespan cancel pattern (no new scheduler
  dependency); (5) pydantic `BaseSettings` for all config.
- **Dependencies:** M23 (A–H, the pipeline being driven). **No new external dependency.**
- **Effort:** Small–Medium (pure orchestration; zero new pipeline logic) · **Risk:** Low (additive;
  off by default; the driven pipeline ships unmodified).
- **Expected improvement:** Makes the M23 leading-social signal **continuous** rather than manual —
  the precondition for cluster/convergence events firing early enough to be actionable. Additive-only.
- **Acceptance criteria:** an enabled roster is captured on a cadence with the whole pipeline running
  per KOL; disabled KOLs are skipped; one KOL's failure/timeout is isolated and retried, never sinking
  the cycle or the loop; concurrency is bounded; a still-running cycle is never double-started; the loop
  is opt-in and off by default; state persists so a restart resumes cleanly; **existing tests stay green
  (zero regression — 472 → 484).**

---

### M26 — Notification Transport Layer (real HTTP sinks for the existing delivery layer) ✅ COMPLETE

- **Goal:** Deliver the intelligence events the KOL engine already produces through real, external
  transports — a generic **webhook**, **Telegram**, and **Discord** — so an alert-worthy convergence /
  cluster / momentum event actually reaches a human channel, not just the log / in-memory sinks M23-H
  shipped. Pure infrastructure: no new intelligence, no scoring/detection/clustering change.
- **Why it matters:** M23-H built the whole dispatch/rule/dedupe/audit machinery but only wired the
  `log` and `memory` sinks; the docs flagged "real transports" as the remaining future work. Without an
  external sink the leading social signal never leaves the process. This is the last hop that turns a
  persisted event into an actual notification.
- **Design rule (non-negotiable):** transports **extend, never redesign.** The existing
  `notifications.dispatch_events` stays the one call-in and the one NotificationManager; the existing
  `NotificationProvider` ABC + `_PROVIDER_FACTORIES` registry + config gating + dedupe + delivery
  logging + failure isolation are all **reused unchanged**. A provider receives a ready-made
  `Notification` (title / body / self-describing payload) and ships it — it knows **nothing** about
  KOLs, rug analysis, scoring, clustering, or snapshot diffing. Retry/backoff is added **once**, in the
  shared `_deliver_one`, so it covers every provider uniformly (log/memory simply never fail).
- **Status:** ✅ **COMPLETE** (2026-07-21).
  _As built:_ three new providers in `app/services/notifications.py` — `WebhookProvider` (generic JSON
  HTTP POST, config-driven extra headers, optional HMAC-SHA256 body signature under a configurable
  header), `TelegramProvider` (Bot API `sendMessage`, Markdown), and `DiscordWebhookProvider` (standard
  incoming webhook with a rich embed). Each does one **synchronous** POST (the dispatcher is sync,
  called inline from the engine) via a short-lived `httpx.Client` with a configurable timeout, and
  **raises on any non-2xx / transport error** so the shared retry + isolation handle it uniformly. Each
  **self-skips** (raises a clean config error, recorded as a `failed` attempt, never a crash) when its
  own required settings (URL / token / chat id) are absent — an enabled-but-unconfigured transport can
  never sink delivery. Retry/backoff added to `_deliver_one` (`notify_retry_count` total tries,
  `notify_retry_delay_seconds` × attempt linear backoff); only the **final** outcome is recorded, so a
  transient failure that later succeeds leaves a single `sent` row and dedupe still applies. All three
  registered in `_PROVIDER_FACTORIES` (names `webhook` / `telegram` / `discord`) — a transport fires
  only when its name is in `notify_providers` **and** `notify_enabled` is on, so the whole layer is
  **inert / zero-overhead** when disabled. Covered by `tests/test_notification_transports.py` (13 tests:
  each provider's payload/headers/HMAC signature/embed shape, self-skip on missing config, raise-on-non-2xx,
  retry-exhaust-then-`failed`, retry-then-`sent`, no-retry-at-count-1, cross-provider isolation, and
  disabled ⇒ no HTTP; `httpx.Client` stubbed so nothing touches the network).
- **Files/modules:** **Touched additively:** `app/services/notifications.py` (three provider classes +
  a shared `_post_json` primitive + retry/backoff in `_deliver_one`; the ABC/registry/dispatch/dedupe/
  audit surface is unchanged), `app/core/config.py` (new `notify_retry_count` / `notify_retry_delay_seconds`
  / `notify_request_timeout_seconds` / `notify_webhook_*` / `notify_telegram_*` / `notify_discord_webhook_url`
  settings, all optional with inert defaults), `tests/test_notification_transports.py` (new). `kol_intel_engine`,
  scoring, detection, clustering, the event pipeline, all APIs, and the frontend are **untouched.**
- **Integration points:** (1) `notifications.dispatch_events()` — the unchanged single call-in from
  `kol_intel_engine._persist_and_emit`; (2) the existing `NotificationProvider` ABC + `_PROVIDER_FACTORIES`
  registry (the documented seam for adding a transport with no producer change); (3) `kol_store.was_delivered`
  / `record_delivery` for dedupe + audit (reused as-is); (4) `httpx` (already a dependency) for the POSTs;
  (5) pydantic `BaseSettings` for all config.
- **Dependencies:** M23-H (the dispatch/rule/dedupe/audit layer these transports plug into). **No new
  external dependency** (`httpx` already ships).
- **Effort:** Small–Medium (three thin transports + shared retry; zero new intelligence) · **Risk:** Low
  (additive; off by default; the dispatch layer + all producers ship unmodified).
- **Expected improvement:** Closes the last gap between "event persisted" and "human notified" — the M23
  signal can now reach webhook / Telegram / Discord. Infrastructure-only; no detection change.
- **Acceptance criteria:** each transport delivers a correctly-shaped payload to its endpoint; a missing
  config self-skips without crashing; a non-2xx / transport error is retried per policy then recorded
  `failed`; one transport failing never blocks the others; the layer is opt-in and adds zero overhead when
  disabled; **existing tests stay green (zero regression — 484 → 497).**

---

### M27 — Watchlist Alerts & Intelligent Notifications (events → configurable rules → delivery) ✅ COMPLETE

- **Goal:** Turn the events the system already produces into **configurable alerts**. The token-monitor
  (M24) emits change events but never delivered them; KOL follow events (M23) were likewise produced but
  never delivered. M27 adds a rule engine that decides *whether* an existing event should notify — per
  alert type, per token, with cooldown / dedupe / severity / aggregation — and delivers the survivors
  through the EXISTING notification providers (M23-H/M26). No new intelligence, no scoring change.
- **Why it matters:** M24/M25 made the analyzer watch continuously and M26 gave it real transports, but
  nothing connected the *watchlist* change stream to those transports, and every alert would have been
  all-or-nothing. Operators need to say "critical honeypot flips always, risk wobble never, and not more
  than once an hour per token." That policy layer is M27.
- **Design rule (non-negotiable):** the engine **connects, never regenerates.** It consumes the existing
  `MonitorEvent` / `FollowEvent` / `KolIntelEvent` objects verbatim (each already carries `event_type` +
  a self-describing `payload`), maps each to one of ten alert types, applies the rule, renders a
  human-readable message, and hands a `Notification` to `notifications.deliver` — the ONE delivery path
  (providers + retry + dedupe + audit), so no transport code is duplicated. No scoring, detection,
  clustering, or event-generation logic is touched.
- **Status:** ✅ **COMPLETE** (2026-07-21).
  _As built:_ `app/models/alerts.py` — the ten-type `ALERT_TYPES` vocabulary, `SEVERITY_LEVELS`
  (critical→info), `AlertRule` (enabled / severity / cooldown), `AlertConfig` (global defaults +
  per-token overrides with `rule_for` precedence per-token > global > built-in), and the rendered
  `Alert`. `app/services/alert_engine.py` — `EVENT_TO_ALERT` maps each existing event type to an alert
  type; `evaluate(events, subject, …)` is pure (event→alert with enable + severity-gate + per-token
  override + optional aggregation into one summary alert); `dispatch(alerts)` applies cooldown (via the
  persisted delivery log, so it survives restart) + dedupe and delivers through the reused providers;
  `process_monitor_result` / `process_follow_events` are the additive, never-raising wiring hooks. To
  give the concentration / smart-wallet / privilege alert types a live source, `MonitorSnapshot` gained
  three fields copied VERBATIM from the reused analysis (`top10_concentration`, `smart_wallet_count`,
  `privilege_signature`) + their change events — no new computation. `notifications.py` was refactored to
  expose one generic `deliver(notification, name)` (the KOL path now delegates to it) so the alert engine
  reuses the exact retry/dedupe/audit machinery. Wired into `token_monitor.run_cycle` (per-token, isolated)
  and `kol_watchlist.capture_following` (new follows). Everything gated by `settings.alerts_enabled`
  (**off by default**, zero overhead when off). Covered by `tests/test_alert_engine.py` (20 tests:
  event→alert mapping incl. the three new monitor sources, new-KOL-follow via FollowEvent, non-alertable
  events skipped, global disable, per-token override beats global, per-token severity raise, severity gate,
  dedupe, cooldown suppress + cooldown-zero repeats, aggregation on/off, disabled no-op, monitor-result
  wiring, bad-provider isolation, no-providers no-op).
- **Files/modules (new):** `app/models/alerts.py`, `app/services/alert_engine.py`,
  `tests/test_alert_engine.py`. **Touched additively:** `app/core/config.py` (`alerts_*` block),
  `app/models/monitor.py` (+3 reused-scalar fields + 3 event types), `app/services/token_monitor.py`
  (populate the 3 fields + a gated alert call in the cycle), `app/services/kol_watchlist.py` (a gated
  alert call on new follows), `app/services/notifications.py` (extract the generic `deliver` — behaviour
  identical, the KOL path delegates). Scoring, detection, clustering, the event pipeline, all APIs, and
  the frontend are **untouched.**
- **Integration points:** (1) existing event producers (`MonitorEvent`, `FollowEvent`) — consumed verbatim;
  (2) `notifications.deliver` + providers — the reused delivery path; (3) `kol_store` delivery log — reused
  for dedupe + cooldown; (4) pydantic `BaseSettings` for all rule config.
- **Dependencies:** M23-H/M26 (notification providers), M24 (token-monitor events), M23 (follow events).
  **No new external dependency.**
- **Effort:** Medium (rule engine + config + wiring; zero new intelligence) · **Risk:** Low (additive; off
  by default; the delivery layer + producers ship behaviourally unchanged).
- **Expected improvement:** Makes continuous monitoring *actionable* — the watchlist change stream and new
  KOL follows now reach webhook/Telegram/Discord under operator-tuned rules, instead of dying as internal
  events. Infrastructure-only; no detection change.
- **Acceptance criteria:** each of the ten alert types is configurable (enable/disable, severity, cooldown)
  with per-token overrides beating global defaults; existing events drive alerts with no new event
  generation; cooldown + dedupe suppress repeats; multiple alerts aggregate; messages are human-readable;
  one bad provider never blocks the others; the engine is opt-in and zero-overhead when disabled;
  **existing tests stay green (zero regression — 497 → 517).**

---

## Prioritized checklist (highest ROI → lowest)

ROI = detection/user value per unit effort-and-risk. Enablers rank high because they unblock everything downstream.

1. **[M1] HTTP response caching** — Small, unblocks all request-heavy work. Highest ROI.
2. **[M8] Registry population** — Small, pure data, upgrades multiple signals to high-confidence. Near-free.
3. **[M3] Real token age** — Small, fixes a systematic false-negative on the riskiest (pre-liquidity) tokens.
4. **[M4] Contract filtering** — Small, kills a recurring false positive (router/LP flagged as insider #1).
5. **[M5] Union-find re-keying** — Small, prevents silently dropped clusters (false negative).
6. **[M6] Signal semantics fix** — Small, corrects a signal pointing the wrong way.
7. **[M7] Confidence scoring (data-completeness)** — Small, lets users distinguish "clean" from "couldn't see."
8. **[M2] Scan tiering + concurrency semaphore** — Medium, makes scan-of-50 viable; prerequisite for heavy analysis.
9. **[M9] Honeypot / sell simulation** — Large, flagship; the single biggest false-negative killer.
10. **[M10] Contract-privilege reads** — Medium–Large, flagship; "what can the dev still do to you."
11. **[M11] Liquidity/mcap ratio + sell impact** — Small–Medium, catches thin-float pumps that pass absolute checks.
12. **[M12] Full holder set** — Medium, deeper concentration/cluster accuracy.
13. **[M18] Persistent deployer reputation** — Medium, compounding moat; serial ruggers stay flagged.
14. **[M17] Persistent wallet reputation** — Medium, compounding; feeds `watchlist_hits`.
15. **[M13] LP lock duration** — Medium–Large, time-aware lock signal (needs RPC).
16. **[M14] Funder-graph depth / bundler detection** — Medium–Large, catches sybil launches.
17. **[M15] Same-block coordinated-buy detection** — Medium, mostly analysis on data we already fetch.
18. **[M16] Smart-wallet cross-token activation** — Medium–Large, activates dead path (blocked on API probe).
19. **[M19] Snapshots + trend detection** — Medium, enables time-series signals.
20. **[M20] Technical-debt cleanup** — Small, hygiene.
21. **[M21] Watchlist improvements** — Small–Medium, usability.
22. **[M22] Multi-chain** — Large, deferred; breadth not depth.
23. **[M23] KOL Intelligence (X follow-graph)** — Large, new leading/social+alpha dimension; cluster events are the payoff. Reuses the analyze pipeline, so effort is sourcing+scheduling+scoring, not re-analysis.
24. **[M24] Token Watchlist & Monitoring** — Medium, adds a temporal (change-over-time) dimension by re-running the analyze pipeline on a schedule and recording deltas. Pure orchestration+persistence; reuses the analyzer and KOL intel, so no new analysis and low risk.

---

## Version plan

### v1.1 — Enablers + cheap accuracy (foundation)
Ship the plumbing and the low-risk correctness fixes. No new infra, immediate FP/FN reduction.
- M1 HTTP caching
- M2 Scan tiering + concurrency semaphore
- M3 Real token age
- M4 Contract filtering
- M5 Union-find re-keying
- M6 Signal semantics
- M7 Confidence scoring
- M8 Registry population
- M20 Technical-debt cleanup

**Theme:** faster, more honest, fewer false positives — before adding any new data source.

### v1.2 — Liquidity & holder depth
Deeper versions of what we already do, plus the cheapest new market signal.
- M11 Liquidity/mcap ratio + sell impact
- M12 Full holder set
- M15 Same-block coordinated-buy detection
- M21 Watchlist improvements

**Theme:** sharper on concentration, coordination, and thin-float risk. **Blocker to clear first:** M1/M2 must be in (these add requests).

### v1.5 — The behavior gap (flagship)
Build the RPC layer once; light up simulation and privilege analysis. This is the best-in-class jump.
- **Blocker (solve first):** probe the Robinhood-Chain public RPC for reliable `eth_call` at acceptable rates. If flaky, budget the upper time bounds or gate behind a configurable RPC.
- M9 Honeypot / sell simulation
- M10 Contract-privilege reads
- M13 LP lock duration
- M14 Funder-graph depth / bundler detection

**Theme:** read contract *state and behavior*, not just metadata.

### v2.0 — Compounding intelligence + scale
Persistent, growing reputation that turns usage into a moat.
- **Blocker (solve first):** confirm a per-wallet token-holdings endpoint exists for this chain (gates M16).
- M16 Smart-wallet cross-token activation
- M17 Persistent wallet reputation
- M18 Persistent deployer reputation
- M19 Snapshots + trend detection
- M22 Multi-chain *(optional, only if product direction shifts to breadth)*
- M23 KOL Intelligence (X follow-graph) *(new leading/social+alpha dimension; **blocker:** provision a persistent X session for Playwright)*
- M24 Token Watchlist & Monitoring *(temporal change-over-time dimension; re-runs the analyze pipeline on a schedule and records deltas — pure orchestration, reuses M10 + M23, no new infra)*

**Theme:** the analyzer gets smarter the more it's used — on-chain reputation that compounds, plus a leading social signal (M23) that fires before liquidity exists and a standing watch (M24) that catches a token *changing* after the first look.

---

## Blocker summary (solve before the dependent milestone)

| Blocker | Gates | How to clear |
|---|---|---|
| No caching / concurrency cap | M9–M19 (all request-heavy) | Ship M1 + M2 first |
| ~~RPC `eth_call` state-override support~~ | M10 | **Cleared 2026-07-16:** Nitro node supports overrides (see M10 probe result) |
| DEX router address/ABI unknown for this chain | M10, M13 | Config-gated router map; sim inert until a router is sourced |
| Per-wallet holdings endpoint unconfirmed | M16 | API probe against Blockscout for this chain |
| Locker registry empty | M13 (+ M8 quality) | Populate confirmed locker addresses |
| Labeled rug/non-rug dataset absent | M7 weight back-testing | Accumulate via M19 snapshots, then calibrate |
| No authenticated X session for scraping | M23 | Provision persistent cookies out-of-band; store the browser context on disk for reuse |

---

*End of roadmap. Living document — revise as milestones land and as RPC/API probes resolve the open assumptions above.*
