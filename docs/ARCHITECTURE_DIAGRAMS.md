# Architecture Diagrams

Rendered Mermaid diagrams for the Robinhood Rug Analyzer. Companion to
[`ARCHITECTURE.md`](./ARCHITECTURE.md). Diagrams reflect the **implemented**
system; planned/unbuilt elements are marked as such.

---

## Overall architecture

```mermaid
flowchart TB
    subgraph clients[External data sources - free/public]
        BS[Blockscout REST]
        DS[DexScreener]
        RPC[Chain JSON-RPC]
        DDG[DuckDuckGo / optional LLM]
        X[X / Twitter web]
    end

    subgraph app[FastAPI application]
        API[api/routes<br/>/analyze /scan /chain /watchlist]
        subgraph analysis[On-chain analysis]
            RA[rug_analyzer]
            SC[scoring]
            AN[analyzers]
            HP[honeypot_sim]
            RD[route_discovery]
            CI[contract_intel]
            LP[launchpad_registry]
            WI[wallet_intel]
            LO[lore_client]
        end
        subgraph kol[KOL Intelligence - opt-in, internal]
            KW[kol_watchlist]
            KM[kol_monitor]
            KP[kol_crypto_pipeline]
            KE[kol_intel_engine]
            subgraph social[social/ provider abstraction]
                REG[registry]
                XP[x_provider + session + scraper]
                DIFF[diff]
                CINT[crypto_intel + signals + extract]
                KS[kol_scoring]
            end
        end
        subgraph infra[Shared infra]
            HTTP[http pool]
            CACHE[cache]
            CFG[config settings]
        end
        subgraph store[Persistence - SQLite]
            WS[(watchlist.db)]
            KST[(kol.db)]
        end
    end

    API --> RA
    RA --> SC & AN & HP & CI & LP & WI & LO
    HP --> RD
    RA --> BS & DS
    HP --> RPC
    RD --> RPC
    LO --> DDG
    WI --> WS

    KW --> KM & KP & KE
    KW --> REG --> XP --> X
    KM --> DIFF
    KP --> CINT
    KP --> RA
    KE --> KS
    KW & KM & KP & KE --> KST

    RA & KW --> HTTP & CACHE & CFG
```

---

## Service dependency graph

```mermaid
flowchart TD
    routes --> rug_analyzer
    routes --> watchlist_store

    rug_analyzer --> analyzers
    rug_analyzer --> scoring
    rug_analyzer --> contract_intel
    rug_analyzer --> honeypot_sim
    rug_analyzer --> launchpad_registry
    rug_analyzer --> wallet_intel
    rug_analyzer --> lore_client
    rug_analyzer --> blockscout_client
    rug_analyzer --> dexscreener_client

    honeypot_sim --> route_discovery
    honeypot_sim --> rpc_client
    route_discovery --> rpc_client
    contract_intel --> blockscout_client
    wallet_intel --> blockscout_client
    wallet_intel --> watchlist_store
    analyzers --> launchpad_registry

    kol_watchlist --> kol_monitor
    kol_watchlist --> kol_crypto_pipeline
    kol_watchlist --> kol_intel_engine
    kol_watchlist --> social_registry[social.registry]
    kol_monitor --> social_diff[social.diff]
    kol_crypto_pipeline --> social_crypto[social.crypto_intel]
    kol_crypto_pipeline --> rug_analyzer
    kol_intel_engine --> social_scoring[social.kol_scoring]
    social_registry --> x_provider

    kol_watchlist --> kol_store
    kol_monitor --> kol_store
    kol_crypto_pipeline --> kol_store
    kol_intel_engine --> kol_store

    blockscout_client --> http & cache
    rpc_client --> http & cache
    dexscreener_client --> http

    classDef reuse fill:#e8f5e9,stroke:#43a047;
    class rug_analyzer,http,cache,config,kol_store reuse;
```

Green nodes are the most-reused/shared services. Note the single reuse edge
`kol_crypto_pipeline → rug_analyzer` and the absence of any back-edges (no
cycles).

---

## Database relationships

```mermaid
erDiagram
    kols ||--o{ following_snapshots : captures
    kols ||--o| sync_meta : health
    kols ||--o{ follow_events : emits
    kols ||--o{ profile_changes : records
    kols ||--o{ followed_accounts : follows
    kols ||--o{ crypto_classifications : "classifies (per KOL)"
    crypto_classifications ||--o{ crypto_events : "analysis audit"
    followed_accounts }o--|| kol_intel_scores : "project (platform, account_key)"
    kol_intel_scores ||--o{ kol_intel_score_history : timeline
    kol_intel_scores ||--o{ kol_cluster_history : clusters
    kol_intel_scores ||--o{ kol_intel_events : events

    wallets ||--o{ wallet_activity : "recent buys"
```

`wallets` / `wallet_activity` live in the separate `watchlist.db`; all other
tables live in `kol.db`.

---

## Event pipeline

```mermaid
flowchart LR
    subgraph producers
        M[kol_monitor] --> FE[follow_events<br/>new_follow / unfollow]
        P[kol_crypto_pipeline] --> CE[crypto_events<br/>detected / extracted / analysis_*]
        E[kol_intel_engine] --> IE[kol_intel_events<br/>score_updated / cluster / momentum]
    end
    FE & CE & IE --> DB[(SQLite - append-only)]
    DB --> RC[correlation engine reads<br/>latest_analysis_summary]
    DB -. planned .-> NP[Notification publisher<br/>Deliverable H]
    NP -. planned .-> AI[AI reasoning layer]
```

---

## KOL pipeline (end-to-end)

```mermaid
flowchart TD
    A[capture_following] --> B[provider.fetch_following]
    B --> C{snapshot complete?}
    C -->|no| Z[record failed sync, keep baseline]
    C -->|yes| D[process_snapshot: diff vs latest complete]
    D --> E{new follows?}
    E -->|baseline/none| F[persist snapshot + events only]
    E -->|yes| G[classify_account per new follow]
    G --> H[extract contracts]
    H --> I{crypto project + actionable?}
    I -->|no| J[save classification]
    I -->|yes| K[REUSE rug_analyzer per supported contract]
    K --> L[emit crypto events]
    J --> M[update_project_intelligence]
    L --> M
    M --> N[list_kols_following - cross-KOL]
    N --> O{fingerprint changed?}
    O -->|no| P[return previous unchanged]
    O -->|yes| Q[score_project + detect_cluster]
    Q --> R[persist ProjectIntelligence + history + intel events]
```

---

## Analysis pipeline (composition order)

```mermaid
flowchart TD
    V[validate address] --> B[parallel fetch batch]
    B --> MD[market data] --> AG[age] --> HD[holders]
    HD --> TR[transfers - fetched once] --> CL[clusters] --> DV[dev/creator]
    DV --> WL[wallet intel + watchlist hits] --> LK[liquidity lock]
    LK --> LPG{launchpad registry enabled?}
    LPG -->|yes| LPD[creation-evidence detect]
    LPG -->|no| LPU[Unknown]
    LPD --> LR[lore if requested]
    LPU --> LR
    LR --> HN[honeypot sim - inert unless router mapped]
    HN --> SC[score_token] --> RES[TokenAnalysisResponse]
```

---

## Notification architecture (planned - Deliverable H)

```mermaid
flowchart LR
    IE[(kol_intel_events)] --> PUB[Publisher interface]
    CE[(crypto_events)] --> PUB
    PUB --> T1[Telegram adapter]
    PUB --> T2[Discord adapter]
    PUB --> T3[Webhook adapter]
    PUB --> T4[UI feed]
    PUB -.->|config-gated, default off| GATE[notify_enabled]
```

*Not implemented. Shown as the intended shape: adapters read existing event
tables; producers are unchanged.*

---

## Future AI architecture (planned)

```mermaid
flowchart TD
    PI[ProjectIntelligence<br/>score + evidence + contributors<br/>+ cluster + correlation + timeline] --> AIC[AI client - config-gated]
    HIST[(score/cluster history)] --> AIC
    AIC --> NLE[Natural-language explanation]
    AIC --> NLQ[Natural-language querying]
    AIC --> CAL[Prediction calibration]
    CAL -. feedback .-> W[config-driven weights]
    AIC --> NOTE[AI output as new event type<br/>never mutates deterministic score]
```

*Not implemented. Reuses the optional-LLM pattern from `lore_client`.*
