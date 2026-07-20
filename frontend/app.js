const tabs = document.querySelectorAll(".tab");
const panels = document.querySelectorAll(".tab-panel");

tabs.forEach((tab) => {
  tab.addEventListener("click", () => {
    tabs.forEach((t) => t.classList.remove("active"));
    panels.forEach((p) => p.classList.remove("active"));
    tab.classList.add("active");
    document.querySelector(`#tab-${tab.dataset.tab}`).classList.add("active");
  });
});

// Escape untrusted text before it goes into innerHTML. Token names/symbols come
// from on-chain metadata and lore titles/urls from web search — all attacker-controllable.
function esc(value) {
  if (value === null || value === undefined) return "";
  return String(value)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#39;");
}

// Only allow http(s) URLs into href attributes; block javascript:/data: and junk.
function safeUrl(url) {
  if (!url) return "#";
  try {
    const u = new URL(url, window.location.origin);
    return u.protocol === "http:" || u.protocol === "https:" ? url : "#";
  } catch {
    return "#";
  }
}

function fmtCurrency(value) {
  if (value === null || value === undefined) return "N/A";
  return new Intl.NumberFormat("en-US", {
    style: "currency",
    currency: "USD",
    maximumFractionDigits: 2,
  }).format(value);
}

function fmtPct(value) {
  return value === null || value === undefined ? "N/A" : `${value}%`;
}

// Age as "Xd Yh" (or just hours when under a day), preferring exact hours when available.
function fmtAge(days, hours) {
  const totalHours =
    hours !== null && hours !== undefined
      ? hours
      : days !== null && days !== undefined
        ? days * 24
        : null;
  if (totalHours === null) return "N/A";
  const d = Math.floor(totalHours / 24);
  const h = Math.round(totalHours % 24);
  return d > 0 ? `${d}d ${h}h` : `${h}h`;
}

// Map a 0-100 risk score onto a smooth red -> green gradient (green = low risk).
// Hue 130 (green) at 0 down to hue 0 (red) at 100.
function riskColor(score) {
  const s = Math.max(0, Math.min(100, score ?? 0));
  const hue = 130 - (130 * s) / 100;
  return `hsl(${hue}, 75%, 45%)`;
}

function badgeHtml(hits) {
  if (!hits || !hits.length) return "";
  const smart = hits.filter((h) => h.kind === "smart").length;
  const insider = hits.filter((h) => h.kind === "insider").length;
  const parts = [];
  if (smart) parts.push(`<span class="wallet-badge smart">${smart} smart</span>`);
  if (insider) parts.push(`<span class="wallet-badge insider">${insider} insider</span>`);
  return `<div class="wallet-badges">${parts.join("")}</div>`;
}

// --- Ranked scanner ---

const scanForm = document.querySelector("#scan-form");
const scanStatus = document.querySelector("#scan-status");
const scanResults = document.querySelector("#scan-results");

function renderRanked(tokens) {
  if (!tokens.length) {
    scanResults.innerHTML = "";
    return;
  }
  scanResults.innerHTML = tokens
    .map(
      (t, i) => `
      <article class="ranked-card" style="border-left: 5px solid ${riskColor(t.risk_score)}">
        <div class="rank">#${i + 1}</div>
        <div class="ranked-main">
          <strong>${esc(t.name || "Unknown")} <span class="sym">${esc(t.symbol || "")}</span></strong>
          <code class="addr" data-address="${esc(t.contract_address)}" title="Analyze this token">${esc(t.contract_address)}</code>
          <div class="ranked-meta">
            <span>Holders: ${t.holder_count ?? "N/A"}</span>
            <span>Liquidity: ${fmtCurrency(t.liquidity_usd)}</span>
            <span>Market cap: ${fmtCurrency(t.market_cap)}</span>
            <span>Age: ${fmtAge(t.age_days, t.age_hours)}</span>
          </div>
          ${badgeHtml(t.flagged_by)}
          ${t.top_signal ? `<div class="top-signal">Top risk: ${esc(t.top_signal)}</div>` : ""}
        </div>
        <div class="score-badge" style="background: ${riskColor(t.risk_score)}">
          <strong>${t.risk_score}</strong>
          <span>${esc((t.risk_level || "").toUpperCase())}</span>
        </div>
      </article>`,
    )
    .join("");

  // Clicking an address opens it in the analyze tab.
  scanResults.querySelectorAll(".addr").forEach((el) => {
    el.addEventListener("click", () => {
      document.querySelector('[data-tab="analyze"]').click();
      document.querySelector("#contract-address").value = el.dataset.address;
      analyzeForm.requestSubmit();
    });
  });
}

scanForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  const limit = Number(document.querySelector("#scan-limit").value) || 10;
  const includeLore = document.querySelector("#scan-lore").checked;
  scanStatus.textContent = `Scanning ${limit} tokens... this can take a moment.`;
  scanResults.innerHTML = "";

  try {
    const response = await fetch("/api/v1/scan", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ limit, include_lore: includeLore }),
    });
    const data = await response.json();
    if (!response.ok) throw new Error(data.detail || "Scan failed");
    scanStatus.textContent = data.message;
    renderRanked(data.ranked_tokens);
  } catch (error) {
    scanStatus.textContent = `Scan failed: ${error.message}`;
  }
});

// --- Single token analysis ---

const analyzeForm = document.querySelector("#analyze-form");
const result = document.querySelector("#result");

function renderSignals(signals) {
  if (!signals.length) {
    return "<li>No major warning signals detected from available public data.</li>";
  }
  return signals
    .map(
      (s) => `
      <li class="signal signal-${esc(s.severity)}">
        <strong>${esc(s.name)}</strong>
        <span>${esc(s.category)} · ${esc((s.severity || "").toUpperCase())} · +${s.points}</span>
        <p>${esc(s.description)}</p>
      </li>`,
    )
    .join("");
}

function card(label, value, sub) {
  return `<article><span class="label">${label}</span><strong>${value}</strong>${sub ? `<div class="card-sub">${sub}</div>` : ""}</article>`;
}

function shortAddr(addr) {
  if (!addr) return "N/A";
  return `${addr.slice(0, 6)}…${addr.slice(-4)}`;
}

// M11 contract-privilege helpers. Renounced (owner==zero) is the only reassuring state;
// retained or unconfirmed ownership keeps the powers dangerous.
function privilegeOwnership(p) {
  if (!p || !p.analyzed) return "UNKNOWN";
  if (p.ownership_renounced === true) return "RENOUNCED";
  if (p.ownership_renounced === false) return "OWNER RETAINED";
  return "UNCONFIRMED";
}

function privilegePowers(p) {
  const powers = [];
  if (p.can_mint) powers.push("mint");
  if (p.can_pause) powers.push(p.is_paused ? "paused NOW" : "pause");
  if (p.can_blacklist) powers.push("blacklist");
  if (p.can_set_fees) powers.push("fees");
  return powers.length ? powers.join(" · ") : "no dangerous powers";
}

function renderLore(lore) {
  if (!lore) return "";
  const sources = lore.sources
    .map((s) => `<li><a href="${safeUrl(s.url)}" target="_blank" rel="noopener">${esc(s.title)}</a> <em>(${esc(s.source)})</em></li>`)
    .join("");
  const themes = lore.themes.map((t) => `<span class="chip">${esc(t)}</span>`).join("");
  return `
    <section>
      <h2>Lore &amp; Social Narrative</h2>
      <p class="lore-summary">${esc(lore.summary || "No summary available.")}</p>
      <p class="lore-meta">Sentiment: <strong>${esc((lore.sentiment || "unknown").toUpperCase())}</strong> · Source: ${esc(lore.generated_by)}</p>
      ${themes ? `<div class="chips">${themes}</div>` : ""}
      ${sources ? `<ul class="sources">${sources}</ul>` : ""}
    </section>`;
}

function renderInsiders(insiders, hits) {
  if ((!insiders || !insiders.length) && (!hits || !hits.length)) return "";
  const insiderRows = (insiders || [])
    .map(
      (w) => `
      <li class="signal">
        <strong>${esc(w.address)}</strong>
        <span>${esc(w.reason.replace(/_/g, " "))}${w.buy_rank ? ` · buyer #${esc(w.buy_rank)}` : ""} · ${fmtPct(w.holding_percentage)} held</span>
        ${w.note ? `<p>${esc(w.note)}</p>` : ""}
      </li>`,
    )
    .join("");
  const hitRows = (hits || [])
    .map(
      (h) => `
      <li class="signal signal-medium">
        <strong>${esc(h.address)}</strong>
        <span>Watchlisted ${esc(h.kind)}${h.proxy_score != null ? ` · proxy ${esc(h.proxy_score)}` : ""} holds this token${h.prior_tokens ? ` · seen on ${esc(h.prior_tokens)} prior token${h.prior_tokens === 1 ? "" : "s"}` : ""}</span>
      </li>`,
    )
    .join("");
  return `
    <section>
      <h2>Insider &amp; Smart-Wallet Signals</h2>
      <p class="lore-meta">Smart-wallet scores are heuristic estimates from free on-chain data, not verified ROI.</p>
      <ul class="signals">${hitRows}${insiderRows || "<li>No insider wallets detected from the sampled transfers.</li>"}</ul>
    </section>`;
}

function renderDevDetail(d) {
  if (!d) return "";
  const launched = (d.launched_tokens || [])
    .map((t) => `<li class="signal"><strong>${esc(t.name || t.address)}</strong><span>${esc((t.outcome || "unknown").replace(/_/g, " "))}</span></li>`)
    .join("");
  const transfers = (d.dev_transfers || [])
    .slice(0, 8)
    .map((t) => `<li class="signal"><strong>${esc(t.to_address)}</strong><span>${t.amount_percentage != null ? `${esc(t.amount_percentage)}% of supply` : "amount N/A"}</span></li>`)
    .join("");
  if (!launched && !transfers) return "";
  return `
    <section>
      <h2>Deployer Detail</h2>
      ${
        d.transferred_out
          ? `<p class="lore-meta">Deployer moved tokens out to ${d.transfers_out_count} wallet(s)${d.transferred_out_percentage != null ? ` (~${d.transferred_out_percentage}% of supply)` : ""}.</p>`
          : `<p class="lore-meta">No outgoing deployer transfers detected in the sampled window.</p>`
      }
      ${transfers ? `<ul class="signals">${transfers}</ul>` : ""}
      ${launched ? `<h2>Other tokens by this deployer</h2><ul class="signals">${launched}</ul>` : ""}
    </section>`;
}

function renderAnalysis(data) {
  const m = data.market_data;
  const a = data.analysis;
  const h = data.holders;
  const d = data.dev;
  const ll = data.liquidity_lock;
  const color = riskColor(a.risk_score);

  result.innerHTML = `
    <section class="analysis-summary" style="border-left: 5px solid ${color}">
      ${card("Risk Score", `${a.risk_score}/100`)}
      ${card("Risk Level", esc(a.risk_level.toUpperCase()))}
      ${a.confidence != null ? card("Data Confidence", `${esc(a.confidence)}%`, esc((a.confidence_level || "").toUpperCase())) : ""}
      ${card("Token", `${esc(m?.base_token_name || "Unknown")} (${esc(m?.base_token_symbol || "N/A")})`)}
      ${card("Age", fmtAge(data.token_age?.age_days, data.token_age?.age_hours))}
    </section>

    <section class="market-grid">
      ${card("Price", m?.price_usd ? `$${esc(m.price_usd)}` : "N/A")}
      ${card("Liquidity", fmtCurrency(m?.liquidity?.usd))}
      ${card("24h Volume", fmtCurrency(m?.volume?.h24))}
      ${card("Holders", h?.holder_count ?? "N/A")}
      ${card("Top 10 Hold", fmtPct(h?.top10_percentage), "excludes LP pool")}
      ${card("Top Holder", fmtPct(h?.top1_percentage), "excludes LP pool")}
      ${card("LP Pool Holds", fmtPct(h?.lp_percentage), h?.lp_address ? shortAddr(h.lp_address) : "no LP detected")}
      ${card("Dev Holdings", fmtPct(d?.dev_holding_percentage))}
      ${card("Dev Reputation", esc(d?.reputation || "unknown"))}
      ${card("Liquidity Lock", esc((ll?.status || "unknown").toUpperCase()), ll?.unlock_in_days != null ? (ll.unlock_in_days > 0 ? `unlocks in ~${esc(ll.unlock_in_days)}d` : "lock expired") : "")}
      ${card("Sellability", esc((data.honeypot?.status || "unknown").toUpperCase()), data.honeypot?.sell_tax_percentage != null ? `~${esc(data.honeypot.sell_tax_percentage)}% round-trip loss` : "simulation")}
      ${card("Launchpad", esc(data.launchpad?.name || "Unknown"))}
      ${card("Clusters", data.clusters?.clusters?.length ?? 0)}
      ${card("Clustered %", fmtPct(data.clusters?.clustered_percentage))}
      ${card("Bundling", esc(data.bundle?.classification || "Normal"), data.bundle?.bundled_wallets ? `${esc(data.bundle.bundled_wallets)} wallets · ${fmtPct(data.bundle.bundled_percentage)}` : "no bundle detected")}
      ${card("Buy Timing", esc(data.buy_timing?.coordinated ? "COORDINATED" : "NORMAL"), data.buy_timing?.same_block_wallets ? `${esc(data.buy_timing.same_block_wallets)} wallets same block` : "no launch cohort")}
      ${card("Trend", esc(data.trend?.has_prior ? (data.trend.signals?.length ? "ADVERSE" : "STABLE") : "FIRST SCAN"), data.trend?.has_prior && data.trend.liquidity_change_pct != null ? `liquidity ${data.trend.liquidity_change_pct > 0 ? "+" : ""}${esc(data.trend.liquidity_change_pct)}%` : "no prior snapshot")}
    </section>

    <section class="market-grid">
      ${card(
        "Deployer / Creator",
        d?.creator_address ? `<code class="addr-inline">${esc(d.creator_address)}</code>` : "Unknown",
        d?.creation_tx ? `creation tx ${esc(shortAddr(d.creation_tx))}` : null,
      )}
      ${card(
        "Contract",
        esc(data.contract_intel?.contract_name || "Unnamed"),
        data.contract_intel?.verified
          ? esc(`${data.contract_intel.template}${data.contract_intel.protocol ? ` · ${data.contract_intel.protocol}` : ""}`)
          : "unverified source",
      )}
      ${card(
        "Compiler",
        esc(data.contract_intel?.compiler || "N/A"),
        esc(data.contract_intel?.language || ""),
      )}
      ${card(
        "Ownership",
        esc(privilegeOwnership(data.contract_privileges)),
        data.contract_privileges?.analyzed ? esc(privilegePowers(data.contract_privileges)) : "unverified / no ABI",
      )}
    </section>

    <section>
      <h2>Risk Signals</h2>
      <ul class="signals">${renderSignals(a.signals)}</ul>
    </section>

    ${renderInsiders(data.insiders, data.watchlist_hits)}

    ${renderDevDetail(d)}

    ${renderLore(data.lore)}

    <section>
      <h2>Limitations</h2>
      <ul class="limitations">${a.limitations.map((i) => `<li>${esc(i)}</li>`).join("")}</ul>
    </section>

    ${m?.url ? `<a class="source-link" href="${safeUrl(m.url)}" target="_blank" rel="noopener">View pair on DexScreener</a>` : ""}
  `;
}

analyzeForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  const contractAddress = document.querySelector("#contract-address").value.trim();
  const includeLore = document.querySelector("#analyze-lore").checked;
  result.textContent = "Running full rug-risk analysis...";

  try {
    const response = await fetch("/api/v1/analyze", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ contract_address: contractAddress, include_lore: includeLore }),
    });
    const data = await response.json();
    if (!response.ok) throw new Error(data.detail || "Analysis request failed");
    renderAnalysis(data);
  } catch (error) {
    result.textContent = `Request failed: ${error.message}`;
  }
});

// --- Smart Wallets watchlist ---

const walletsForm = document.querySelector("#wallets-form");
const walletsResults = document.querySelector("#wallets-results");
const walletsNote = document.querySelector("#wallets-note");

function walletCard(w) {
  const buys = (w.recent_buys || [])
    .slice(0, 6)
    .map((b) => `<span class="chip">${esc(b.symbol || (b.token_address || "").slice(0, 8))}</span>`)
    .join("");
  return `
    <article class="ranked-card" style="border-left: 5px solid ${w.kind === "smart" ? "#38f58c" : "#ffd166"}">
      <div class="ranked-main">
        <strong>${esc(w.label || w.kind)}${w.proxy_score != null ? ` · proxy ${esc(w.proxy_score)}` : ""}</strong>
        <code class="addr" data-address="${esc(w.address)}">${esc(w.address)}</code>
        <div class="ranked-meta"><span>Recently buying:</span></div>
        <div class="chips">${buys || '<span class="chip">no recent buys tracked</span>'}</div>
      </div>
      <div class="score-badge" style="background: ${w.kind === "smart" ? "#146c3a" : "#8a6d1f"}">
        <span>${esc((w.kind || "").toUpperCase())}</span>
      </div>
    </article>`;
}

async function loadWatchlist() {
  walletsResults.innerHTML = "";
  walletsNote.textContent = "Loading watchlist...";
  try {
    const response = await fetch("/api/v1/watchlist");
    const data = await response.json();
    if (!response.ok) throw new Error(data.detail || "Watchlist request failed");
    walletsNote.textContent = data.note;
    const smart = (data.smart_wallets || []).map(walletCard).join("");
    const insider = (data.insider_wallets || []).map(walletCard).join("");
    walletsResults.innerHTML = `
      <h2>Smart Wallets (estimated)</h2>
      <div class="ranked-list">${smart || "<p class='lore-meta'>No smart wallets found for this token from free on-chain signals (early entry, held position, and surviving cross-token holdings). This is an estimate, not verified ROI.</p>"}</div>
      <h2>Insider Wallets</h2>
      <div class="ranked-list">${insider || "<p class='lore-meta'>No insider wallets flagged yet.</p>"}</div>`;
  } catch (error) {
    walletsNote.textContent = `Request failed: ${error.message}`;
  }
}

walletsForm.addEventListener("submit", (event) => {
  event.preventDefault();
  loadWatchlist();
});

// Clicking a wallet address analyzes nothing (wallets aren't tokens); just copy-friendly.

// Auto-load the watchlist the first time its tab is opened.
let watchlistLoaded = false;
document.querySelector('.tab[data-tab="wallets"]').addEventListener("click", () => {
  if (!watchlistLoaded) {
    watchlistLoaded = true;
    loadWatchlist();
  }
});
