# KOL Intelligence Engine — Architecture

This document covers:

- **M23 Deliverable A** — the KOL watchlist and the generic social-provider
  abstraction (sections 1–9).
- **M23 Deliverable B** — the X following scraper: a persistent authenticated
  Playwright session that fetches and persists following snapshots (section 10).

Snapshot diffing, follow detection, scoring, clustering, and alerting remain
**later deliverables** and are not implemented.

> Scope note for Deliverable B: the engine can now *read* one platform (X) and
> *store* what it read. It still does not compare snapshots, detect new follows,
> score, or alert. Deliverable B only retrieves and persists following snapshots.

---

## 1. Design goal: the engine never knows what platform it is reading

The organizing principle is a dependency inversion. The intelligence engine
reasons only about platform-neutral concepts — KOLs, follows, snapshots — defined
in `app/models/kol.py`. Each social platform (X today; Farcaster, Telegram,
Discord, Reddit, Lens tomorrow) is a **provider** that translates its own wire
format into those neutral models.

```
                 ┌─────────────────────────────────────────┐
                 │        KOL Intelligence Engine            │
                 │  (watchlist, + future: diff/score/alert)  │
                 │   depends only on models/kol + the        │
                 │   SocialGraphProvider interface           │
                 └───────────────┬───────────────────────────┘
                                 │ asks registry for a provider
                                 │ by platform key ("x", ...)
                 ┌───────────────▼───────────────┐
                 │   social/registry.py          │  platform -> provider
                 └───────────────┬───────────────┘
             ┌───────────────────┼───────────────────┐
             ▼                   ▼                    ▼
      XProvider (built)   FarcasterProvider     TelegramProvider
                            (future)              (future)
```

The engine imports the **interface** and the **registry** — never a concrete
provider. Adding a platform is: write one module, register it. No engine change.

---

## 2. Module responsibilities

| Module | Responsibility |
|---|---|
| `app/models/kol.py` | Platform-neutral domain models + controlled vocabularies (`SOCIAL_PLATFORMS`, `KOL_TIERS`, `KOL_STATUSES`). The shared vocabulary. |
| `app/services/social/base.py` | The `SocialGraphProvider` ABC and `ProviderError`. The single seam between engine and platforms. |
| `app/services/social/registry.py` | Platform-key → provider lookup. Keeps the engine decoupled from concrete providers. |
| `app/services/social/x_provider.py` | First concrete provider (X/Twitter). Handle rules + URLs implemented; network fetch deferred to Deliverable B. |
| `app/services/kol_store.py` | Low-level sqlite persistence (watchlist, snapshots, sync metadata). Raw CRUD only. |
| `app/services/kol_watchlist.py` | **Public service facade.** All validation and business rules. The only module callers should touch. |

Nothing outside `kol_store` touches sqlite; nothing outside a provider knows a
platform's specifics; callers use only `kol_watchlist`.

---

## 3. The provider abstraction (extension point)

`SocialGraphProvider` (in `social/base.py`) is the contract every platform
implements:

```python
class SocialGraphProvider(ABC):
    platform: str                                   # must be in SOCIAL_PLATFORMS

    def capabilities(self) -> ProviderCapabilities: ...   # what it can do now
    def normalize_handle(self, handle: str) -> str: ...   # canonical identity
    def account_url(self, handle: str) -> str: ...        # profile URL (pure)
    async def fetch_following(self, handle) -> FollowingSnapshot: ...  # the graph
```

Key idea: the engine reads `capabilities()` instead of branching on platform
name. A provider that cannot yet fetch (like X in Deliverable A) reports
`can_fetch_following=False` and raises `NotImplementedError` from
`fetch_following`, so the engine degrades gracefully rather than crashing or
inventing an empty follow set.

`ProviderError` is the expected-failure channel (auth expired, rate limited,
transient UI/network error), carrying `platform` and a `retryable` flag for the
future scheduler. Providers raise it instead of leaking bare exceptions, so a
scrape failure always degrades to an explicit "unknown", never a false signal.

### Adding a new provider (e.g. Farcaster)

1. Create `app/services/social/farcaster_provider.py` with a
   `class FarcasterProvider(SocialGraphProvider)` implementing the four methods.
2. Register it in `registry._install_default_providers()`:
   ```python
   from app.services.social.farcaster_provider import FarcasterProvider
   register_provider(FarcasterProvider())
   ```
3. Ensure `"farcaster"` is in `SOCIAL_PLATFORMS` (it already is).

No change to `kol_watchlist`, `kol_store`, or any future engine code. The
watchlist immediately accepts `platform="farcaster"` entries and normalizes their
handles through the new provider.

> A platform can be listed in `SOCIAL_PLATFORMS` *before* it has a provider. The
> watchlist will store such entries (handles normalized with a safe default) and
> `get_watch_status(...).provider_available` reports `False` until the provider is
> wired. This lets you stage config ahead of implementation.

---

## 4. Persistence model

`kol_store.py` uses stdlib `sqlite3` (no new dependency), mirroring the existing
`watchlist_store.py`: one lock-guarded module connection, defensive reads, a
`reset_for_tests` hook. The DB path is `settings.kol_db_path` (default
`data/kol.db`), kept separate from the wallet watchlist DB so the two stores stay
independently scalable.

Three tables, designed so later deliverables **extend rather than migrate**:

| Table | Key | Holds |
|---|---|---|
| `kols` | `(platform, handle)` | The watchlist row: display_name, tier, enabled, notes, date_added, last_checked, status. |
| `following_snapshots` | `(platform, handle, captured_at)` | Point-in-time follow captures. Accounts stored as JSON. Schema + reader ready; **producers land in Deliverable B/C.** |
| `sync_meta` | `(platform, handle)` | last_success / last_attempt / last_error, kept separate so sync accounting doesn't churn the watchlist row. |

Identity is `(platform, lowercased handle)`. The store normalizes case as a
backstop so two rows can never differ only by case. Deleting a KOL cascades to its
snapshots and sync metadata.

Snapshots store the account list as JSON in a single row. At watchlist scale
(tens of KOLs, ~two snapshots each) this is simpler and faster than a wide join,
and can be normalized later without touching the `kols` table or the public API.

---

## 5. Configuration format

All settings live in `app/core/config.py` (pydantic `BaseSettings`), overridable
via `.env`:

| Setting | Default | Meaning |
|---|---|---|
| `kol_intel_enabled` | `False` | Master switch for future background work. |
| `kol_db_path` | `data/kol.db` | sqlite path for the KOL store. |
| `kol_default_platform` | `"x"` | Platform assumed when a seed/entry omits one. |
| `kol_watchlist_seed` | `[]` | Declarative watchlist (see below). |
| `kol_config_overwrites` | `True` | Whether config re-sync overwrites operator edits. |

The **watchlist seed** is the no-code management surface. Each item:

```python
kol_watchlist_seed = [
    {"handle": "cobie", "tier": 1, "display_name": "Cobie"},
    {"handle": "@ansem", "platform": "x", "tier": 2, "notes": "sol caller"},
    {"handle": "someone", "enabled": False},
]
```

`kol_watchlist.sync_from_config()` reconciles this into the store:

- **Adds** any missing KOL.
- **Updates** existing rows (display_name/tier/enabled/notes) **only if**
  `kol_config_overwrites=True`; otherwise leaves operator edits untouched and
  counts the seed as skipped.
- **Never deletes.** Removing a KOL from config does not remove it from the store
  — deletion is an explicit operator action, so a config typo can't wipe a tracked
  KOL and its history.
- **Skips invalid seeds** (bad handle, unknown platform, out-of-range tier) with a
  logged warning; one bad entry never aborts the sync.

Managing the watchlist through config alone: add/remove an entry, flip `enabled`,
or change `tier` in `kol_watchlist_seed`, then let `sync_from_config()` run.

---

## 6. Public interface

Callers (API routes, the future scheduler, tests) use **only** `kol_watchlist`:

| Function | Purpose |
|---|---|
| `add_kol(handle, *, platform, display_name, tier, enabled, notes)` | Add or update a KOL. Returns the stored `KolEntry`. |
| `update_kol(handle, *, ...)` | Patch given fields. `KeyError` if absent. |
| `remove_kol(handle, *, platform)` | Delete (cascades snapshots/sync). Returns `bool`. |
| `list_kols(*, platform, enabled_only)` | List entries, sorted by tier then handle. |
| `get_kol(handle, *, platform)` | Fetch one `KolEntry` or `None`. |
| `set_enabled(handle, enabled, *, platform)` | Toggle without removing. |
| `set_tier(handle, tier, *, platform)` | Change tier. |
| `get_watch_status(handle, *, platform)` | Read-only `WatchStatus` (health, snapshot presence, provider availability) — exposes no storage/provider internals. |
| `sync_from_config(seeds=None)` | Reconcile config into the store. Returns `{added, updated, skipped}`. |

Validation happens at this boundary: invalid handles, unknown platforms, and
out-of-range tiers raise `ValueError` before anything reaches the store.

---

## 7. Status model

Two orthogonal fields describe a KOL's state:

- **`enabled`** (bool) — operator intent: should we watch this KOL at all.
- **`status`** (lifecycle) — operational health:
  - `pending` — enabled, never synced yet (no snapshot).
  - `active` — enabled and last sync succeeded *(set by future sync code)*.
  - `error` — enabled but last sync failed *(set by future sync code)*.
  - `paused` — disabled by the operator.

Deliverable A only ever sets `pending`/`paused` (there is no sync yet); `active`
and `error` are written once the Deliverable B scheduler runs.

---

## 8. What is intentionally NOT here (later deliverables)

| Deferred | Where it lands |
|---|---|
| ~~Playwright scraping / live `fetch_following`~~ | **Done — Deliverable B (§10)** |
| Snapshot diffing / new-follow detection | Deliverable C |
| Crypto-account detection | Deliverable D |
| Rug/alpha pipeline integration | Deliverable E |
| KOL-intel scoring | Deliverable F |
| Cluster detection | Deliverable G |
| Alert generation | Deliverable H |

The follow-graph read is stubbed as an explicit `NotImplementedError` so nothing
silently mistakes "not implemented" for "follows nobody".

---

## 9. Future migration path (X free-scrape → official API)

Because all X specifics live behind `XProvider` (and its `x_session`/`x_scraper`
helpers), swapping the free Playwright scrape for the official X API later touches
only those three files: reimplement `fetch_following` and update `capabilities()`.
The engine, watchlist, store, and every other provider are unaffected — the same
property that lets new platforms plug in also lets one platform's data source
change underneath.

---

## 10. Deliverable B — the X following scraper

Deliverable B implements X's `fetch_following` against a **persistent
authenticated browser**. It reads and persists snapshots; it does not diff,
detect, score, or alert.

### 10.1 Module layout

X is split into three files so session, DOM, and orchestration each stay testable
in isolation:

| Module | Responsibility |
|---|---|
| `social/x_session.py` | `XSession` — owns the browser lifecycle and **authentication**. A Playwright *persistent context* rooted at `x_user_data_dir` so cookies survive across runs. Detects session state; never bypasses auth. |
| `social/x_scraper.py` | Pure page-driven DOM logic: profile-state classification, infinite-scroll collection with de-dup, handle extraction. Operates on an injected page. |
| `social/x_provider.py` | Orchestrates: normalize handle → open session → scrape → return a `FollowingSnapshot`. Translates any stray error into a typed `ProviderError`. |
| `social/errors.py` | The `ProviderError` taxonomy (below). |

The two lower modules never touch each other; the provider wires them. Playwright
is imported **lazily** (only inside the real launch path), so importing any of
these modules — and the whole app and unit test suite — needs no browser binaries.

### 10.2 Session management & authentication

`XSession` uses a persistent context, so authentication is a one-time manual step:

- **Existing session** — `ensure_ready()` opens the profile, loads `/home`, and
  confirms authed chrome is present. Returns a ready page.
- **Expired session** — profile has prior state but `/home` redirects to login →
  `SessionExpiredError` (not retryable; a human must reauthenticate).
- **No session** — empty/missing profile dir → `AuthUnavailableError`.
- **Manual (re)authentication** — `login_interactive()` is the *only* auth entry
  point. It forces a headful window and waits for a human to complete X's login/
  challenge, then persists the session. We never type credentials or solve
  challenges programmatically, and never bypass authentication.

The context launcher is injectable (`context_factory`), which is how the tests
exercise all four states with a fake context and zero Playwright dependency.

Setup (once): `python -m playwright install chromium`, then run the interactive
login with `x_headless=False` to seed `data/x_profile/`. That directory holds
session credentials and is gitignored (`data/`).

### 10.3 Snapshot fetching

`x_scraper.scrape_following(page, handle)`:

1. Navigate to `https://x.com/<handle>/following`.
2. `classify_profile_state` — before scrolling, inspect the page and raise a typed
   error for private / suspended / not-found (rename) / rate-limited states, so a
   bad account never yields a misleading empty list.
3. `scroll_and_collect` — X virtualizes the list (only a window of rows is in the
   DOM), so we read handles on **every** scroll step and accumulate into a
   dict keyed by lowercased handle (de-dup). Scrolling stops when:
   - `x_scroll_stable_rounds` consecutive scrolls add nothing new (list end), or
   - `x_scroll_max_rounds` / `x_following_max` safety caps trip → the result is
     flagged `complete=False`.

The provider wraps the rows into a normalized `FollowingSnapshot` (handles
lowercased, profile URLs built). `complete=False` propagates so later diffing can
refuse to treat a partial pull as "unfollowed everyone".

### 10.4 Error taxonomy (`errors.py`)

All subclass Deliverable A's `ProviderError`, so `except ProviderError` still
catches everything and `.retryable` still works; callers that need to react
specifically match a subclass.

| Error | retryable | Meaning |
|---|---|---|
| `SessionExpiredError` | no | Session lapsed; needs manual reauth. |
| `AuthUnavailableError` | no | No session ever established. |
| `RateLimitedError` | yes | X is throttling; carries `retry_after_seconds` hint. |
| `TransientNetworkError` | yes | Timeout / connection reset / navigation failure. |
| `AccountPrivateError` | no | Target is protected; following not visible. |
| `AccountUnavailableError` | no | Suspended / not-found / deactivated. Handle **renames** land here (old handle 404s); re-linking is an explicit later-deliverable decision, never an automatic guess. |

The provider's catch-all converts any unexpected browser exception into a
`TransientNetworkError` — a scrape failure always degrades to an explicit,
typed error, never a false "follows nobody".

### 10.5 Persistence facade

`kol_watchlist.capture_following(handle, platform=None)` is the public entry point:

1. Resolve + normalize the handle; require the KOL to be on the watchlist.
2. Check the provider advertises `can_fetch_following`.
3. `await provider.fetch_following(handle)`.
4. On success: `save_snapshot`, `record_sync(success=True)`, status → `active`.
5. On a typed failure: `record_sync(success=False, error=…)`, status → `error`,
   prior snapshots left intact, and the error re-raised for the caller/scheduler.

It stores snapshots using the Deliverable A persistence layer unchanged. It does
**not** compare snapshots — two captures simply produce two independent rows.

### 10.6 Extension points

- **New platform** — unchanged from §3: write a provider, register it. A platform
  can reuse the session/scraper split if it's also browser-scraped, or ignore it
  entirely (e.g. an API-based Farcaster provider needs neither).
- **Swap X's data source** — see §9; now spans `x_provider`/`x_session`/`x_scraper`.
- **Scheduler (Deliverable C+)** — will call `capture_following` per enabled KOL on
  a cadence, using `ProviderError.retryable` / `retry_after_seconds` for back-off.
- **Tuning** — scroll/timeout/cap behavior is all config (`x_*` in `config.py`);
  no code change to adjust pacing or limits.
