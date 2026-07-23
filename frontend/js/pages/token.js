/** Token Analysis page — POST /api/v1/analyze, render full response + recent searches. */
import { apiClient } from "../api.js";
import { activateTab } from "../router.js";
import {
  esc, safeUrl, fmtCurrency, fmtPct, fmtAge, riskColor, shortAddr,
  createProgress, lockButton, skeletonAnalysis, toast,
} from "../ui.js";

const analyzeForm = document.querySelector("#analyze-form");
const result = document.querySelector("#result");
const analyzeStatus = document.querySelector("#analyze-status");

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
  // Progress + skeleton live in their OWN status node, never in #result, so the
  // finish() cleanup timer can never wipe the rendered analysis.
  const progress = createProgress(analyzeStatus, ANALYZE_STEPS);
  result.innerHTML = skeletonAnalysis();

  try {
    const data = await apiClient.analyze(contractAddress, includeLore);
    progress.finish("Analysis complete.");
    renderAnalysis(data);
    result.classList.add("result-enter");
    recordRecent(contractAddress, data.market_data?.base_token_symbol);
    requestAnimationFrame(() => result.scrollIntoView({ behavior: "smooth", block: "start" }));
  } catch (error) {
    progress.fail(`Request failed: ${error.message}`);
    toast(`Analysis failed: ${error.message}`, "error");
    result.innerHTML = "";
  } finally {
    analyzing = false;
    release();
    setTimeout(() => document.querySelectorAll(".token-selected").forEach((el) => el.classList.remove("token-selected")), 600);
  }
});

// Send a token to the Analyze tab: populate, switch, auto-analyze, highlight.
// Invoked directly by recent-search chips and via the "rra:analyze" event that
// ui.js dispatches from any wired token card (scan results / wallet cards).
function analyzeAddress(address, sourceEl) {
  if (!address) return;
  document.querySelector("#contract-address").value = address;
  activateTab("analyze");
  document.querySelector("#tabbtn-analyze").focus();
  if (sourceEl) {
    document.querySelectorAll(".token-selected").forEach((el) => el.classList.remove("token-selected"));
    sourceEl.classList.add("token-selected");
  }
  analyzeForm.requestSubmit();
}

document.addEventListener("rra:analyze", (e) => analyzeAddress(e.detail.address, e.detail.sourceEl));

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
