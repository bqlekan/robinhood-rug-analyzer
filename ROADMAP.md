# Robinhood Rug Analyzer — Implementation Roadmap

_Last updated: 2026-07-16. Reflects the codebase after Phases 1–3 (address validation,
frontend XSS hardening, shared HTTP client + scan efficiency, smart-wallet honesty fix)._

This roadmap groups all remaining work into dependency-ordered milestones, highest ROI
first. Effort is Small (<1 day), Medium (1–3 days), or Large (1 week+). Detection Δ is the
expected lift in catching real rugs / avoiding false calls.

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

### M10 — Honeypot / sell-tax simulation (flagship)

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
  - **C. Route M9 creation-tx retrieval through the client** (carried from M9): prefer RPC
    (`eth_getTransactionByHash` / `eth_getTransactionReceipt`) and **fall back to the current
    Blockscout path** (`blockscout_client.get_transaction` / `get_transaction_logs`) when RPC
    is unavailable or errors. M9's launchpad detection consumes whichever source succeeds; the
    registry-driven matching (`match_creation_evidence`) is source-agnostic and needs no
    change. See the marker at the M9 creation-evidence block in `rug_analyzer.py`.
- **Files/modules:** new JSON-RPC client module, new simulation module, `rug_analyzer.py`
  (wire result), `scoring.py` (new signals), `models/token.py`, `frontend/app.js`.
- **Dependencies:** M1 (cache — one sim per analyze, cached). Builds its own RPC client (A);
  does not depend on M9, which shipped on Blockscout.
- **Effort:** Large · **Risk:** High (correctness of simulation; chain-specific router ABI).
- **Expected improvement:** Catches unsellable/high-tax rugs metadata can't see.
  Detection Δ: Very High.
- **Acceptance criteria:**
  - Known-sellable token → sellable; simulated honeypot → flagged with a high-severity signal.
  - Simulation is bounded (one per analyze) and cached; RPC failure degrades to "could not
    simulate," never a crash or false "safe."
- **Suggested tests:** sim result → signal mapping (pure); failure path yields explicit
  unknown, not a clean score.

---

### M11 — Contract-privilege / authority reads

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

### M12 — Full holder set (paged) instead of one sampled page

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

### M13 — LP lock duration & unlock schedule

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

### M14 — Funder-graph depth & bundler detection

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

### M15 — Same-block / coordinated-buy timing detection

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

### M16 — Smart-wallet cross-token implementation (activate the dead path)

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

### M17 — Persistent wallet reputation (cross-token memory)

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

### M18 — Persistent deployer reputation

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

### M19 — Historical scan snapshots + trend detection

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

### M20 — Technical-debt cleanup

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

### M21 — Watchlist improvements

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

### M22 — Future multi-chain architecture

**Goal:** (Deferred) Re-introduce multi-chain support if product direction changes.

**Why it matters:** The app was deliberately narrowed to single-chain (deleted `blockchain_detector.py`, dead `*SCAN_API_KEY` vars). Genuine multi-chain is a large re-architecture: chain-parameterized clients, per-chain registries, per-chain RPC. Depth on one chain (Tiers above) beats breadth for detection quality, so this is last.

**Files/modules likely to change:** Broad — `app/core/config.py` (chain registry), all clients (`blockscout_client`, `dexscreener_client`, RPC layer), `launchpad_registry` (per-chain), `rug_analyzer`, models, frontend chain selector.

**Dependencies:** Everything. Only start after v1.x depth features are stable. **Blocker:** an explicit product decision that breadth is wanted over further depth.

**Effort:** Large (weeks) · **Risk:** High (touches every layer; regresses single-chain focus if rushed).

**Expected improvement:** Breadth, not detection depth. Detection Δ Low per-chain.

**Acceptance criteria:** A second chain can be added via config without forking logic; single-chain behavior unchanged when only one chain is configured.

**Suggested tests:** Chain-parameterized fixtures; single-chain regression suite stays green.

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

**Theme:** the analyzer gets smarter the more it's used, on a single chain, with no external data dependency.

---

## Blocker summary (solve before the dependent milestone)

| Blocker | Gates | How to clear |
|---|---|---|
| No caching / concurrency cap | M9–M19 (all request-heavy) | Ship M1 + M2 first |
| RPC `eth_call` reliability unknown | M10, M11, M13 | Probe public RPC; make RPC URL configurable |
| Per-wallet holdings endpoint unconfirmed | M16 | API probe against Blockscout for this chain |
| Locker registry empty | M13 (+ M8 quality) | Populate confirmed locker addresses |
| Labeled rug/non-rug dataset absent | M7 weight back-testing | Accumulate via M19 snapshots, then calibrate |

---

*End of roadmap. Living document — revise as milestones land and as RPC/API probes resolve the open assumptions above.*
