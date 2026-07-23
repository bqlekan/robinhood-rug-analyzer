# Frontend Roadmap — Robinhood Rug Analyzer

_Planning document. Defines milestones **F1–F13** for a dedicated frontend that
exposes the **already-completed** backend (M1–M27). This roadmap is independent of
the backend roadmap in [`../ROADMAP.md`](../ROADMAP.md) and adds **no** new
intelligence, scoring, detection, or analysis. Companion: [`ARCHITECTURE.md`](./ARCHITECTURE.md),
[`DATA_FLOW.md`](./DATA_FLOW.md)._

_Last updated: 2026-07-21. Nothing here is implemented yet — this is scope only._

---

## Guiding principles (non-negotiable)

1. **Expose, never reinvent.** Every frontend feature maps to an existing backend
   capability. The frontend renders, filters, and navigates data the backend already
   produces. It does no scoring, detection, clustering, or analysis of its own.
2. **No new backend intelligence.** Where a page needs data that the backend already
   *computes/stores internally* but does not yet *expose over HTTP* (KOL intelligence,
   token-monitor watchlist, alerts/notifications, diagnostics), the only backend work
   permitted is a **thin, read-only endpoint that surfaces existing state** — never new
   logic, scoring, or a schema change to the analysis pipeline. Every such endpoint is
   flagged **[NEW READ-ONLY ENDPOINT]** in its milestone.
3. **Secrets never leave the server.** No config surface ever returns API keys,
   bot tokens, webhook secrets/URLs, session paths, or raw env values (see the
   redaction rule in F10/F13).
4. **Additive to the current UI.** The existing static UI (`frontend/index.html`,
   `app.js`, `styles.css`) is the starting point; F1 establishes structure and the
   later milestones grow it. No backend file under `app/services/` that performs
   analysis is modified for presentation reasons.

---

## Backend API surface as it exists today

The frontend has exactly this to build on (verified against `app/api/routes.py`
and `app/main.py`):

| Method / Path | Returns | Notes |
|---|---|---|
| `GET /health` | `{status, app, version}` | Liveness probe (app-level). |
| `GET /api/v1/chain` | chain name/id, explorer URL, DexScreener chain | Active `ChainConfig` (M22). |
| `POST /api/v1/analyze` | `TokenAnalysisResponse` | Full rug-risk analysis of one token. |
| `POST /api/v1/scan` | `ScanResponse` (ranked tokens) | Risk-ranked scan. |
| `GET /api/v1/watchlist?kind=&sort=` | `WatchlistResponse` (smart + insider wallets) | M21 filter/sort. |
| `POST /api/v1/watchlist/refresh` | `{refreshed}` | On-request wallet refresh (M21). |
| `GET /api/v1/wallet/{address}` | `WatchlistEntry` | One tracked wallet's detail. |
| `GET /api/v1/history/{address}?limit=` | `{contract_address, snapshots}` | Stored trend snapshots (M19). |
| `GET /docs`, `/redoc`, `/openapi.json` | FastAPI auto docs / schema | Enabled by default. |

**Not exposed over HTTP today** (built + persisted internally, no route): KOL
intelligence (`kol_store`, `ProjectIntelligence`, clusters, follow/crypto/intel
events), token-monitor watchlist (`token_monitor` / `token_monitor_store`), alert
rules + `notification_deliveries`, cache state, scheduler/background-task state,
logs, runtime config, DB statistics. Milestones that need these carry a
**[NEW READ-ONLY ENDPOINT]** flag.

---

## F1 — Frontend Foundation & Architecture

**Objective:** Establish a maintainable frontend structure, a single API client, a
design system, and shared UI primitives — the base every later milestone builds on.
Preserve the current same-origin, static-served model (no separate frontend server;
FastAPI keeps mounting the UI at `/`).

**Deliverables:**
- Modular structure under `frontend/` (e.g. `frontend/js/` for ES modules, `frontend/css/` for split stylesheets, `frontend/pages/` or a hash/History router) — vanilla ES modules, **no framework mandated**; if a build step is ever added it must still emit static assets FastAPI can serve.
- A single `apiClient` module wrapping every endpoint in the table above (typed request/response helpers, one place for base URL, errors, and the shared retry/progress conventions already in `app.js`).
- Design tokens (colors, spacing, typography) extracted from the current `styles.css` into CSS custom properties; a shared component set: buttons, cards, tabs, progress bar, skeletons, toasts (reuse the existing progress/skeleton/`esc()`/`safeUrl()` code).
- Client-side router + app shell (header, nav, panel container) reproducing today's tabbed navigation.
- Keep `esc()` / `safeUrl()` XSS discipline as shared utilities.

**Acceptance Criteria:**
- The app still loads at `/`, served by FastAPI StaticFiles, no build required to run.
- Every existing feature (scan, analyze, wallets) works through the new `apiClient`.
- No backend change; `node --check` (or the chosen linter) clean; existing behavior preserved.

**Files expected to change:** `frontend/index.html`, `frontend/app.js` → split into `frontend/js/*` modules (`api.js`, `router.js`, `ui.js`, feature modules), `frontend/css/*`. No `app/` changes.

---

## F2 — Dashboard

**Objective:** A landing overview that summarizes chain status and the most
recent/at-risk activity, aggregating data the backend already returns.

**Deliverables:**
- Chain banner from `GET /api/v1/chain` (name, id, explorer link) + liveness dot from `GET /health`.
- "Top risk" panel from `POST /api/v1/scan` (a small default limit), each row linking into F3.
- Watchlist snapshot counts from `GET /api/v1/watchlist` (number of smart / insider wallets).
- Quick-analyze input that deep-links to F3.

**Acceptance Criteria:**
- Dashboard renders entirely from existing endpoints; no invented metrics.
- All numbers trace to a real response field; loading/skeleton/error states from F1.

**Files expected to change:** `frontend/js/pages/dashboard.js`, `frontend/index.html` (route/nav), `frontend/css/*`. No `app/` changes.

---

## F3 — Token Analysis

**Objective:** The full single-token drill-down — render **every** field of
`TokenAnalysisResponse` clearly, plus the token's stored trend history.

**Deliverables:**
- Analyze form (address + include-lore) → `POST /api/v1/analyze`, reusing the staged progress bar.
- Complete rendering of the response: risk score/level/confidence, market data, holders/concentration, clusters, bundle, buy-timing, dev/deployer detail, liquidity lock, launchpad, honeypot, contract intel + privileges, insiders, watchlist hits, lore, limitations, trend.
- Trend/history view from `GET /api/v1/history/{address}` (snapshot list; simple sparkline of risk/liquidity over stored snapshots).
- Token action row (copy / Blockscout / DexScreener via `GET /api/v1/chain`), recent searches (localStorage) — reuse the F1 primitives.

**Acceptance Criteria:**
- Every non-null field in a real `/analyze` response is displayed or intentionally summarized; nothing silently dropped.
- History view reads only `/history/{address}`; empty history degrades gracefully.
- No scoring or field re-computation client-side.

**Files expected to change:** `frontend/js/pages/token.js`, `frontend/js/render/*`, `frontend/css/*`. No `app/` changes.

---

## F4 — Smart Wallet Intelligence

**Objective:** Surface the wallet watchlist and per-wallet detail already exposed by
the API, with the discovered-token navigation the polish pass introduced.

**Deliverables:**
- Watchlist view from `GET /api/v1/watchlist?kind=&sort=` (kind filter, score/recency sort) — smart vs insider groups.
- Per-wallet detail from `GET /api/v1/wallet/{address}` (flag, proxy score, prior-tokens, recent buys).
- "Refresh from chain" via `POST /api/v1/watchlist/refresh`.
- Each discovered token clickable → F3 (reuse existing contract, no extra lookup); copy / Blockscout / DexScreener actions.
- Honest empty states + the heuristic-not-ROI disclaimer already returned in `note`.

**Acceptance Criteria:**
- All wallet data comes from the three existing watchlist/wallet endpoints.
- Filter/sort round-trip to the server (server whitelists them); no client-side re-ranking that changes meaning.

**Files expected to change:** `frontend/js/pages/wallets.js`, `frontend/css/*`. No `app/` changes.

---

## F5 — KOL Intelligence

**Objective:** A read-only view of the KOL Intelligence the M23 engine already
computes and persists (watched KOLs, project intelligence, clusters, follow/intel
event timelines). **The engine is complete; only its HTTP surface is missing.**

**Deliverables:**
- **[NEW READ-ONLY ENDPOINT]** thin GET routes that surface existing `kol_store` reads **only** — e.g. `GET /api/v1/kol/kols` (list watched KOLs), `GET /api/v1/kol/projects` + `/api/v1/kol/projects/{platform}/{account_key}` (stored `ProjectIntelligence`), `GET /api/v1/kol/events` (intel/follow event timeline), `GET /api/v1/kol/clusters`. These call existing `kol_store.list_*` / `get_project_intelligence` functions verbatim — **no scoring, no capture trigger, no new computation**, gated so they no-op/empty when the KOL engine is disabled.
- KOL roster view (tier, enabled, last capture) and per-project intelligence view (score, confidence, contributors, cluster, evidence, timeline) rendered read-only.
- Convergence/cluster timeline visualization from the stored history rows.

**Acceptance Criteria:**
- Zero new intelligence: the endpoints are pure reads of already-persisted rows; scoring/clustering logic is untouched.
- No secrets (X session paths, provider internals) are ever returned.
- Page degrades to an "engine disabled / no data" state when `kol_intel_enabled` is off.

**Files expected to change:** `app/api/routes.py` (or new `app/api/kol_routes.py`) — read-only routes only; `frontend/js/pages/kol.js`, `frontend/css/*`. No change to `kol_store`, `kol_intel_engine`, `kol_scoring`, or any analysis logic.

---

## F6 — Watchlists

**Objective:** One place to view both watchlist domains — the **wallet** watchlist
(already exposed) and the **token-monitor** watchlist (M24, internal today).

**Deliverables:**
- Wallet watchlist management surface (reuses F4 endpoints).
- **[NEW READ-ONLY ENDPOINT]** token-monitor read routes surfacing existing `token_monitor` / `token_monitor_store` state — e.g. `GET /api/v1/monitor/tokens` (watchlist entries + status), `GET /api/v1/monitor/history/{address}` (change events already stored). Pure reads of existing tables; **no** trigger of `run_cycle`, no analysis.
- Combined view: monitored tokens, last-checked status, recent change events; deep-link each token to F3.

**Acceptance Criteria:**
- Token-monitor data is read-only surfacing of existing rows; the scheduler and change-detection logic are untouched.
- Empty/disabled states when `token_monitor_enabled` is off.

**Files expected to change:** `app/api/routes.py` (or `app/api/monitor_routes.py`) — read-only; `frontend/js/pages/watchlists.js`, `frontend/css/*`. No change to `token_monitor` logic.

---

## F7 — Alerts & Notifications

**Objective:** View the alert rules (config) and the delivery history the
notification layer already records — read-only.

**Deliverables:**
- **[NEW READ-ONLY ENDPOINT]** `GET /api/v1/alerts/config` (the resolved, **secret-redacted** alert rules + which providers are enabled by *name only*) and `GET /api/v1/alerts/deliveries` (surfacing `kol_store.list_deliveries` — event type, destination *name*, status, timestamp; **never** webhook URLs, bot tokens, chat ids, or secrets).
- Alert-rules viewer (per-type enable/severity/cooldown, per-token overrides) rendered read-only from the config surface.
- Delivery log table with filtering by status/type/destination-name.

**Acceptance Criteria:**
- No secret ever appears: provider config shows enabled/disabled + provider *name*, never the URL/token/secret/chat-id values.
- Rules are displayed, not editable (editing config stays an operator/env action).
- Deliveries are a pure read of the existing audit table.

**Files expected to change:** `app/api/routes.py` (or `app/api/alerts_routes.py`) — read-only + redaction; `frontend/js/pages/alerts.js`, `frontend/css/*`. No change to `alert_engine` / `notifications` logic.

---

## F8 — Analytics

**Objective:** Time-series and aggregate views built purely from stored snapshots
and scan results — no new metrics, only visualization of existing data.

**Deliverables:**
- Per-token trend charts from `GET /api/v1/history/{address}` (risk score, liquidity, top-10 concentration, holder count over stored snapshots).
- Scan-distribution view from `POST /api/v1/scan` (risk-level histogram, top signals) computed as pure client-side aggregation of returned rows (presentation only, not scoring).
- Charts follow the `dataviz` accessibility/palette conventions.

**Acceptance Criteria:**
- Every series maps to a stored field; no derived "score" the backend didn't produce.
- Client-side aggregation is limited to counting/bucketing already-scored rows.

**Files expected to change:** `frontend/js/pages/analytics.js`, `frontend/js/charts/*`, `frontend/css/*`. No `app/` changes (reuses `/history` + `/scan`).

---

## F9 — Global Search

**Objective:** A single entry point to jump to any token or wallet by address,
reusing existing lookups.

**Deliverables:**
- Address-aware search box: an EVM token address routes to F3 (`/analyze`), a watchlisted wallet address routes to F4 (`/wallet/{address}`).
- Recent searches (localStorage, from the existing polish work) and quick suggestions from the current watchlist/scan results already in memory.
- Keyboard-first (focus shortcut, arrow nav, Enter to go).

**Acceptance Criteria:**
- Search performs no new backend query type — it only routes to `/analyze` or `/wallet/{address}`.
- Invalid input is rejected client-side with a clear message (reuse `is_valid_address`-equivalent shape check; the server remains the authority).

**Files expected to change:** `frontend/js/search.js`, `frontend/js/router.js`, `frontend/css/*`. No `app/` changes.

---

## F10 — Settings

**Objective:** Client-side preferences plus a **safe, read-only** view of runtime
context. No secret ever displayed.

**Deliverables:**
- Client preferences (localStorage): theme, default include-lore, default scan limit, recent-search management, reduced-motion respect.
- Read-only chain/context panel from `GET /api/v1/chain` + `GET /health` (app name, version, chain).
- Optional link to the F13 config viewer (advanced mode) — which itself only shows redacted, non-secret config.

**Acceptance Criteria:**
- Preferences persist locally and never touch the backend.
- The context panel shows only already-public `/chain` + `/health` fields; no env values, no secrets.

**Files expected to change:** `frontend/js/pages/settings.js`, `frontend/js/prefs.js`, `frontend/css/*`. No `app/` changes.

---

## F11 — Mobile & Responsive Design

**Objective:** Make every page fully usable on phones/tablets, extending the mobile
polish already begun.

**Deliverables:**
- Responsive layouts for dashboard, token analysis grid, wallet/KOL lists, tables (horizontal scroll or card-stack), charts, and the admin console.
- Touch-friendly controls, stacked forms, no horizontal overflow, progress bars that resize.
- Respect `prefers-reduced-motion` and `prefers-color-scheme`.

**Acceptance Criteria:**
- No horizontal overflow at 320px width on any page; all actions reachable by touch.
- Tables scroll or stack; charts remain legible.

**Files expected to change:** `frontend/css/*` (responsive rules), minor `frontend/js/*` layout tweaks. No `app/` changes.

---

## F12 — Production Polish

**Objective:** Harden UX across the whole app — errors, loading, accessibility,
performance — extending the earlier polish pass to every new page.

**Deliverables:**
- Consistent loading (progress/skeleton), empty, and error states on every page; global error/toast surface; never-stuck-loading guarantee.
- Duplicate-request prevention on all actions (per-action in-flight guards, as already done for analyze/scan/wallets).
- Accessibility sweep: ARIA roles, keyboard nav, focus indicators, labelled controls, live regions across all pages.
- Performance: lazy-load heavy pages/charts, cache `GET /api/v1/chain` client-side, debounce search.

**Acceptance Criteria:**
- Every page passes a basic a11y/keyboard pass; no page can be left in a stuck-loading state.
- No duplicate concurrent requests from any control.

**Files expected to change:** `frontend/js/*`, `frontend/css/*`. No `app/` changes.

---

## F13 — Admin & Diagnostics Console

**Objective:** A developer/administrator page that surfaces **existing** backend
information for diagnostics. **Hidden behind an "Advanced/Developer Mode" toggle**
(off by default; a localStorage flag) and never in the normal user flow. It exposes
existing state only — **no new intelligence, no scoring, read-only except explicitly
safe actions.**

**Hard rules (apply to every panel below):**
- Never expose secrets, API keys, private/bot tokens, webhook secrets or URLs, chat ids, session paths, passwords, or raw environment-variable values.
- Read-only, with the single explicitly-safe exception of manual cache clearing (a bounded, non-destructive operational action).
- Reuse existing endpoints where they exist; otherwise a **[NEW READ-ONLY ENDPOINT]** that surfaces already-computed state. No panel adds scoring/detection/analysis.

**Deliverables (by panel — each notes what exists today vs. what needs a thin read-only endpoint):**

- **API Explorer** — reuse `GET /openapi.json` + `/docs`; render every endpoint, allow executing requests, show formatted JSON. *(Exists today — pure client over the OpenAPI schema.)*
- **Documentation Links** — static links to `ROADMAP.md`, `ARCHITECTURE.md`, `DATA_FLOW.md`, `FRONTEND_ROADMAP.md`, and `/docs`. *(Exists — static.)*
- **System Health** — backend health (`/health`), current chain (`/chain`); **[NEW READ-ONLY ENDPOINT]** `GET /api/v1/admin/health` surfacing existing flags/state: scheduler enabled-flags (`watchlist_refresh_enabled`, `token_monitor_enabled`, `kol_scheduler_enabled`, `alerts_enabled`, `notify_enabled`), RPC URL **host only** (redacted), registered social providers (names), notification providers (names). Booleans/names only — no values.
- **Cache Inspector** — **[NEW READ-ONLY ENDPOINT]** `GET /api/v1/admin/caches` surfacing each `TTLCache`'s configured TTL and current `len()`/`max_size` (the objects already exist: static, market, honeypot, KOL dedup). Plus one **explicitly-safe** `POST /api/v1/admin/caches/clear` (calls existing `TTLCache.clear()`; non-destructive). No cache *contents* are dumped (may contain addresses/payloads — surface counts only).
- **Configuration Viewer** — **[NEW READ-ONLY ENDPOINT]** `GET /api/v1/admin/config` returning a **redacted allow-list** of non-secret settings (thresholds, TTLs, limits, enabled-flags, chain identity). A hard-coded deny-list drops every secret-bearing field (`*_api_key`, `*_secret`, `*_token`, `notify_webhook_url`, `notify_webhook_headers`, `notify_discord_webhook_url`, `notify_telegram_chat_id`, `x_user_data_dir`, `rpc_url` shown host-only). Never returns raw env.
- **Watchlist Diagnostics** — reuses F6's monitor read routes + the wallet watchlist; shows active watchlists, scheduler enabled-state (from admin/health), last execution / last-checked timestamps already stored, entry counts. *(Read-only surfacing of existing store rows.)*
- **KOL Diagnostics** — reuses F5's KOL read routes: registered providers (names), session status (a boolean/`can_fetch` capability, **never** cookies/paths), last capture time + snapshot counts from `kol_store`. *(Read-only.)*
- **Smart Wallet Diagnostics** — reuses the watchlist endpoints + counts: watchlist size, historical analysis count (from `snapshot_store` counts). *(Read-only aggregation of existing stores.)*
- **Logs Viewer** — **[NEW READ-ONLY ENDPOINT]** `GET /api/v1/admin/logs?level=&q=&limit=` tailing the existing rotating `logs/app.log` (the file already exists via `logging_config`), with level filter + text search + a bounded line cap. Read-only; the endpoint must scrub nothing beyond what the app already logs (the app does not log secrets).
- **Performance Dashboard** — surfaces timing/latency/cache-hit metrics. **These are NOT collected today.** This panel requires lightweight, read-only **instrumentation plumbing** (request-timing middleware, per-stage `perf_counter` spans, `TTLCache` hit/miss counters, RPC/provider latency counters) exposed via **[NEW READ-ONLY ENDPOINT]** `GET /api/v1/admin/metrics`. This is measurement plumbing only — **no scoring/intelligence change** — and is explicitly the largest net-new piece; it is scoped last within F13 and may ship incrementally (health/cache panels first, metrics after instrumentation lands).
- **Network Monitor** — a **client-side** live log of the API requests the frontend itself makes (duration, HTTP status, payload size), captured in the F1 `apiClient`. *(No backend endpoint needed — the browser observes its own traffic.)*
- **Database Inspector** — **[NEW READ-ONLY ENDPOINT]** `GET /api/v1/admin/db` returning per-store table names, row counts, and on-disk file sizes for the existing sqlite stores (`watchlist.db`, `kol.db`, `snapshots.db`, `token_monitor.db`) via `COUNT(*)` / file `stat`. Counts + sizes only — **no row contents** (rows contain addresses/payloads).

**Acceptance Criteria:**
- The console is unreachable unless Advanced/Developer Mode is explicitly enabled; normal users never see it and it never alters their workflows.
- Every panel is read-only except `POST /api/v1/admin/caches/clear`; no panel introduces scoring, detection, or analysis.
- Redaction is enforced server-side by allow-list/deny-list — a new secret-bearing setting is redacted by default, not accidentally exposed.
- No secret, key, token, URL-with-credential, chat id, session path, or raw env value is ever returned by any admin endpoint (covered by tests).
- All "exists today" panels work with zero backend change; each **[NEW READ-ONLY ENDPOINT]** only calls existing store/config/log/cache accessors.

**Files expected to change:** new `app/api/admin_routes.py` (read-only diagnostics + the single safe cache-clear + redaction allow/deny list), optional lightweight instrumentation in `app/services/cache.py` (hit/miss counters) / `app/main.py` (timing middleware) / clients (latency counters) **for the Performance panel only**; `frontend/js/pages/admin/*`, `frontend/js/advancedMode.js`, `frontend/css/*`; tests `tests/test_admin_routes.py` (redaction + read-only guarantees). No change to any analysis, scoring, KOL, alert, or monitor logic.

---

## Milestone summary

| Milestone | Backend dependency | New read-only endpoints? |
|---|---|---|
| F1 Foundation | none (frontend arch) | no |
| F2 Dashboard | `/chain`, `/scan`, `/watchlist`, `/health` | no |
| F3 Token Analysis | `/analyze`, `/history/{address}`, `/chain` | no |
| F4 Smart Wallets | `/watchlist`, `/wallet/{address}`, `/watchlist/refresh` | no |
| F5 KOL Intelligence | `kol_store` (internal today) | **yes — read-only** |
| F6 Watchlists | wallet watchlist (exists) + `token_monitor` (internal) | **yes — read-only** (monitor) |
| F7 Alerts & Notifications | alert config + `notification_deliveries` (internal) | **yes — read-only + redaction** |
| F8 Analytics | `/history/{address}`, `/scan` | no |
| F9 Global Search | `/analyze`, `/wallet/{address}` | no |
| F10 Settings | `/chain`, `/health` + localStorage | no |
| F11 Mobile/Responsive | none (frontend) | no |
| F12 Production Polish | none (frontend) | no |
| F13 Admin & Diagnostics | mix: OpenAPI/health/chain (exist) + admin reads/metrics | **yes — read-only + one safe cache-clear** |

**Net backend footprint of this entire frontend roadmap:** thin, read-only
diagnostic/query endpoints that surface already-built state, one non-destructive
cache-clear action, and (only for F13's performance panel) lightweight read-only
instrumentation. **No new intelligence, no scoring change, no analysis-pipeline
change, no architecture redesign, no secret exposure.**

---

*End of frontend roadmap. Living document — revise as milestones land.*
