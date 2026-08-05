/** Ranked Scanner page — POST /api/v1/scan, opportunity-first cards with client-side filters.
 *  Filters operate on already-returned data — no extra API calls.
 *  Sorts by Opportunity Score (alpha_score) by default. */
import { apiClient } from "../api.js";
import {
  esc, fmtCurrency, fmtAge, riskColor, opportunityColor, alphaSignalsHtml, badgeHtml,
  createProgress, lockButton, skeletonCards, tokenActions, wireTokenActions, toast,
} from "../ui.js";

const scanForm = document.querySelector("#scan-form");
const scanStatus = document.querySelector("#scan-status");
const scanResults = document.querySelector("#scan-results");
const scanFilters = document.querySelector("#scan-filters");
let scanning = false;
let lastTokens = []; // cached for re-filtering without re-fetching

function getFilters() {
  return {
    oppMin: Number(document.querySelector("#f-opp-min")?.value) || 0,
    riskMax: Number(document.querySelector("#f-risk-max")?.value) || 100,
    liqMin: Number(document.querySelector("#f-liq-min")?.value) || 0,
    ageMax: Number(document.querySelector("#f-age-max")?.value) || Infinity,
    verified: document.querySelector("#f-verified")?.value || "",
    honeypot: document.querySelector("#f-honeypot")?.value || "",
  };
}

function applyFilters(tokens) {
  const f = getFilters();
  return tokens.filter((t) => {
    if ((t.alpha_score ?? 0) < f.oppMin) return false;
    if (t.risk_score > f.riskMax) return false;
    if ((t.liquidity_usd ?? 0) < f.liqMin) return false;
    if (f.ageMax !== Infinity && (t.age_hours ?? Infinity) > f.ageMax) return false;
    if (f.verified === "yes" && !t.alpha_signals?.some((s) => s.name === "verified" && s.positive)) return false;
    if (f.verified === "no" && t.alpha_signals?.some((s) => s.name === "verified" && s.positive)) return false;
    if (f.honeypot === "yes" && !t.alpha_signals?.some((s) => s.name === "honeypot" && s.positive)) return false;
    return true;
  });
}

function renderRanked(tokens) {
  tokens.sort((a, b) => (b.alpha_score ?? 0) - (a.alpha_score ?? 0));
  if (!tokens.length) {
    scanResults.innerHTML = `<div class="empty-state"><strong>No qualifying launches found</strong>Try adjusting your filters or increasing the scan window.</div>`;
    return;
  }
  scanResults.innerHTML = tokens
    .map(
      (t, i) => `
      <article class="ranked-card" data-address="${esc(t.contract_address)}" style="border-left: 5px solid ${opportunityColor(t.alpha_score)}">
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
          ${alphaSignalsHtml(t.alpha_signals)}
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
    .join("");

  if (tokens.some((t) => t.scores_estimated)) {
    scanResults.insertAdjacentHTML(
      "beforeend",
      `<p class="lore-meta">* estimated — same scoring engine, discovery data only (no on-chain verification)</p>`,
    );
  }

  wireTokenActions(scanResults);
}

// Re-filter on any filter input change (no API call).
if (scanFilters) {
  scanFilters.addEventListener("input", () => {
    if (lastTokens.length) renderRanked(applyFilters(lastTokens));
  });
}

scanForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  if (scanning) return;
  scanning = true;
  const submitBtn = scanForm.querySelector("button[type=submit]");
  const release = lockButton(submitBtn, "Scanning…");
  const limit = Number(document.querySelector("#scan-limit").value) || 10;
  const includeLore = document.querySelector("#scan-lore").checked;
  const progress = createProgress(scanStatus, [
    "Scanning launches…",
    "Analysing contracts…",
    "Checking holders…",
    "Scoring developers…",
    "Evaluating smart wallets…",
    "Building opportunity scores…",
    "Finalizing…",
  ]);
  scanResults.innerHTML = skeletonCards(Math.min(limit, 6));

  try {
    const data = await apiClient.scan(limit, includeLore);
    lastTokens = data.ranked_tokens || [];
    progress.finish(data.message);
    if (scanFilters) scanFilters.hidden = false;
    renderRanked(applyFilters(lastTokens));
  } catch (error) {
    progress.fail(`Scan failed: ${error.message}`);
    toast(`Scan failed: ${error.message}`, "error");
    scanResults.innerHTML = "";
  } finally {
    scanning = false;
    release();
  }
});
