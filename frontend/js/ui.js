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
