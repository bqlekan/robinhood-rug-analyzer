/** Smart Wallets page — GET /api/v1/watchlist (+ refresh), render wallet cards. */
import { apiClient } from "../api.js";
import { onTabFirstOpen } from "../router.js";
import {
  esc, shortAddr, createProgress, lockButton, skeletonCards,
  tokenActions, wireTokenActions, toast,
} from "../ui.js";

const walletsForm = document.querySelector("#wallets-form");
const walletsResults = document.querySelector("#wallets-results");
const walletsNote = document.querySelector("#wallets-note");
const walletsLoadBtn = walletsForm.querySelector("button[type=submit]");
const walletsRefreshBtn = document.querySelector("#wallets-refresh");
let walletsBusy = false;

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
  try {
    const data = await apiClient.watchlist(kind, sort);
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
    toast(`Watchlist failed: ${error.message}`, "error");
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
    await apiClient.watchlistRefresh();
    progress.finish("Refreshed. Reloading…");
  } catch (error) {
    progress.fail(`Refresh failed: ${error.message}`);
    toast(`Refresh failed: ${error.message}`, "error");
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

// Auto-load the watchlist the first time its tab is opened.
onTabFirstOpen("wallets", loadWatchlist);
