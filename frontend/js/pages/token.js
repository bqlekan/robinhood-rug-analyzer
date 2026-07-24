/** Token Analysis page (F3) — POST /api/v1/analyze + GET /api/v1/history/{address}.
 *  Modular: render helpers live in js/render/analysis.js and js/render/history.js.
 *  The key fix over the F1 token.js: progress.finish() is called AFTER renderAnalysis()
 *  so the 350ms cleanup timer never races with the freshly written #result DOM. */
import { apiClient } from "../api.js";
import { activateTab } from "../router.js";
import { renderAnalysis } from "../render/analysis.js";
import { renderHistory } from "../render/history.js";
import {
  esc, shortAddr, createProgress, lockButton, skeletonAnalysis, toast,
} from "../ui.js";

const analyzeForm = document.querySelector("#analyze-form");
const result      = document.querySelector("#result");
const analyzeStatus = document.querySelector("#analyze-status");

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

let analyzing = false;

analyzeForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  if (analyzing) return;
  const contractAddress = document.querySelector("#contract-address").value.trim();
  if (!contractAddress) return;
  const includeLore = document.querySelector("#analyze-lore").checked;

  analyzing = true;
  const submitBtn = analyzeForm.querySelector("button[type=submit]");
  const release = lockButton(submitBtn, "Analyzing…");
  const progress = createProgress(analyzeStatus, ANALYZE_STEPS);
  result.innerHTML = skeletonAnalysis();

  try {
    const data = await apiClient.analyze(contractAddress, includeLore);
    // Render BEFORE finishing progress so the 350ms cleanup timer never touches #result.
    renderAnalysis(data, result);
    progress.finish("Analysis complete.");
    result.classList.add("result-enter");
    recordRecent(contractAddress, data.market_data?.base_token_symbol);
    requestAnimationFrame(() => result.scrollIntoView({ behavior: "smooth", block: "start" }));
    // Load history into the #history-view placeholder renderAnalysis() left behind.
    loadHistory(contractAddress);
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

async function loadHistory(address) {
  const mount = document.querySelector("#history-view");
  if (!mount) return;
  try {
    const payload = await apiClient.history(address, 50);
    renderHistory(payload, mount);
  } catch {
    mount.innerHTML = `<section><h2>Trend History</h2><p class="lore-meta">History unavailable.</p></section>`;
  }
}

// Cross-tab navigation: populate form, switch tab, auto-submit.
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

// --- Recent searches ---
const RECENT_KEY = "rra_recent_v1";
const recentBox  = document.querySelector("#recent-searches");

function loadRecent() {
  try {
    const arr = JSON.parse(localStorage.getItem(RECENT_KEY) || "[]");
    return Array.isArray(arr) ? arr : [];
  } catch { return []; }
}

function recordRecent(address, symbol) {
  const addr = (address || "").trim();
  if (!addr) return;
  let items = loadRecent().filter((it) => it.address.toLowerCase() !== addr.toLowerCase());
  items.unshift({ address: addr, symbol: symbol || "", at: Date.now() });
  items = items.slice(0, 10);
  try { localStorage.setItem(RECENT_KEY, JSON.stringify(items)); } catch { /* quota */ }
  renderRecent();
}

function renderRecent() {
  const items = loadRecent();
  if (!items.length) { recentBox.hidden = true; recentBox.innerHTML = ""; return; }
  recentBox.hidden = false;
  recentBox.innerHTML = `
    <span class="recent-label">Recent searches</span>
    <div class="recent-chips">${items
      .map((it) => `<button type="button" class="recent-chip" data-address="${esc(it.address)}" title="${esc(it.address)}">${esc(it.symbol || shortAddr(it.address))}</button>`)
      .join("")}</div>`;
  recentBox.querySelectorAll(".recent-chip").forEach((el) => {
    el.addEventListener("click", () => analyzeAddress(el.dataset.address, el));
  });
}

renderRecent();
