/** Dashboard page (F2) — landing overview from existing endpoints only:
 *  chain banner (/chain) + liveness dot (/health), top-risk panel (/scan),
 *  watchlist counts (/watchlist), quick-analyze deep-link into the Analyze tab.
 *  No invented metrics: every number traces to a real response field. */
import { apiClient, chainInfo } from "../api.js";
import {
  esc, safeUrl, fmtCurrency, fmtAge, riskColor,
  skeletonCards, tokenActions, wireTokenActions, badgeHtml,
} from "../ui.js";

const chainBox = document.querySelector("#dashboard-chain");
const statRow = document.querySelector("#dashboard-watchlist");
const topRiskBox = document.querySelector("#dashboard-top-risk");
const quickForm = document.querySelector("#quick-analyze-form");

const TOP_RISK_LIMIT = 5; // small default per F2; full control lives in Ranked Scanner

// --- Chain banner + liveness dot ---
async function loadBanner() {
  chainBox.innerHTML = `
    <span class="dash-live"><span class="dash-dot" aria-hidden="true"></span> checking…</span>
    <span class="dash-chain-meta">Loading chain info…</span>`;
  // Independent fetches: a dead backend still renders a banner with a red dot.
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

// --- Watchlist snapshot counts ---
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

// --- Top-risk panel (small scan; rows link into Analyze like the scanner) ---
async function loadTopRisk() {
  topRiskBox.innerHTML = `
    <h2 class="dash-section-title">Top risk right now</h2>
    ${skeletonCards(3)}`;
  try {
    const data = await apiClient.scan(TOP_RISK_LIMIT, false);
    const tokens = data.ranked_tokens || [];
    if (!tokens.length) {
      topRiskBox.innerHTML = `
        <h2 class="dash-section-title">Top risk right now</h2>
        <p class="dash-empty">No active tokens returned by the scan.</p>`;
      return;
    }
    topRiskBox.innerHTML = `
      <h2 class="dash-section-title">Top risk right now</h2>
      <div class="ranked-list">${tokens
        .map(
          (t, i) => `
          <article class="ranked-card" data-address="${esc(t.contract_address)}" style="border-left: 5px solid ${riskColor(t.risk_score)}">
            <div class="rank">#${i + 1}</div>
            <div class="ranked-main">
              <strong><button type="button" class="token-name" data-address="${esc(t.contract_address)}" data-symbol="${esc(t.symbol || t.name || "")}" title="Analyze this token">${esc(t.name || "Unknown")}</button> <span class="sym">${esc(t.symbol || "")}</span></strong>
              <code class="addr" data-address="${esc(t.contract_address)}" title="Analyze this token">${esc(t.contract_address)}</code>
              <div class="ranked-meta">
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
        .join("")}</div>`;
    wireTokenActions(topRiskBox);
  } catch (error) {
    topRiskBox.innerHTML = `
      <h2 class="dash-section-title">Top risk right now</h2>
      <p class="dash-empty">Scan failed: ${esc(error.message)}</p>`;
  }
}

// --- Quick analyze: deep-link into the Analyze tab via the shared event ---
quickForm.addEventListener("submit", (event) => {
  event.preventDefault();
  const address = document.querySelector("#quick-address").value.trim();
  if (!address) return;
  document.dispatchEvent(new CustomEvent("rra:analyze", { detail: { address, sourceEl: null } }));
});

// Dashboard is the default tab — load its panels immediately.
loadBanner();
loadWatchlistCounts();
loadTopRisk();
