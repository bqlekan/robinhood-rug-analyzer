/** render/analysis.js — renders every field of TokenAnalysisResponse. */
import {
  esc, safeUrl, fmtCurrency, fmtPct, fmtAge, riskColor, shortAddr,
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

export function renderLore(lore) {
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
  return `
    <section>
      <h2>Insider &amp; Smart-Wallet Signals</h2>
      <p class="lore-meta">Smart-wallet scores are heuristic estimates from free on-chain data, not verified ROI.</p>
      <ul class="signals">${hitRows}${insiderRows || "<li>No insider wallets detected from the sampled transfers.</li>"}</ul>
    </section>`;
}

function repColor(score) {
  const s = Math.max(0, Math.min(100, score ?? 0));
  const hue = (130 * s) / 100;
  return `hsl(${hue}, 75%, 40%)`;
}

function renderDevReputation(rep) {
  if (!rep) return "";
  const lines = (rep.evidence || [])
    .map((e) => `<li class="signal${e.startsWith("+") ? "" : " signal-medium"}">${esc(e)}</li>`)
    .join("");
  return `
    <section>
      <h2>Developer Reputation</h2>
      <div class="analysis-summary" style="border-left: 5px solid ${repColor(rep.score)}">
        ${card("Reputation Score", `${rep.score}/100`)}
        ${rep.wallet_age_days != null ? card("Wallet Age", `${Math.round(rep.wallet_age_days)}d`) : ""}
        ${card("Contracts Deployed", esc(rep.total_contracts_deployed))}
        ${card("Verified", esc(rep.verified_contracts))}
        ${card("Abandoned", esc(rep.abandoned_launches))}
      </div>
      ${lines ? `<ul class="signals">${lines}</ul>` : ""}
    </section>`;
}

function renderWalletReputations(reps) {
  if (!reps || !reps.length) return "";
  const cards = reps
    .map((r) => {
      const ev = (r.evidence || [])
        .map((e) => `<li class="signal${e.startsWith("+") ? "" : " signal-medium"}">${esc(e)}</li>`)
        .join("");
      return `
        <div class="analysis-summary" style="border-left: 5px solid ${repColor(r.score)}; margin-bottom: 0.5em;">
          ${card("Score", `${r.score}/100`, r.confidence)}
          ${card("Address", `<code class="addr-inline">${esc(shortAddr(r.address))}</code>`)}
          ${r.wallet_age_days != null ? card("Age", `${Math.round(r.wallet_age_days)}d`) : ""}
          ${card("Tokens", esc(r.token_interactions))}
          ${card("Surviving", esc(r.surviving_projects))}
          ${r.rugs_entered ? card("Rugs", esc(r.rugs_entered)) : ""}
        </div>
        ${ev ? `<ul class="signals">${ev}</ul>` : ""}`;
    })
    .join("");
  return `
    <section>
      <h2>Smart Wallet Reputations</h2>
      ${cards}
    </section>`;
}

function renderDeveloperNetwork(net) {
  if (!net) return "";
  const ev = (net.evidence || [])
    .map((e) => `<li class="signal${e.startsWith("+") ? "" : " signal-medium"}">${esc(e)}</li>`)
    .join("");
  const sibs = (net.siblings || [])
    .slice(0, 8)
    .map((s) => `<li class="signal"><strong>${esc(s.name || s.symbol || shortAddr(s.address))}</strong><span>${esc((s.outcome || "unknown").replace(/_/g, " "))}${s.holder_count != null ? ` · ${esc(s.holder_count)} holders` : ""}${s.shared_wallets ? ` · ${esc(s.shared_wallets)} shared` : ""}</span></li>`)
    .join("");
  const infra = (net.siblings || []).flatMap((s) => s.shared_infrastructure || []);
  const infraHtml = infra.length
    ? `<p class="lore-meta">Shared: ${infra.map((i) => esc(i)).join(", ")}</p>`
    : "";
  return `
    <section>
      <h2>Developer Network</h2>
      <div class="analysis-summary" style="border-left: 5px solid ${repColor(net.score)}">
        ${card("Network Score", `${net.score}/100`, net.cluster_confidence)}
        ${card("Cluster Size", esc(net.cluster_size))}
        ${net.funding_wallet ? card("Funding Wallet", `<code class="addr-inline">${esc(shortAddr(net.funding_wallet))}</code>`) : ""}
        ${net.historical_success_rate != null ? card("Success Rate", `${Math.round(net.historical_success_rate * 100)}%`) : ""}
        ${net.historical_failure_rate != null && net.historical_failure_rate > 0 ? card("Failure Rate", `${Math.round(net.historical_failure_rate * 100)}%`) : ""}
      </div>
      ${infraHtml}
      ${ev ? `<ul class="signals">${ev}</ul>` : ""}
      ${sibs ? `<h3>Sibling Tokens</h3><ul class="signals">${sibs}</ul>` : ""}
    </section>`;
}

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
    <section>
      <h2>Deployer Detail</h2>
      ${d.transferred_out
        ? `<p class="lore-meta">Deployer moved tokens out to ${d.transfers_out_count} wallet(s)${d.transferred_out_percentage != null ? ` (~${d.transferred_out_percentage}% of supply)` : ""}.</p>`
        : `<p class="lore-meta">No outgoing deployer transfers detected in the sampled window.</p>`}
      ${transfers ? `<ul class="signals">${transfers}</ul>` : ""}
      ${launched ? `<h2>Other tokens by this deployer</h2><ul class="signals">${launched}</ul>` : ""}
    </section>`;
}

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
  const events = (tl.events || []);
  if (!events.length) return `<section><h2>Alpha Timeline</h2>${summaryHtml}<p>No timeline events generated.</p></section>`;
  const rows = events.map((e) => {
    const sevIcon = _SEV_ICON[e.severity] || "";
    const catIcon = _CAT_ICON[e.category] || "";
    const sevClass = e.severity === "critical" || e.severity === "high" ? " signal-" + esc(e.severity) : "";
    return `
      <li class="signal${sevClass}">
        <strong>${catIcon} ${esc(e.title)}</strong>
        <span>${esc(e.category)} · ${esc((e.severity || "info").toUpperCase())} · ${esc((e.confidence || "medium").toUpperCase())} confidence</span>
        ${e.explanation ? `<p>${esc(e.explanation)}</p>` : ""}
        ${e.impact ? `<p class="lore-meta">Impact: ${esc(e.impact)}</p>` : ""}
        ${e.evidence ? `<details><summary>Evidence</summary><p class="lore-meta">${esc(e.evidence)}</p></details>` : ""}
        ${e.timestamp ? `<p class="lore-meta" style="font-size:0.8em">${esc(e.timestamp)}</p>` : ""}
      </li>`;
  }).join("");
  return `
    <section>
      <h2>Alpha Timeline</h2>
      ${summaryHtml}
      <ul class="signals">${rows}</ul>
    </section>`;
}

export function renderAnalysis(data, resultEl) {
  const m = data.market_data;
  const a = data.analysis;
  const h = data.holders;
  const d = data.dev;
  const ll = data.liquidity_lock;
  const color = riskColor(a.risk_score);

  resultEl.innerHTML = `
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
      ${card("Deployer / Creator", d?.creator_address ? `<code class="addr-inline">${esc(d.creator_address)}</code>` : "Unknown", d?.creation_tx ? `creation tx ${esc(shortAddr(d.creation_tx))}` : null)}
      ${card("Contract", esc(data.contract_intel?.contract_name || "Unnamed"), data.contract_intel?.verified ? esc(`${data.contract_intel.template}${data.contract_intel.protocol ? ` · ${data.contract_intel.protocol}` : ""}`) : "unverified source")}
      ${card("Compiler", esc(data.contract_intel?.compiler || "N/A"), esc(data.contract_intel?.language || ""))}
      ${card("Ownership", esc(privilegeOwnership(data.contract_privileges)), data.contract_privileges?.analyzed ? esc(privilegePowers(data.contract_privileges)) : "unverified / no ABI")}
    </section>

    <section>
      <h2>Risk Signals</h2>
      <ul class="signals">${renderSignals(a.signals)}</ul>
    </section>

    ${renderDevReputation(data.developer_reputation)}
    ${renderDeveloperNetwork(data.developer_network)}
    ${renderTimeline(data.timeline)}
    ${renderWalletReputations(data.wallet_reputations)}
    ${renderInsiders(data.insiders, data.watchlist_hits)}
    ${renderDevDetail(d)}
    ${renderLore(data.lore)}

    <section>
      <h2>Limitations</h2>
      <ul class="limitations">${a.limitations.map((i) => `<li>${esc(i)}</li>`).join("")}</ul>
    </section>

    ${m?.url ? `<a class="source-link" href="${safeUrl(m.url)}" target="_blank" rel="noopener">View pair on DexScreener</a>` : ""}
    <div id="history-view"></div>
  `;
}
