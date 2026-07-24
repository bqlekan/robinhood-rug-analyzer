/** Smart Wallets page (F4) — watchlist + per-wallet detail.
 *  Endpoints: GET /api/v1/watchlist, GET /api/v1/wallet/{address},
 *             POST /api/v1/watchlist/refresh.
 *  Filter/sort round-trip to server (server whitelists both params).
 *  Each discovered token is clickable → Analyze (rra:analyze event). */
import { apiClient } from "../api.js";
import { onTabFirstOpen } from "../router.js";
import {
  esc, shortAddr, createProgress, lockButton, skeletonCards,
  tokenActions, wireTokenActions, toast,
} from "../ui.js";

const walletsForm      = document.querySelector("#wallets-form");
const walletsResults   = document.querySelector("#wallets-results");
const walletsNote      = document.querySelector("#wallets-note");
const walletsLoadBtn   = walletsForm.querySelector("button[type=submit]");
const walletsRefreshBtn = document.querySelector("#wallets-refresh");
let walletsBusy = false;

// --- Per-wallet detail panel ---
// Fetches /wallet/{address} and renders into a detail div inside the card.
async function loadWalletDetail(address, detailEl) {
  detailEl.textContent = "Loading detail…";
  try {
    const w = await apiClient.wallet(address);
    const rows = (w.recent_buys || [])
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
    detailEl.innerHTML = `
      <div class="wallet-detail-meta">
        ${w.first_seen ? `<span>First seen: ${esc(w.first_seen)}</span>` : ""}
        ${w.last_refreshed ? `<span>Last refreshed: ${esc(w.last_refreshed)}</span>` : ""}
        ${w.prior_tokens ? `<span>Seen on ${esc(w.prior_tokens)} token${w.prior_tokens === 1 ? "" : "s"}</span>` : ""}
      </div>
      <div class="token-hits">${rows || '<span class="chip">no recent buys tracked</span>'}</div>`;
    wireTokenActions(detailEl);
  } catch (err) {
    detailEl.textContent = `Detail unavailable: ${err.message}`;
  }
}

function walletCard(w) {
  const kindColor = w.kind === "smart" ? "#38f58c" : "#ffd166";
  const kindBg    = w.kind === "smart" ? "#146c3a" : "#8a6d1f";
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
    <article class="ranked-card" style="border-left: 5px solid ${kindColor}">
      <div class="ranked-main">
        <strong>${esc(w.label || w.kind)}${w.proxy_score != null ? ` · proxy ${esc(w.proxy_score)}` : ""}</strong>
        <code class="wallet-addr">${esc(w.address)}</code>
        <div class="ranked-meta"><span>${w.prior_tokens ? `Seen on ${esc(w.prior_tokens)} token${w.prior_tokens === 1 ? "" : "s"} · ` : ""}Recently buying:</span></div>
        <div class="token-hits">${buyRows || '<span class="chip">no recent buys tracked</span>'}</div>
        <div class="wallet-detail-wrap">
          <button type="button" class="tok-btn wallet-detail-btn" data-address="${esc(w.address)}">Show detail</button>
          <div class="wallet-detail" hidden></div>
        </div>
      </div>
      <div class="score-badge" style="background: ${kindBg}">
        <span>${esc((w.kind || "").toUpperCase())}</span>
      </div>
    </article>`;
}

function wireDetailButtons(container) {
  container.querySelectorAll(".wallet-detail-btn").forEach((btn) => {
    btn.addEventListener("click", async () => {
      const detailEl = btn.nextElementSibling;
      if (!detailEl.hidden) { detailEl.hidden = true; btn.textContent = "Show detail"; return; }
      detailEl.hidden = false;
      btn.textContent = "Hide detail";
      if (!detailEl.dataset.loaded) {
        detailEl.dataset.loaded = "1";
        await loadWalletDetail(btn.dataset.address, detailEl);
      }
    });
  });
}

async function loadWatchlist() {
  if (walletsBusy) return;
  walletsBusy = true;
  const release = lockButton(walletsLoadBtn, "Loading…");
  walletsRefreshBtn.disabled = true;
  const progress = createProgress(walletsNote, [
    "Loading watchlist…",
    "Reading wallet intelligence…",
    "Finalizing…",
  ]);
  walletsResults.innerHTML = skeletonCards(3);
  const kind = document.querySelector("#wallets-kind")?.value || "";
  const sort = document.querySelector("#wallets-sort")?.value || "score";
  try {
    const data = await apiClient.watchlist(kind, sort);
    progress.finish(data.note);
    const smart   = (data.smart_wallets   || []).map(walletCard).join("");
    const insider = (data.insider_wallets || []).map(walletCard).join("");
    walletsResults.innerHTML = `
      <p class="lore-meta">Smart-wallet scores are heuristic estimates from free on-chain behavior (early entry, position distribution, surviving cross-token holdings), not verified ROI.</p>
      <h2>Smart Wallets (estimated)</h2>
      <div class="ranked-list">${smart   || "<p class='lore-meta'>No smart wallets found yet. Analyze or scan tokens to populate the watchlist.</p>"}</div>
      <h2>Insider Wallets</h2>
      <div class="ranked-list">${insider || "<p class='lore-meta'>No insider wallets flagged yet.</p>"}</div>`;
    walletsResults.classList.add("result-enter");
    wireTokenActions(walletsResults);
    wireDetailButtons(walletsResults);
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

walletsForm.addEventListener("submit", (event) => { event.preventDefault(); loadWatchlist(); });
walletsRefreshBtn.addEventListener("click", refreshWatchlistFromChain);
onTabFirstOpen("wallets", loadWatchlist);
