/** render/analysis.js — renders TokenAnalysisResponse as an Opportunity Intelligence view.
 *  All scores come from the backend response; no business logic is computed here. */
import {
  esc, safeUrl, fmtCurrency, fmtPct, fmtAge, riskColor, shortAddr,
  opportunityColor, opportunityLevel, opportunityBadgesHtml, healthBar, summaryText,
  alphaSignalsHtml,
} from "../ui.js";

function card(label, value, sub) {
  return `<article><span class="label">${label}</span><strong>${value}</strong>${sub ? `<div class="card-sub">${sub}</div>` : ""}</article>`;
}

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

function repColor(score) {
  const s = Math.max(0, Math.min(100, score ?? 0));
  const hue = (130 * s) / 100;
  return `hsl(${hue}, 75%, 40%)`;
}

// --- Explanation panel (what increased / reduced a score) ---
function explanationPanel(evidence, confidence) {
  if (!evidence || !evidence.length) return "";
  const pos = evidence.filter((e) => e.startsWith("+"));
  const neg = evidence.filter((e) => !e.startsWith("+"));
  return `<div class="explanation-panel">
    ${pos.length ? `<h4>What increased it</h4><div class="explanation-items">${pos.map((e) => `<div class="explanation-item positive">${esc(e.slice(1).trim())}</div>`).join("")}</div>` : ""}
    ${neg.length ? `<h4>What reduced it</h4><div class="explanation-items">${neg.map((e) => `<div class="explanation-item negative">${esc(e.replace(/^[-−]/, "").trim())}</div>`).join("")}</div>` : ""}
    ${confidence ? `<div style="margin-top:0.4rem;color:var(--muted);font-size:0.75rem">Confidence: ${esc(confidence)}</div>` : ""}
  </div>`;
}

// --- Signals ---
export function renderSignals(signals) {
  if (!signals.length) return "<li>No major warning signals detected from available public data.</li>";
  return signals
    .map((s) => `
      <li class="signal signal-${esc(s.severity)}">
        <strong>${esc(s.name)}</strong>
        <span>${esc(s.category)} · ${esc((s.severity || "").toUpperCase())} · +${s.points}</span>
        <p>${esc(s.description)}</p>
      </li>`)
    .join("");
}

// --- Lore ---
export function renderLore(lore) {
  if (!lore) return "";
  const sources = lore.sources
    .map((s) => `<li><a href="${safeUrl(s.url)}" target="_blank" rel="noopener">${esc(s.title)}</a> <em>(${esc(s.source)})</em></li>`)
    .join("");
  const themes = lore.themes.map((t) => `<span class="chip">${esc(t)}</span>`).join("");
  return `
    <details class="collapsible">
      <summary>Lore &amp; Social Narrative</summary>
      <p class="lore-summary">${esc(lore.summary || "No summary available.")}</p>
      <p class="lore-meta">Sentiment: <strong>${esc((lore.sentiment || "unknown").toUpperCase())}</strong> · Source: ${esc(lore.generated_by)}</p>
      ${themes ? `<div class="chips">${themes}</div>` : ""}
      ${sources ? `<ul class="sources">${sources}</ul>` : ""}
    </details>`;
}

// --- Insiders ---
export function renderInsiders(insiders, hits) {
  if ((!insiders || !insiders.length) && (!hits || !hits.length)) return "";
  const insiderRows = (insiders || [])
    .map((w) => `
      <li class="signal">
        <strong>${esc(w.address)}</strong>
        <span>${esc(w.reason.replace(/_/g, " "))}${w.buy_rank ? ` · buyer #${esc(w.buy_rank)}` : ""} · ${fmtPct(w.holding_percentage)} held</span>
        ${w.note ? `<p>${esc(w.note)}</p>` : ""}
      </li>`)
    .join("");
  const hitRows = (hits || [])
    .map((h) => `
      <li class="signal signal-medium">
        <strong>${esc(h.address)}</strong>
        <span>Watchlisted ${esc(h.kind)}${h.proxy_score != null ? ` · proxy ${esc(h.proxy_score)}` : ""} holds this token${h.prior_tokens ? ` · seen on ${esc(h.prior_tokens)} prior token${h.prior_tokens === 1 ? "" : "s"}` : ""}</span>
      </li>`)
    .join("");
  return `<ul class="signals">${hitRows}${insiderRows || "<li>No insider wallets detected from the sampled transfers.</li>"}</ul>`;
}

// --- Developer reputation ---
function renderDevReputation(rep) {
  if (!rep) return `<div class="empty-state">No developer reputation data available.</div>`;
  return `
    <div class="analysis-summary" style="border-left: 5px solid ${repColor(rep.score)}">
      ${card("Reputation Score", `${rep.score}/100`)}
      ${rep.wallet_age_days != null ? card("Wallet Age", `${Math.round(rep.wallet_age_days)}d`) : ""}
      ${card("Contracts Deployed", esc(rep.total_contracts_deployed))}
      ${card("Verified", esc(rep.verified_contracts))}
      ${card("Abandoned", esc(rep.abandoned_launches))}
    </div>
    ${explanationPanel(rep.evidence, rep.confidence)}`;
}

// --- Wallet reputations ---
function renderWalletReputations(reps) {
  if (!reps || !reps.length) return `<div class="empty-state">No smart wallet activity has been detected yet.</div>`;
  return reps
    .map((r) => `
      <div class="analysis-summary" style="border-left: 5px solid ${repColor(r.score)}; margin-bottom: 0.5em;">
        ${card("Score", `${r.score}/100`, r.confidence)}
        ${card("Address", `<code class="addr-inline">${esc(shortAddr(r.address))}</code>`)}
        ${r.wallet_age_days != null ? card("Age", `${Math.round(r.wallet_age_days)}d`) : ""}
        ${card("Tokens", esc(r.token_interactions))}
        ${card("Surviving", esc(r.surviving_projects))}
        ${r.rugs_entered ? card("Rugs", esc(r.rugs_entered)) : ""}
      </div>
      ${explanationPanel(r.evidence, r.confidence)}`)
    .join("");
}

// --- Developer network ---
function renderDeveloperNetwork(net) {
  if (!net) return `<div class="empty-state">No network data available for this deployer.</div>`;
  const sibs = (net.siblings || [])
    .slice(0, 8)
    .map((s) => `<li class="signal"><strong>${esc(s.name || s.symbol || shortAddr(s.address))}</strong><span>${esc((s.outcome || "unknown").replace(/_/g, " "))}${s.holder_count != null ? ` · ${esc(s.holder_count)} holders` : ""}${s.shared_wallets ? ` · ${esc(s.shared_wallets)} shared` : ""}</span></li>`)
    .join("");
  const infra = (net.siblings || []).flatMap((s) => s.shared_infrastructure || []);
  return `
    <div class="analysis-summary" style="border-left: 5px solid ${repColor(net.score)}">
      ${card("Network Score", `${net.score}/100`, net.cluster_confidence)}
      ${card("Cluster Size", esc(net.cluster_size))}
      ${net.funding_wallet ? card("Funding Wallet", `<code class="addr-inline">${esc(shortAddr(net.funding_wallet))}</code>`) : ""}
      ${net.historical_success_rate != null ? card("Success Rate", `${Math.round(net.historical_success_rate * 100)}%`) : ""}
      ${net.historical_failure_rate != null && net.historical_failure_rate > 0 ? card("Failure Rate", `${Math.round(net.historical_failure_rate * 100)}%`) : ""}
    </div>
    ${explanationPanel(net.evidence, net.cluster_confidence)}
    ${infra.length ? `<p class="lore-meta">Shared: ${infra.map((i) => esc(i)).join(", ")}</p>` : ""}
    ${sibs ? `<h3>Sibling Tokens</h3><ul class="signals">${sibs}</ul>` : ""}`;
}

// --- Dev detail ---
export function renderDevDetail(d) {
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
    ${d.transferred_out
      ? `<p class="lore-meta">Deployer moved tokens out to ${d.transfers_out_count} wallet(s)${d.transferred_out_percentage != null ? ` (~${d.transferred_out_percentage}% of supply)` : ""}.</p>`
      : `<p class="lore-meta">No outgoing deployer transfers detected in the sampled window.</p>`}
    ${transfers ? `<ul class="signals">${transfers}</ul>` : ""}
    ${launched ? `<h3>Other tokens by this deployer</h3><ul class="signals">${launched}</ul>` : ""}`;
}

// --- Vertical Timeline ---
const _SEV_ICON = { critical: "⛔", high: "⚠️", medium: "ℹ️", low: "•", info: "✔️" };
const _CAT_ICON = {
  Launch: "🚀", Developer: "👤", Liquidity: "💧", "Smart Wallet": "🧠",
  Insider: "🕵️", Security: "🔒", Ownership: "📜", Contract: "📄",
  "Holder Growth": "📈", Network: "🌐", KOL: "🌟", Opportunity: "⭐",
  Risk: "⚠️", Whale: "🐳",
};

function renderTimeline(tl) {
  if (!tl) return "";
  const s = tl.summary || {};
  const summaryHtml = `
    <div class="analysis-summary" style="border-left: 5px solid var(--accent, #4a90d9)">
      ${card("Launch", esc((s.launch_quality || "unknown").toUpperCase()))}
      ${card("Developer", esc((s.developer_behaviour || "unknown").replace(/_/g, " ").toUpperCase()))}
      ${card("Liquidity", esc((s.liquidity_evolution || "unknown").toUpperCase()))}
      ${card("Community", esc((s.community_growth || "unknown").replace(/_/g, " ").toUpperCase()))}
      ${card("Smart Money", esc((s.smart_money || "none").toUpperCase()))}
    </div>
    ${s.narrative ? `<p class="lore-summary">${esc(s.narrative)}</p>` : ""}`;

  const events = tl.events || [];
  if (!events.length) return `${summaryHtml}<p class="lore-meta">No timeline events generated.</p>`;

  const timelineHtml = `<div class="vtimeline">${events.map((e) => {
    const sev = e.severity || "info";
    const catIcon = _CAT_ICON[e.category] || "";
    return `
      <div class="vtimeline-event">
        <div class="vtimeline-dot ${esc(sev)}"></div>
        <div class="vtimeline-event-title">${catIcon} ${esc(e.title)}</div>
        <div class="vtimeline-event-meta">${esc(e.category)} · ${esc(sev.toUpperCase())} · ${esc((e.confidence || "medium").toUpperCase())} confidence${e.timestamp ? ` · <span class="vtimeline-ts">${esc(e.timestamp)}</span>` : ""}</div>
        ${e.explanation ? `<div class="vtimeline-event-body">${esc(e.explanation)}</div>` : ""}
        ${e.impact ? `<div class="vtimeline-event-impact">Impact: ${esc(e.impact)}</div>` : ""}
        ${e.evidence ? `<details><summary>Evidence</summary><p>${esc(e.evidence)}</p></details>` : ""}
      </div>`;
  }).join("")}</div>`;

  return `${summaryHtml}${timelineHtml}`;
}

// --- Quick Health Bars (all values from backend response) ---
function renderHealthBars(data) {
  const oppScore = data.alpha_score;
  const riskScore = data.analysis?.risk_score;
  const securityScore = riskScore != null ? Math.max(0, 100 - riskScore) : null;
  const devScore = data.developer_reputation?.score ?? null;
  const netScore = data.developer_network?.score ?? null;
  const smartCount = (data.watchlist_hits || []).filter((h) => h.kind === "smart").length;
  const smartScore = smartCount > 0 ? Math.min(100, smartCount * 33) : 0;
  const liqUsd = data.market_data?.liquidity?.usd;
  const liqScore = liqUsd != null ? Math.min(100, Math.max(0, Math.round(Math.log10(Math.max(liqUsd, 1)) / Math.log10(100000) * 100))) : null;

  return `<div class="health-bars">
    ${healthBar("Opportunity", oppScore, opportunityColor(oppScore))}
    ${healthBar("Security", securityScore, riskColor(riskScore ?? 50))}
    ${healthBar("Developer", devScore, repColor(devScore))}
    ${healthBar("Dev Network", netScore, repColor(netScore))}
    ${healthBar("Smart Wallets", smartScore, smartCount > 0 ? "var(--primary)" : "var(--muted)")}
    ${healthBar("Liquidity", liqScore, liqScore != null && liqScore >= 50 ? "var(--primary)" : "var(--warning)")}
  </div>`;
}

// --- Opportunity signals (alpha_signals from backend) ---
function renderAlphaSignals(signals) {
  if (!signals || !signals.length) return "";
  return alphaSignalsHtml(signals);
}

// --- Main render ---
export function renderAnalysis(data, resultEl) {
  const m = data.market_data;
  const a = data.analysis;
  const h = data.holders;
  const d = data.dev;
  const ll = data.liquidity_lock;
  const oppColor = opportunityColor(data.alpha_score);
  const rColor = riskColor(a.risk_score);
  const summary = summaryText(data);

  resultEl.innerHTML = `
    <!-- Summary Header -->
    <div class="summary-header">
      <div class="summary-scores">
        <div class="summary-opp-score" style="background: ${oppColor}">
          <strong>${data.alpha_score ?? "–"}</strong>
          <span>${opportunityLevel(data.alpha_score)}</span>
        </div>
        <div class="summary-risk-score" style="border-left: 4px solid ${rColor}">
          <strong>${a.risk_score}</strong>
          <span>RISK</span>
        </div>
      </div>
      <div class="summary-body">
        <h2>${esc(m?.base_token_name || "Unknown")} <span class="sym">(${esc(m?.base_token_symbol || "N/A")})</span></h2>
        <p class="summary-text">${esc(summary)}</p>
        ${a.confidence != null ? `<p class="summary-confidence">Data confidence: ${esc(a.confidence)}% (${esc((a.confidence_level || "").toUpperCase())})</p>` : ""}
      </div>
    </div>

    <!-- Quick Health Bars -->
    ${renderHealthBars(data)}

    <!-- Opportunity Badges -->
    ${opportunityBadgesHtml(data)}

    <!-- Opportunity Signals -->
    ${renderAlphaSignals(data.alpha_signals)}

    <!-- Overview -->
    <section class="market-grid">
      ${card("Price", m?.price_usd ? `$${esc(m.price_usd)}` : "N/A")}
      ${card("Liquidity", fmtCurrency(m?.liquidity?.usd))}
      ${card("24h Volume", fmtCurrency(m?.volume?.h24))}
      ${card("Market Cap", fmtCurrency(m?.market_cap))}
      ${card("Age", fmtAge(data.token_age?.age_days, data.token_age?.age_hours))}
      ${card("Holders", h?.holder_count ?? "N/A")}
      ${card("Sellability", esc((data.honeypot?.status || "unknown").toUpperCase()), data.honeypot?.sell_tax_percentage != null ? `~${esc(data.honeypot.sell_tax_percentage)}% round-trip loss` : "simulation")}
      ${card("Launchpad", esc(data.launchpad?.name || "Unknown"))}
    </section>

    <!-- Security -->
    <details class="collapsible" open>
      <summary>Security</summary>
      <section class="market-grid">
        ${card("Risk Score", `${a.risk_score}/100`)}
        ${card("Risk Level", esc(a.risk_level.toUpperCase()))}
        ${card("Ownership", esc(privilegeOwnership(data.contract_privileges)), data.contract_privileges?.analyzed ? esc(privilegePowers(data.contract_privileges)) : "unverified / no ABI")}
        ${card("Contract", esc(data.contract_intel?.contract_name || "Unnamed"), data.contract_intel?.verified ? esc(`${data.contract_intel.template}${data.contract_intel.protocol ? ` · ${data.contract_intel.protocol}` : ""}`) : "unverified source")}
        ${card("Compiler", esc(data.contract_intel?.compiler || "N/A"), esc(data.contract_intel?.language || ""))}
      </section>
      <h3>Risk Signals</h3>
      <ul class="signals">${renderSignals(a.signals)}</ul>
    </details>

    <!-- Developer -->
    <details class="collapsible">
      <summary>Developer</summary>
      <section class="market-grid">
        ${card("Deployer", d?.creator_address ? `<code class="addr-inline">${esc(d.creator_address)}</code>` : "Unknown", d?.creation_tx ? `creation tx ${esc(shortAddr(d.creation_tx))}` : null)}
        ${card("Dev Holdings", fmtPct(d?.dev_holding_percentage))}
        ${card("Dev Reputation", esc(d?.reputation || "unknown"))}
      </section>
      <h3>Developer Reputation</h3>
      ${renderDevReputation(data.developer_reputation)}
      ${renderDevDetail(d)}
    </details>

    <!-- Developer Network -->
    <details class="collapsible">
      <summary>Developer Network</summary>
      ${renderDeveloperNetwork(data.developer_network)}
    </details>

    <!-- Smart Wallets -->
    <details class="collapsible">
      <summary>Smart Wallets</summary>
      <p class="lore-meta">Smart-wallet scores are heuristic estimates from free on-chain data, not verified ROI.</p>
      ${renderWalletReputations(data.wallet_reputations)}
      ${renderInsiders(data.insiders, data.watchlist_hits)}
    </details>

    <!-- Liquidity -->
    <details class="collapsible">
      <summary>Liquidity</summary>
      <section class="market-grid">
        ${card("Liquidity", fmtCurrency(m?.liquidity?.usd))}
        ${card("LP Pool Holds", fmtPct(h?.lp_percentage), h?.lp_address ? shortAddr(h.lp_address) : "no LP detected")}
        ${card("Liquidity Lock", esc((ll?.status || "unknown").toUpperCase()), ll?.unlock_in_days != null ? (ll.unlock_in_days > 0 ? `unlocks in ~${esc(ll.unlock_in_days)}d` : "lock expired") : "")}
        ${card("Trend", esc(data.trend?.has_prior ? (data.trend.signals?.length ? "ADVERSE" : "STABLE") : "FIRST SCAN"), data.trend?.has_prior && data.trend.liquidity_change_pct != null ? `liquidity ${data.trend.liquidity_change_pct > 0 ? "+" : ""}${esc(data.trend.liquidity_change_pct)}%` : "no prior snapshot")}
      </section>
    </details>

    <!-- Holder Analysis -->
    <details class="collapsible">
      <summary>Holder Analysis</summary>
      <section class="market-grid">
        ${card("Holders", h?.holder_count ?? "N/A")}
        ${card("Top 10 Hold", fmtPct(h?.top10_percentage), "excludes LP pool")}
        ${card("Top Holder", fmtPct(h?.top1_percentage), "excludes LP pool")}
        ${card("Clusters", data.clusters?.clusters?.length ?? 0)}
        ${card("Clustered %", fmtPct(data.clusters?.clustered_percentage))}
        ${card("Bundling", esc(data.bundle?.classification || "Normal"), data.bundle?.bundled_wallets ? `${esc(data.bundle.bundled_wallets)} wallets · ${fmtPct(data.bundle.bundled_percentage)}` : "no bundle detected")}
        ${card("Buy Timing", esc(data.buy_timing?.coordinated ? "COORDINATED" : "NORMAL"), data.buy_timing?.same_block_wallets ? `${esc(data.buy_timing.same_block_wallets)} wallets same block` : "no launch cohort")}
      </section>
    </details>

    <!-- Alpha Timeline -->
    <details class="collapsible"${data.timeline ? " open" : ""}>
      <summary>Alpha Timeline</summary>
      ${data.timeline ? renderTimeline(data.timeline) : '<div class="empty-state">No timeline events available for this token.</div>'}
    </details>

    <!-- Lore & Evidence -->
    ${renderLore(data.lore)}

    <details class="collapsible">
      <summary>Limitations</summary>
      <ul class="limitations">${a.limitations.map((i) => `<li>${esc(i)}</li>`).join("")}</ul>
    </details>

    ${m?.url ? `<a class="source-link" href="${safeUrl(m.url)}" target="_blank" rel="noopener">View pair on DexScreener</a>` : ""}
    <div id="history-view"></div>
  `;
}
