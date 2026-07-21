const tabs = Array.from(document.querySelectorAll(".tab"));
const panels = document.querySelectorAll(".tab-panel");

// Switch to a tab by its data-tab name. Central so token-navigation (Smart Wallets
// -> Analyze) and keyboard nav both route through the same ARIA-correct path.
function activateTab(name) {
  tabs.forEach((t) => {
    const on = t.dataset.tab === name;
    t.classList.toggle("active", on);
    t.setAttribute("aria-selected", on ? "true" : "false");
    t.tabIndex = on ? 0 : -1;
  });
  panels.forEach((p) => p.classList.remove("active"));
  const panel = document.querySelector(`#tab-${name}`);
  if (panel) {
    panel.classList.add("active");
    // Re-trigger the subtle enter animation each time the panel is shown.
    panel.classList.remove("panel-enter");
    void panel.offsetWidth;
    panel.classList.add("panel-enter");
  }
}

tabs.forEach((tab) => {
  tab.addEventListener("click", () => activateTab(tab.dataset.tab));
});

// Keyboard support: Left/Right/Home/End move focus between tabs (roving tabindex),
// matching the WAI-ARIA tabs pattern. Enter/Space already activate (native buttons).
const tablist = document.querySelector(".tabs");
if (tablist) {
  tablist.addEventListener("keydown", (e) => {
    const i = tabs.indexOf(document.activeElement);
    if (i === -1) return;
    let next = null;
    if (e.key === "ArrowRight") next = tabs[(i + 1) % tabs.length];
    else if (e.key === "ArrowLeft") next = tabs[(i - 1 + tabs.length) % tabs.length];
    else if (e.key === "Home") next = tabs[0];
    else if (e.key === "End") next = tabs[tabs.length - 1];
    if (next) {
      e.preventDefault();
      next.focus();
      activateTab(next.dataset.tab);
    }
  });
}

// --- Chain info (fetched once) for building external explorer/DEX links ---
// Reuses the existing /api/v1/chain endpoint. No new backend. Falls back to sane
// public defaults if the request fails, so external buttons always work.
let chainInfo = {
  explorer: "https://robinhoodchain.blockscout.com",
  dexscreener_chain: "robinhood",
};
async function loadChainInfo() {
  try {
    const resp = await fetch("/api/v1/chain");
    if (resp.ok) {
      const c = await resp.json();
      chainInfo = { ...chainInfo, ...c };
    }
  } catch {
    /* keep defaults */
  }
}
loadChainInfo();

function blockscoutTokenUrl(address) {
  return `${chainInfo.explorer.replace(/\/$/, "")}/token/${address}`;
}
function dexscreenerUrl(address) {
  return `https://dexscreener.com/${encodeURIComponent(chainInfo.dexscreener_chain)}/${address}`;
}

// --- Progress controller (indeterminate, staged status text) ---
// The backend exposes no progress stream, so this drives a high-quality
// indeterminate bar plus rotating human-readable status lines while a request is
// in flight, then snaps to 100% on success. One controller per target element.
function createProgress(container, steps) {
  container.classList.add("status", "progress-host");
  container.setAttribute("aria-busy", "true");
  container.innerHTML = `
    <div class="progress-line" aria-hidden="true">
      <div class="progress-bar"><div class="progress-fill indeterminate"></div></div>
    </div>
    <div class="progress-text">${esc(steps[0] || "Working…")}</div>`;
  const fill = container.querySelector(".progress-fill");
  const text = container.querySelector(".progress-text");
  let idx = 0;
  const timer = setInterval(() => {
    idx = Math.min(idx + 1, steps.length - 1);
    text.textContent = steps[idx];
  }, 900);
  return {
    finish(message) {
      clearInterval(timer);
      fill.classList.remove("indeterminate");
      fill.classList.add("done");
      fill.style.width = "100%";
      text.textContent = message || "Done.";
      container.removeAttribute("aria-busy");
      // Let the fill animation land, then clear the scaffold.
      setTimeout(() => {
        container.classList.remove("progress-host");
        container.innerHTML = "";
        container.textContent = message || "";
      }, 350);
    },
    fail(message) {
      clearInterval(timer);
      container.classList.remove("progress-host");
      container.removeAttribute("aria-busy");
      container.innerHTML = "";
      container.textContent = message;
    },
  };
}

// Button lock: disable + swap label to loading text, restore on release. Combined
// with a per-action in-flight flag this prevents duplicate requests (including via
// requestSubmit(), which fires even when the button is disabled).
function lockButton(btn, loadingText) {
  if (!btn) return () => {};
  const original = btn.textContent;
  btn.disabled = true;
  btn.classList.add("is-loading");
  btn.textContent = loadingText;
  return () => {
    btn.disabled = false;
    btn.classList.remove("is-loading");
    btn.textContent = original;
  };
}

// Shared staged status lines for the deep single-token analysis.
const ANALYZE_STEPS = [
  "Connecting…",
  "Loading blockchain data…",
  "Checking holders…",
  "Analyzing liquidity…",
  "Running honeypot simulation…",
  "Evaluating deployer…",
  "Checking KOL intelligence…",
  "Calculating Alpha Score…",
  "Finalizing…",
];

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

// Skeleton placeholders shown while a fetch is in flight, so waiting areas are
// never blank. Purely visual (aria-hidden); the live status region announces state.
function skeletonCards(count) {
  return `<div class="skeleton-wrap" aria-hidden="true">${Array.from({ length: count })
    .map(
      () => `<div class="skeleton-card">
        <div class="skeleton-line w40"></div>
        <div class="skeleton-line w70"></div>
        <div class="skeleton-line w55"></div>
      </div>`,
    )
    .join("")}</div>`;
}

function skeletonAnalysis() {
  return `<div class="skeleton-wrap" aria-hidden="true">
    <div class="skeleton-grid">${Array.from({ length: 5 })
      .map(() => `<div class="skeleton-card"><div class="skeleton-line w50"></div><div class="skeleton-line w80"></div></div>`)
      .join("")}</div>
    <div class="skeleton-grid">${Array.from({ length: 8 })
      .map(() => `<div class="skeleton-card"><div class="skeleton-line w60"></div><div class="skeleton-line w40"></div></div>`)
      .join("")}</div>
  </div>`;
}

// --- Token action row (copy / Blockscout / DexScreener) + navigation ---
// Every discovered token reuses the contract the backend already returned — no
// extra lookup. External links open in a new tab.
function tokenActions(address) {
  const a = esc(address);
  return `<div class="token-actions" role="group" aria-label="Token actions">
    <button type="button" class="tok-btn copy-addr" data-address="${a}" aria-label="Copy contract address">Copy</button>
    <a class="tok-btn" href="${safeUrl(blockscoutTokenUrl(address))}" target="_blank" rel="noopener" aria-label="Open on Blockscout">Blockscout</a>
    <a class="tok-btn" href="${safeUrl(dexscreenerUrl(address))}" target="_blank" rel="noopener" aria-label="Open on DexScreener">DexScreener</a>
  </div>`;
}

// Send a token to the Analyze tab: populate, switch, auto-analyze, scroll + highlight.
function analyzeAddress(address, sourceEl) {
  if (!address) return;
  const input = document.querySelector("#contract-address");
  input.value = address;
  activateTab("analyze");
  document.querySelector("#tabbtn-analyze").focus();
  if (sourceEl) {
    document.querySelectorAll(".token-selected").forEach((el) => el.classList.remove("token-selected"));
    sourceEl.classList.add("token-selected");
  }
  analyzeForm.requestSubmit();
}

async function copyToClipboard(text, btn) {
  try {
    await navigator.clipboard.writeText(text);
  } catch {
    // Fallback for non-secure contexts / older browsers.
    const ta = document.createElement("textarea");
    ta.value = text;
    ta.style.position = "fixed";
    ta.style.opacity = "0";
    document.body.appendChild(ta);
    ta.select();
    try { document.execCommand("copy"); } catch { /* ignore */ }
    document.body.removeChild(ta);
  }
  if (btn) {
    const prev = btn.textContent;
    btn.textContent = "Copied!";
    btn.classList.add("copied");
    setTimeout(() => { btn.textContent = prev; btn.classList.remove("copied"); }, 1200);
  }
}

// Delegate clicks for token name, address, and Copy buttons within a container.
function wireTokenActions(container) {
  container.querySelectorAll(".token-name, .addr").forEach((el) => {
    el.addEventListener("click", () => {
      const card = el.closest("[data-address]") || el;
      analyzeAddress(el.dataset.address, card);
    });
  });
  container.querySelectorAll(".copy-addr").forEach((el) => {
    el.addEventListener("click", (e) => {
      e.stopPropagation();
      copyToClipboard(el.dataset.address, el);
    });
  });
}

// --- Ranked scanner ---

const scanForm = document.querySelector("#scan-form");
const scanStatus = document.querySelector("#scan-status");
const scanResults = document.querySelector("#scan-results");
let scanning = false;

function renderRanked(tokens) {
  if (!tokens.length) {
    scanResults.innerHTML = "";
    return;
  }
  scanResults.innerHTML = tokens
    .map(
      (t, i) => `
      <article class="ranked-card" data-address="${esc(t.contract_address)}" style="border-left: 5px solid ${riskColor(t.risk_score)}">
        <div class="rank">#${i + 1}</div>
        <div class="ranked-main">
          <strong><button type="button" class="token-name" data-address="${esc(t.contract_address)}" data-symbol="${esc(t.symbol || t.name || "")}" title="Analyze this token">${esc(t.name || "Unknown")}</button> <span class="sym">${esc(t.symbol || "")}</span></strong>
          <code class="addr" data-address="${esc(t.contract_address)}" title="Analyze this token">${esc(t.contract_address)}</code>
          <div class="ranked-meta">
            <span>Holders: ${t.holder_count ?? "N/A"}</span>
            <span>Liquidity: ${fmtCurrency(t.liquidity_usd)}</span>
            <span>Market cap: ${fmtCurrency(t.market_cap)}</span>
            <span>Age: ${fmtAge(t.age_days, t.age_hours)}</span>
          </div>
          ${tokenActions(t.contract_address)}
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

  wireTokenActions(scanResults);
}

scanForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  if (scanning) return; // ignore repeated clicks / duplicate submits
  scanning = true;
  const submitBtn = scanForm.querySelector("button[type=submit]");
  const release = lockButton(submitBtn, "Scanning…");
  const limit = Number(document.querySelector("#scan-limit").value) || 10;
  const includeLore = document.querySelector("#scan-lore").checked;
  const progress = createProgress(scanStatus, [
    `Scanning ${limit} tokens…`,
    "Loading blockchain data…",
    "Ranking by rug risk…",
    "Finalizing…",
  ]);
  scanResults.innerHTML = skeletonCards(Math.min(limit, 6));

  try {
    const response = await fetch("/api/v1/scan", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ limit, include_lore: includeLore }),
    });
    const data = await response.json();
    if (!response.ok) throw new Error(data.detail || "Scan failed");
    progress.finish(data.message);
    renderRanked(data.ranked_tokens);
  } catch (error) {
    progress.fail(`Scan failed: ${error.message}`);
    scanResults.innerHTML = "";
  } finally {
    scanning = false;
    release();
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

let analyzing = false;

analyzeForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  if (analyzing) return; // ignore repeated clicks / duplicate submits
  const contractAddress = document.querySelector("#contract-address").value.trim();
  if (!contractAddress) return;
  const includeLore = document.querySelector("#analyze-lore").checked;

  analyzing = true;
  const submitBtn = analyzeForm.querySelector("button[type=submit]");
  const release = lockButton(submitBtn, "Analyzing…");
  const progress = createProgress(result, ANALYZE_STEPS);
  // Skeleton lives in a dedicated area under the status while the request runs.
  let skeleton = document.querySelector("#analyze-skeleton");
  if (!skeleton) {
    skeleton = document.createElement("div");
    skeleton.id = "analyze-skeleton";
    result.insertAdjacentElement("afterend", skeleton);
  }
  skeleton.innerHTML = skeletonAnalysis();

  try {
    const response = await fetch("/api/v1/analyze", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ contract_address: contractAddress, include_lore: includeLore }),
    });
    const data = await response.json();
    if (!response.ok) throw new Error(data.detail || "Analysis request failed");
    progress.finish("");
    skeleton.innerHTML = "";
    renderAnalysis(data);
    result.classList.add("result-enter");
    recordRecent(contractAddress, data.market_data?.base_token_symbol);
    // Smooth scroll to the freshly rendered results.
    requestAnimationFrame(() => result.scrollIntoView({ behavior: "smooth", block: "start" }));
  } catch (error) {
    progress.fail(`Request failed: ${error.message}`);
    skeleton.innerHTML = "";
  } finally {
    analyzing = false;
    release();
    // Clear the token highlight once analysis settles.
    setTimeout(() => document.querySelectorAll(".token-selected").forEach((el) => el.classList.remove("token-selected")), 600);
  }
});

// --- Recent searches (last 10 analyzed contracts, persisted in localStorage) ---
const RECENT_KEY = "rra_recent_v1";
const recentBox = document.querySelector("#recent-searches");

function loadRecent() {
  try {
    const raw = localStorage.getItem(RECENT_KEY);
    const arr = raw ? JSON.parse(raw) : [];
    return Array.isArray(arr) ? arr : [];
  } catch {
    return [];
  }
}

function recordRecent(address, symbol) {
  const addr = (address || "").trim();
  if (!addr) return;
  let items = loadRecent().filter((it) => it.address.toLowerCase() !== addr.toLowerCase());
  items.unshift({ address: addr, symbol: symbol || "", at: Date.now() });
  items = items.slice(0, 10);
  try { localStorage.setItem(RECENT_KEY, JSON.stringify(items)); } catch { /* ignore quota */ }
  renderRecent();
}

function renderRecent() {
  const items = loadRecent();
  if (!items.length) {
    recentBox.hidden = true;
    recentBox.innerHTML = "";
    return;
  }
  recentBox.hidden = false;
  recentBox.innerHTML = `
    <span class="recent-label">Recent searches</span>
    <div class="recent-chips">${items
      .map(
        (it) => `<button type="button" class="recent-chip" data-address="${esc(it.address)}" title="${esc(it.address)}">${esc(it.symbol || shortAddr(it.address))}</button>`,
      )
      .join("")}</div>`;
  recentBox.querySelectorAll(".recent-chip").forEach((el) => {
    el.addEventListener("click", () => analyzeAddress(el.dataset.address, el));
  });
}

renderRecent();

// --- Smart Wallets watchlist ---

// --- Smart Wallets watchlist ---

const walletsForm = document.querySelector("#wallets-form");
const walletsResults = document.querySelector("#wallets-results");
const walletsNote = document.querySelector("#wallets-note");

function walletCard(w) {
  // Each discovered token the wallet recently bought becomes an actionable row:
  // the name analyzes it (reusing the contract already returned — no extra lookup),
  // plus copy / Blockscout / DexScreener. Tokens without a contract stay plain chips.
  const buyRows = (w.recent_buys || [])
    .slice(0, 6)
    .map((b) => {
      const addr = b.token_address || "";
      const label = esc(b.symbol || (addr ? shortAddr(addr) : "token"));
      if (!addr) return `<span class="chip">${label}</span>`;
      return `<div class="token-hit" data-address="${esc(addr)}">
        <button type="button" class="token-name" data-address="${esc(addr)}" data-symbol="${esc(b.symbol || "")}" title="Analyze this token">${label}</button>
        ${tokenActions(addr)}
      </div>`;
    })
    .join("");
  return `
    <article class="ranked-card" style="border-left: 5px solid ${w.kind === "smart" ? "#38f58c" : "#ffd166"}">
      <div class="ranked-main">
        <strong>${esc(w.label || w.kind)}${w.proxy_score != null ? ` · proxy ${esc(w.proxy_score)}` : ""}</strong>
        <code class="wallet-addr">${esc(w.address)}</code>
        <div class="ranked-meta"><span>${w.prior_tokens ? `Seen on ${esc(w.prior_tokens)} token${w.prior_tokens === 1 ? "" : "s"} · ` : ""}Recently buying:</span></div>
        <div class="token-hits">${buyRows || '<span class="chip">no recent buys tracked</span>'}</div>
      </div>
      <div class="score-badge" style="background: ${w.kind === "smart" ? "#146c3a" : "#8a6d1f"}">
        <span>${esc((w.kind || "").toUpperCase())}</span>
      </div>
    </article>`;
}

const walletsLoadBtn = walletsForm.querySelector("button[type=submit]");
const walletsRefreshBtn = document.querySelector("#wallets-refresh");
let walletsBusy = false;

async function loadWatchlist() {
  if (walletsBusy) return; // ignore duplicate loads
  walletsBusy = true;
  const release = lockButton(walletsLoadBtn, "Loading…");
  walletsRefreshBtn.disabled = true;
  const progress = createProgress(walletsNote, [
    "Loading watchlist…",
    "Reading wallet intelligence…",
    "Finalizing…",
  ]);
  walletsResults.innerHTML = skeletonCards(3);
  // M21: filter by kind + sort key from the controls; the server whitelists both.
  const kind = document.querySelector("#wallets-kind")?.value || "";
  const sort = document.querySelector("#wallets-sort")?.value || "score";
  const params = new URLSearchParams({ sort });
  if (kind) params.set("kind", kind);
  try {
    const response = await fetch(`/api/v1/watchlist?${params}`);
    const data = await response.json();
    if (!response.ok) throw new Error(data.detail || "Watchlist request failed");
    progress.finish(data.note);
    const smart = (data.smart_wallets || []).map(walletCard).join("");
    const insider = (data.insider_wallets || []).map(walletCard).join("");
    walletsResults.innerHTML = `
      <h2>Smart Wallets (estimated)</h2>
      <div class="ranked-list">${smart || "<p class='lore-meta'>No smart wallets found for this token from free on-chain signals (early entry, held position, and surviving cross-token holdings). This is an estimate, not verified ROI.</p>"}</div>
      <h2>Insider Wallets</h2>
      <div class="ranked-list">${insider || "<p class='lore-meta'>No insider wallets flagged yet.</p>"}</div>`;
    walletsResults.classList.add("result-enter");
    wireTokenActions(walletsResults);
  } catch (error) {
    progress.fail(`Request failed: ${error.message}`);
    walletsResults.innerHTML = "";
  } finally {
    walletsBusy = false;
    release();
    walletsRefreshBtn.disabled = false;
  }
}

// M21: on-request refresh fallback for idle-prone hosts (Render free tier suspends
// the background loop). Re-pulls a bounded batch from chain, then reloads the view.
async function refreshWatchlistFromChain() {
  if (walletsBusy) return;
  walletsBusy = true;
  const release = lockButton(walletsRefreshBtn, "Refreshing…");
  walletsLoadBtn.disabled = true;
  const progress = createProgress(walletsNote, [
    "Refreshing from chain…",
    "Pulling recent buys…",
    "Finalizing…",
  ]);
  try {
    const response = await fetch("/api/v1/watchlist/refresh", { method: "POST" });
    const data = await response.json();
    if (!response.ok) throw new Error(data.detail || "Refresh failed");
    progress.finish("Refreshed. Reloading…");
  } catch (error) {
    progress.fail(`Refresh failed: ${error.message}`);
    walletsBusy = false;
    release();
    walletsLoadBtn.disabled = false;
    return;
  }
  walletsBusy = false;
  release();
  walletsLoadBtn.disabled = false;
  await loadWatchlist();
}

walletsForm.addEventListener("submit", (event) => {
  event.preventDefault();
  loadWatchlist();
});

walletsRefreshBtn.addEventListener("click", refreshWatchlistFromChain);

// Wallet addresses are shown as plain (copy-friendly) code; only the discovered
// TOKENS inside each wallet card are clickable/analyzable (wired in loadWatchlist).

// Auto-load the watchlist the first time its tab is opened.
let watchlistLoaded = false;
document.querySelector('.tab[data-tab="wallets"]').addEventListener("click", () => {
  if (!watchlistLoaded) {
    watchlistLoaded = true;
    loadWatchlist();
  }
});
