/**
 * Shared UI primitives — escaping, formatting, progress, skeletons, toasts,
 * token-action rows. Reused by every page module. No page-specific logic here.
 */
import { blockscoutTokenUrl, dexscreenerUrl } from "./api.js";

// Escape untrusted text before it goes into innerHTML. Token names/symbols come
// from on-chain metadata and lore titles/urls from web search — all attacker-controllable.
export function esc(value) {
  if (value === null || value === undefined) return "";
  return String(value)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#39;");
}

// Only allow http(s) URLs into href attributes; block javascript:/data: and junk.
export function safeUrl(url) {
  if (!url) return "#";
  try {
    const u = new URL(url, window.location.origin);
    return u.protocol === "http:" || u.protocol === "https:" ? url : "#";
  } catch {
    return "#";
  }
}

export function fmtCurrency(value) {
  if (value === null || value === undefined) return "N/A";
  return new Intl.NumberFormat("en-US", {
    style: "currency",
    currency: "USD",
    maximumFractionDigits: 2,
  }).format(value);
}

export function fmtPct(value) {
  return value === null || value === undefined ? "N/A" : `${value}%`;
}

// Age as "Xd Yh" (or just hours when under a day), preferring exact hours when available.
export function fmtAge(days, hours) {
  const totalHours =
    hours !== null && hours !== undefined
      ? hours
      : days !== null && days !== undefined
        ? days * 24
        : null;
  if (totalHours === null) return "N/A";
  const d = Math.floor(totalHours / 24);
  const h = Math.round(totalHours % 24);
  return d > 0 ? `${d}d ${h}h` : `${h}h`;
}

// Map a 0-100 risk score onto a smooth red -> green gradient (green = low risk).
export function riskColor(score) {
  const s = Math.max(0, Math.min(100, score ?? 0));
  const hue = 130 - (130 * s) / 100;
  return `hsl(${hue}, 75%, 45%)`;
}

// Map a 0-100 alpha/opportunity score onto a green gradient (higher = greener).
export function alphaColor(score) {
  const s = Math.max(0, Math.min(100, score ?? 0));
  const hue = (130 * s) / 100;
  return `hsl(${hue}, 75%, 40%)`;
}

// Opportunity Score color tiers (distinct from risk palette).
export function opportunityColor(score) {
  if (score == null) return "var(--muted)";
  if (score >= 90) return "var(--opp-excellent)";
  if (score >= 75) return "var(--opp-good)";
  if (score >= 60) return "var(--opp-moderate)";
  if (score >= 40) return "var(--opp-cautious)";
  return "var(--opp-poor)";
}

export function opportunityLevel(score) {
  if (score == null) return "Unknown";
  if (score >= 90) return "Excellent";
  if (score >= 75) return "Good";
  if (score >= 60) return "Moderate";
  if (score >= 40) return "Cautious";
  return "Poor";
}

// Build explanation badges from existing analysis fields. Display-only — no scoring.
export function opportunityBadges(data) {
  const badges = [];
  const add = (label, kind) => badges.push({ label, kind });
  if (data.watchlist_hits?.some((h) => h.kind === "smart")) add("Strong Smart Wallets", "positive");
  if (data.developer_reputation?.score >= 60) add("Proven Developer", "positive");
  if (data.market_data?.liquidity?.usd >= 10000) add("Healthy Liquidity", "positive");
  if (data.holders?.holder_count >= 50) add("Growing Holders", "positive");
  if (data.contract_intel?.verified) add("Verified Contract", "positive");
  if (data.honeypot?.status === "sellable") add("Honeypot Safe", "positive");
  if (data.liquidity_lock?.status?.toLowerCase().includes("locked")) add("LP Locked", "positive");
  if (data.contract_privileges?.ownership_renounced === true) add("Renounced", "positive");
  if (data.launchpad?.name && data.launchpad.name !== "Unknown") add("Launchpad", "info");
  if (data.token_age?.age_hours != null && data.token_age.age_hours < 24) add("Fresh Launch", "info");
  if (data.holders?.top1_percentage >= 30) add("Whale Concentration", "warning");
  if (data.developer_reputation?.score != null && data.developer_reputation.score < 30) add("Low History", "warning");
  if (data.market_data?.liquidity?.usd != null && data.market_data.liquidity.usd < 1000) add("Young Liquidity", "warning");
  return badges;
}

export function opportunityBadgesHtml(data) {
  const badges = opportunityBadges(data);
  if (!badges.length) return "";
  return `<div class="opp-badges">${badges.map((b) =>
    `<span class="opp-badge ${b.kind}">${b.kind === "positive" ? "✓" : b.kind === "warning" ? "⚠" : "ℹ"} ${esc(b.label)}</span>`
  ).join("")}</div>`;
}

// Single horizontal health bar (display-only).
export function healthBar(label, score, color) {
  const v = Math.max(0, Math.min(100, score ?? 0));
  return `<div class="health-bar">
    <span class="health-bar-label">${esc(label)}</span>
    <div class="health-bar-track"><div class="health-bar-fill" style="width:${v}%;background:${color}"></div></div>
    <span class="health-bar-value">${score != null ? v : "–"}</span>
  </div>`;
}

// Generate a 1-2 sentence summary from analysis evidence (display-only template).
export function summaryText(data) {
  const opp = data.alpha_score;
  const risk = data.analysis?.risk_score;
  const parts = [];

  if (opp != null && opp >= 75) parts.push("Excellent early opportunity.");
  else if (opp != null && opp >= 50) parts.push("Interesting but speculative.");
  else if (opp != null && opp >= 25) parts.push("Moderate opportunity with caveats.");
  else parts.push("Limited opportunity signals.");

  const positives = [];
  const negatives = [];
  if (data.developer_reputation?.score >= 60) positives.push("strong developer history");
  if (data.watchlist_hits?.some((h) => h.kind === "smart")) positives.push("quality wallet participation");
  if (data.market_data?.liquidity?.usd >= 10000) positives.push("healthy liquidity");
  if (data.honeypot?.status === "sellable") positives.push("sellable token");
  if (data.contract_intel?.verified) positives.push("verified contract");
  if (data.developer_reputation?.score != null && data.developer_reputation.score < 30) negatives.push("weak developer reputation");
  if (risk != null && risk >= 60) negatives.push("elevated risk");
  if (data.market_data?.liquidity?.usd != null && data.market_data.liquidity.usd < 1000) negatives.push("low liquidity");

  if (positives.length && negatives.length) {
    parts.push(`${capitalize(positives.slice(0, 2).join(" and "))} but ${negatives.slice(0, 2).join(" and ")}.`);
  } else if (positives.length) {
    parts.push(`${capitalize(positives.slice(0, 3).join(", "))}.`);
  } else if (negatives.length) {
    parts.push(`${capitalize(negatives.slice(0, 2).join(" and "))}.`);
  }
  return parts.join(" ");
}

function capitalize(s) { return s ? s.charAt(0).toUpperCase() + s.slice(1) : ""; }

export function alphaSignalsHtml(signals) {
  if (!signals || !signals.length) return "";
  return `<div class="alpha-signals">${signals
    .map((s) => `<span class="alpha-signal ${s.positive ? "pos" : "neg"}">${s.positive ? "+" : "−"} ${esc(s.detail)}</span>`)
    .join("")}</div>`;
}

export function shortAddr(addr) {
  if (!addr) return "N/A";
  return `${addr.slice(0, 6)}…${addr.slice(-4)}`;
}

export function badgeHtml(hits) {
  if (!hits || !hits.length) return "";
  const smart = hits.filter((h) => h.kind === "smart").length;
  const insider = hits.filter((h) => h.kind === "insider").length;
  const parts = [];
  if (smart) parts.push(`<span class="wallet-badge smart">${smart} smart</span>`);
  if (insider) parts.push(`<span class="wallet-badge insider">${insider} insider</span>`);
  return `<div class="wallet-badges">${parts.join("")}</div>`;
}

// --- Progress controller (indeterminate, staged status text) ---
// The backend exposes no progress stream, so this drives a high-quality
// indeterminate bar plus rotating status lines while a request is in flight,
// then snaps to 100% on success. One controller per target element.
export function createProgress(container, steps) {
  container.classList.add("status", "progress-host");
  container.setAttribute("aria-busy", "true");
  container.innerHTML = `
    <div class="progress-line" aria-hidden="true">
      <div class="progress-bar"><div class="progress-fill indeterminate"></div></div>
    </div>
    <div class="progress-text">${esc(steps[0] || "Working…")}</div>`;
  const fill = container.querySelector(".progress-fill");
  const text = container.querySelector(".progress-text");
  let idx = 0;
  const timer = setInterval(() => {
    idx = Math.min(idx + 1, steps.length - 1);
    text.textContent = steps[idx];
  }, 900);
  return {
    finish(message) {
      clearInterval(timer);
      fill.classList.remove("indeterminate");
      fill.classList.add("done");
      fill.style.width = "100%";
      text.textContent = message || "Done.";
      container.removeAttribute("aria-busy");
      setTimeout(() => {
        container.classList.remove("progress-host");
        container.innerHTML = "";
        container.textContent = message || "";
      }, 350);
    },
    fail(message) {
      clearInterval(timer);
      container.classList.remove("progress-host");
      container.removeAttribute("aria-busy");
      container.innerHTML = "";
      container.textContent = message;
    },
  };
}

// Button lock: disable + swap label to loading text, restore on release. Combined
// with a per-action in-flight flag this prevents duplicate requests (including via
// requestSubmit(), which fires even when the button is disabled).
export function lockButton(btn, loadingText) {
  if (!btn) return () => {};
  const original = btn.textContent;
  btn.disabled = true;
  btn.classList.add("is-loading");
  btn.textContent = loadingText;
  return () => {
    btn.disabled = false;
    btn.classList.remove("is-loading");
    btn.textContent = original;
  };
}

// Skeleton placeholders shown while a fetch is in flight. Purely visual (aria-hidden).
export function skeletonCards(count) {
  return `<div class="skeleton-wrap" aria-hidden="true">${Array.from({ length: count })
    .map(
      () => `<div class="skeleton-card">
        <div class="skeleton-line w40"></div>
        <div class="skeleton-line w70"></div>
        <div class="skeleton-line w55"></div>
      </div>`,
    )
    .join("")}</div>`;
}

export function skeletonAnalysis() {
  return `<div class="skeleton-wrap" aria-hidden="true">
    <div class="skeleton-grid">${Array.from({ length: 5 })
      .map(() => `<div class="skeleton-card"><div class="skeleton-line w50"></div><div class="skeleton-line w80"></div></div>`)
      .join("")}</div>
    <div class="skeleton-grid">${Array.from({ length: 8 })
      .map(() => `<div class="skeleton-card"><div class="skeleton-line w60"></div><div class="skeleton-line w40"></div></div>`)
      .join("")}</div>
  </div>`;
}

// --- Token action row (copy / Blockscout / DexScreener) ---
// Every discovered token reuses the contract the backend already returned — no
// extra lookup. External links open in a new tab.
export function tokenActions(address) {
  const a = esc(address);
  return `<div class="token-actions" role="group" aria-label="Token actions">
    <button type="button" class="tok-btn copy-addr" data-address="${a}" aria-label="Copy contract address">Copy</button>
    <a class="tok-btn" href="${safeUrl(blockscoutTokenUrl(address))}" target="_blank" rel="noopener" aria-label="Open on Blockscout">Blockscout</a>
    <a class="tok-btn" href="${safeUrl(dexscreenerUrl(address))}" target="_blank" rel="noopener" aria-label="Open on DexScreener">DexScreener</a>
  </div>`;
}

export async function copyToClipboard(text, btn) {
  try {
    await navigator.clipboard.writeText(text);
  } catch {
    // Fallback for non-secure contexts / older browsers.
    const ta = document.createElement("textarea");
    ta.value = text;
    ta.style.position = "fixed";
    ta.style.opacity = "0";
    document.body.appendChild(ta);
    ta.select();
    try { document.execCommand("copy"); } catch { /* ignore */ }
    document.body.removeChild(ta);
  }
  if (btn) {
    const prev = btn.textContent;
    btn.textContent = "Copied!";
    btn.classList.add("copied");
    setTimeout(() => { btn.textContent = prev; btn.classList.remove("copied"); }, 1200);
  }
}

// Delegate clicks for token name/address (→ analyze) and Copy buttons within a
// container. Navigation is decoupled via a DOM event so ui.js needn't import the
// token page (avoids a circular import); the token page listens for "rra:analyze".
export function wireTokenActions(container) {
  container.querySelectorAll(".token-name, .addr").forEach((el) => {
    el.addEventListener("click", () => {
      const card = el.closest("[data-address]") || el;
      document.dispatchEvent(
        new CustomEvent("rra:analyze", { detail: { address: el.dataset.address, sourceEl: card } }),
      );
    });
  });
  container.querySelectorAll(".copy-addr").forEach((el) => {
    el.addEventListener("click", (e) => {
      e.stopPropagation();
      copyToClipboard(el.dataset.address, el);
    });
  });
}

// --- Toasts: transient global messages (F1 shared primitive) ---
function toastHost() {
  let host = document.querySelector(".toast-host");
  if (!host) {
    host = document.createElement("div");
    host.className = "toast-host";
    host.setAttribute("role", "status");
    host.setAttribute("aria-live", "polite");
    document.body.appendChild(host);
  }
  return host;
}

export function toast(message, kind = "info", ms = 4000) {
  const el = document.createElement("div");
  el.className = `toast ${kind}`;
  el.textContent = message;
  toastHost().appendChild(el);
  setTimeout(() => el.remove(), ms);
}
