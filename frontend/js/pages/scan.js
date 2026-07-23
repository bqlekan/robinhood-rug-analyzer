/** Ranked Scanner page — POST /api/v1/scan, render risk-ranked token cards. */
import { apiClient } from "../api.js";
import {
  esc, fmtCurrency, fmtAge, riskColor, badgeHtml,
  createProgress, lockButton, skeletonCards, tokenActions, wireTokenActions, toast,
} from "../ui.js";

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
    const data = await apiClient.scan(limit, includeLore);
    progress.finish(data.message);
    renderRanked(data.ranked_tokens);
  } catch (error) {
    progress.fail(`Scan failed: ${error.message}`);
    toast(`Scan failed: ${error.message}`, "error");
    scanResults.innerHTML = "";
  } finally {
    scanning = false;
    release();
  }
});
