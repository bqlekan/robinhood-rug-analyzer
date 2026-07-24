/** render/history.js — trend history from GET /api/v1/history/{address}.
 *  Snapshot list + simple sparkline of risk score / liquidity over stored
 *  snapshots. Read-only presentation; no scoring, no re-computation. */
import { esc, fmtCurrency, fmtPct } from "../ui.js";

// Minimal inline-SVG sparkline. `values` newest-last after we reverse. Returns
// "" when fewer than 2 points (a single dot is not a trend).
function sparkline(values, stroke) {
  const pts = values.filter((v) => v !== null && v !== undefined);
  if (pts.length < 2) return "";
  const w = 160;
  const hgt = 32;
  const min = Math.min(...pts);
  const max = Math.max(...pts);
  const span = max - min || 1;
  const step = w / (pts.length - 1);
  const coords = pts
    .map((v, i) => `${(i * step).toFixed(1)},${(hgt - ((v - min) / span) * hgt).toFixed(1)}`)
    .join(" ");
  return `<svg class="sparkline" width="${w}" height="${hgt}" viewBox="0 0 ${w} ${hgt}" role="img" aria-hidden="true" preserveAspectRatio="none">
    <polyline fill="none" stroke="${stroke}" stroke-width="2" points="${coords}" />
  </svg>`;
}

export function renderHistory(payload, mountEl) {
  const snapshots = payload?.snapshots || [];
  if (!snapshots.length) {
    mountEl.innerHTML = `
      <section>
        <h2>Trend History</h2>
        <p class="lore-meta">No stored snapshots yet. History builds up as this token is scanned over time.</p>
      </section>`;
    return;
  }

  // Store is newest-first; oldest-first reads left→right on the sparkline.
  const chrono = [...snapshots].reverse();
  const riskSpark = sparkline(chrono.map((s) => s.risk_score), "#ff5c7a");
  const liqSpark = sparkline(chrono.map((s) => s.liquidity_usd), "#38f58c");

  const rows = snapshots
    .map((s) => `
      <tr>
        <td>${esc(s.captured_at || "—")}</td>
        <td>${s.risk_score ?? "N/A"}</td>
        <td>${fmtCurrency(s.liquidity_usd)}</td>
        <td>${fmtPct(s.top10_percentage)}</td>
        <td>${s.holder_count ?? "N/A"}</td>
      </tr>`)
    .join("");

  mountEl.innerHTML = `
    <section>
      <h2>Trend History</h2>
      <div class="spark-row">
        <div class="spark-cell"><span class="label">Risk score</span>${riskSpark || '<span class="lore-meta">need ≥2 points</span>'}</div>
        <div class="spark-cell"><span class="label">Liquidity</span>${liqSpark || '<span class="lore-meta">need ≥2 points</span>'}</div>
      </div>
      <div class="history-scroll">
        <table class="history-table">
          <thead><tr><th>Captured</th><th>Risk</th><th>Liquidity</th><th>Top 10%</th><th>Holders</th></tr></thead>
          <tbody>${rows}</tbody>
        </table>
      </div>
    </section>`;
}
