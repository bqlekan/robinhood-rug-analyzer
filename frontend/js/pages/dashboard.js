/** Dashboard page — "Best Opportunities Right Now" landing.
 *  Sorted by alpha_score (Opportunity Score), opportunity-first cards.
 *  Chain banner, watchlist counts, quick-analyze. No scoring — display only. */
import { apiClient, chainInfo } from "../api.js";
import {
  esc, safeUrl, fmtCurrency, fmtAge, riskColor, opportunityColor, opportunityLevel,
  skeletonCards, tokenActions, wireTokenActions, badgeHtml, alphaSignalsHtml, summaryText,
} from "../ui.js";

const chainBox = document.querySelector("#dashboard-chain");
const statRow = document.querySelector("#dashboard-watchlist");
const topRiskBox = document.querySelector("#dashboard-top-risk");
const quickForm = document.querySelector("#quick-analyze-form");

const PAGE_SIZE = 5;

async function loadBanner() {
  chainBox.innerHTML = `
    <span class="dash-live"><span class="dash-dot" aria-hidden="true"></span> checking…</span>
    <span class="dash-chain-meta">Loading chain info…</span>`;
  const [health, chain] = await Promise.all([
    apiClient.health().catch(() => null),
    apiClient.chain().catch(() => null),
  ]);
  const dotCls = health?.status === "ok" ? "up" : "down";
  const dotLabel = health?.status === "ok" ? `live · v${esc(health.version)}` : "backend unreachable";
  const name = chain?.chain_name || chainInfo.chain_name || "Robinhood Chain";
  const id = chain?.chain_id ?? chainInfo.chain_id;
  const explorer = chain?.explorer || chainInfo.explorer;
  chainBox.innerHTML = `
    <span class="dash-chain-name">${esc(name)}</span>
    <span class="dash-chain-meta">${id != null ? `chain id ${esc(id)}` : ""}</span>
    <a href="${safeUrl(explorer)}" target="_blank" rel="noopener">Blockscout explorer</a>
    <span class="dash-live"><span class="dash-dot ${dotCls}" aria-hidden="true"></span> ${dotLabel}</span>`;
}

async function loadWatchlistCounts() {
  statRow.innerHTML = `
    <div class="dash-stat"><strong>…</strong><span>Smart wallets</span></div>
    <div class="dash-stat"><strong>…</strong><span>Insider wallets</span></div>`;
  try {
    const data = await apiClient.watchlist("", "score");
    const smart = (data.smart_wallets || []).length;
    const insider = (data.insider_wallets || []).length;
    statRow.innerHTML = `
      <div class="dash-stat"><strong>${smart}</strong><span>Smart wallets</span></div>
      <div class="dash-stat"><strong>${insider}</strong><span>Insider wallets</span></div>`;
  } catch {
    statRow.innerHTML = `<p class="dash-empty">Watchlist unavailable.</p>`;
  }
}

function topSignalHtml(t) {
  const pos = (t.alpha_signals || []).find((s) => s.positive);
  const neg = (t.alpha_signals || []).find((s) => !s.positive);
  const parts = [];
  if (pos) parts.push(`<span class="alpha-signal pos">+ ${esc(pos.detail)}</span>`);
  if (neg) parts.push(`<span class="alpha-signal neg">− ${esc(neg.detail)}</span>`);
  return parts.length ? `<div class="alpha-signals">${parts.join("")}</div>` : "";
}

async function loadTopOpportunities() {
  topRiskBox.innerHTML = `
    <h2 class="dash-section-title">Best opportunities right now</h2>
    ${skeletonCards(3)}`;
  try {
    const data = await apiClient.scan(PAGE_SIZE, false);
    let tokens = data.ranked_tokens || [];
    tokens.sort((a, b) => (b.alpha_score ?? 0) - (a.alpha_score ?? 0));
    if (!tokens.length) {
      topRiskBox.innerHTML = `
        <h2 class="dash-section-title">Best opportunities right now</h2>
        <div class="empty-state"><strong>No active tokens found</strong>No qualifying launches were found. Check back soon.</div>`;
      return;
    }
    topRiskBox.innerHTML = `
      <h2 class="dash-section-title">Best opportunities right now</h2>
      <div class="ranked-list">${tokens
        .map(
          (t, i) => `
          <article class="ranked-card" data-address="${esc(t.contract_address)}" style="border-left: 5px solid ${opportunityColor(t.alpha_score)}">
            <div class="rank">#${i + 1}</div>
            <div class="ranked-main">
              <strong><button type="button" class="token-name" data-address="${esc(t.contract_address)}" data-symbol="${esc(t.symbol || t.name || "")}" title="Analyze this token">${esc(t.name || "Unknown")}</button> <span class="sym">${esc(t.symbol || "")}</span></strong>
              <code class="addr" data-address="${esc(t.contract_address)}" title="Analyze this token">${esc(t.contract_address)}</code>
              <div class="ranked-meta">
                <span>Liquidity: ${fmtCurrency(t.liquidity_usd)}</span>
                <span>Market cap: ${fmtCurrency(t.market_cap)}</span>
                <span>Age: ${fmtAge(t.age_days, t.age_hours)}</span>
                <span>Holders: ${t.holder_count ?? "N/A"}</span>
              </div>
              ${tokenActions(t.contract_address)}
              ${badgeHtml(t.flagged_by)}
              ${topSignalHtml(t)}
            </div>
            <div class="score-badges">
              <div class="summary-opp-score" style="background: ${opportunityColor(t.alpha_score)}">
                <strong>${t.alpha_score ?? "–"}${t.scores_estimated ? "*" : ""}</strong>
                <span>OPPORTUNITY</span>
              </div>
              <div class="score-badge score-badge-sm" style="background: ${riskColor(t.risk_score)}">
                <strong>${t.risk_score ?? "–"}${t.scores_estimated ? "*" : ""}</strong>
                <span>RISK</span>
              </div>
            </div>
          </article>`,
        )
        .join("")}</div>
      ${tokens.some((t) => t.scores_estimated)
        ? `<p class="lore-meta">* estimated — same scoring engine, discovery data only (no on-chain verification)</p>`
        : ""}`;
    wireTokenActions(topRiskBox);
  } catch (error) {
    topRiskBox.innerHTML = `
      <h2 class="dash-section-title">Best opportunities right now</h2>
      <p class="dash-empty">Scan failed: ${esc(error.message)}</p>`;
  }
}

quickForm.addEventListener("submit", (event) => {
  event.preventDefault();
  const address = document.querySelector("#quick-address").value.trim();
  if (!address) return;
  document.dispatchEvent(new CustomEvent("rra:analyze", { detail: { address, sourceEl: null } }));
});

loadBanner();
loadWatchlistCounts();
loadTopOpportunities();
