/** KOL Intelligence page (F5) — read-only view of existing kol_store state.
 *  Endpoints: GET /api/v1/kol/kols, /kol/projects, /kol/projects/{p}/{k},
 *             /kol/events?handle=, /kol/clusters?account_key=
 *  Degrades to "engine disabled" when kol_intel_enabled=False. */
import { onTabFirstOpen } from "../router.js";
import { esc, fmtCurrency, skeletonCards, toast } from "../ui.js";

const kolResults = document.querySelector("#kol-results");

async function _get(path) {
  const r = await fetch(path);
  const d = await r.json();
  if (!r.ok) throw new Error(d.detail || `Request failed (${r.status})`);
  return d;
}

function disabledState() {
  kolResults.innerHTML = `
    <p class="lore-meta">KOL Intelligence engine is disabled or has no data yet.
    Enable <code>kol_intel_enabled</code> and run a capture cycle to populate this view.</p>`;
}

function kolRow(k) {
  return `
    <article class="ranked-card">
      <div class="ranked-main">
        <strong>${esc(k.display_name || k.handle)}</strong>
        <span class="sym">@${esc(k.handle)} · ${esc(k.platform)}</span>
        <div class="ranked-meta">
          <span>Tier ${esc(k.tier)}</span>
          <span>Status: ${esc(k.status)}</span>
          ${k.last_checked ? `<span>Last checked: ${esc(k.last_checked)}</span>` : ""}
          <span>${k.enabled ? "Enabled" : "Disabled"}</span>
        </div>
      </div>
    </article>`;
}

function projectRow(p) {
  return `
    <article class="ranked-card kol-project" data-platform="${esc(p.platform)}" data-account="${esc(p.account_key)}">
      <div class="ranked-main">
        <strong><button type="button" class="token-name kol-proj-btn" data-platform="${esc(p.platform)}" data-account="${esc(p.account_key)}">${esc(p.project_handle || p.account_key)}</button></strong>
        <span class="sym">${esc(p.platform)} · ${esc(p.classification || "unknown")}</span>
        <div class="ranked-meta">
          <span>Score: ${p.score ?? "N/A"}</span>
          <span>Confidence: ${esc(p.confidence || "N/A")}</span>
          <span>KOLs: ${p.kol_count ?? 0}</span>
          ${p.updated_at ? `<span>Updated: ${esc(p.updated_at)}</span>` : ""}
        </div>
      </div>
      <div class="score-badge" style="background: var(--surface-2)">
        <strong>${p.score ?? "—"}</strong>
        <span>SCORE</span>
      </div>
    </article>`;
}

function eventRow(e) {
  return `
    <li class="signal">
      <strong>${esc(e.kind === "follow" ? "Follow" : "Crypto")} · ${esc(e.event_type)}</strong>
      <span>${esc(e.account_key || "")}${e.detected_at ? ` · ${esc(e.detected_at)}` : ""}</span>
    </li>`;
}

async function loadProjectDetail(platform, accountKey, detailEl) {
  detailEl.textContent = "Loading…";
  try {
    const [proj, evts, clusters] = await Promise.all([
      _get(`/api/v1/kol/projects/${encodeURIComponent(platform)}/${encodeURIComponent(accountKey)}`),
      _get(`/api/v1/kol/events?platform=${encodeURIComponent(platform)}&handle=${encodeURIComponent(accountKey)}&limit=20`),
      _get(`/api/v1/kol/clusters?platform=${encodeURIComponent(platform)}&account_key=${encodeURIComponent(accountKey)}&limit=10`),
    ]);
    const p = proj.project;
    const evidence = (p?.evidence || [])
      .map((e) => `<li class="signal"><strong>${esc(e.kol_handle)}</strong><span>${esc(e.event_type)} · weight ${esc(e.weight)}</span></li>`)
      .join("");
    const timeline = (p?.timeline || [])
      .map((t) => `<li class="signal"><strong>${t.score}</strong><span>${esc(t.confidence)} · ${esc(t.when || "")}</span></li>`)
      .join("");
    const events = (evts.events || []).map(eventRow).join("");
    const clusterList = (clusters.clusters || [])
      .map((c) => `<li class="signal"><strong>${esc(c.account_key)}</strong><span>${esc((c.cluster_types || []).join(", "))} · ${c.kol_count} KOLs</span></li>`)
      .join("");
    detailEl.innerHTML = `
      ${evidence ? `<h3 style="margin:.5rem 0 .25rem">Evidence</h3><ul class="signals">${evidence}</ul>` : ""}
      ${timeline ? `<h3 style="margin:.5rem 0 .25rem">Score history</h3><ul class="signals">${timeline}</ul>` : ""}
      ${events ? `<h3 style="margin:.5rem 0 .25rem">Recent events</h3><ul class="signals">${events}</ul>` : ""}
      ${clusterList ? `<h3 style="margin:.5rem 0 .25rem">Cluster history</h3><ul class="signals">${clusterList}</ul>` : ""}
      ${!evidence && !timeline && !events && !clusterList ? '<p class="lore-meta">No detail data available yet.</p>' : ""}`;
  } catch (err) {
    detailEl.textContent = `Detail unavailable: ${err.message}`;
  }
}

async function loadKol() {
  kolResults.innerHTML = skeletonCards(3);
  try {
    const [kolsData, projectsData] = await Promise.all([
      _get("/api/v1/kol/kols"),
      _get("/api/v1/kol/projects"),
    ]);
    if (!kolsData.enabled) { disabledState(); return; }
    const kols = kolsData.kols || [];
    const projects = projectsData.projects || [];
    kolResults.innerHTML = `
      <h2>Watched KOLs (${kols.length})</h2>
      <div class="ranked-list">${kols.length ? kols.map(kolRow).join("") : '<p class="lore-meta">No KOLs registered yet.</p>'}</div>
      <h2 style="margin-top:1.5rem">Project Intelligence (${projects.length})</h2>
      <div class="ranked-list" id="kol-projects-list">${projects.length ? projects.map(projectRow).join("") : '<p class="lore-meta">No project intelligence yet.</p>'}</div>`;
    // Wire project expand buttons.
    kolResults.querySelectorAll(".kol-proj-btn").forEach((btn) => {
      btn.addEventListener("click", async () => {
        const card = btn.closest(".kol-project");
        let detailEl = card.querySelector(".kol-proj-detail");
        if (!detailEl) {
          detailEl = document.createElement("div");
          detailEl.className = "kol-proj-detail wallet-detail";
          card.querySelector(".ranked-main").appendChild(detailEl);
        }
        if (!detailEl.hidden && detailEl.dataset.loaded) {
          detailEl.hidden = true; return;
        }
        detailEl.hidden = false;
        if (!detailEl.dataset.loaded) {
          detailEl.dataset.loaded = "1";
          await loadProjectDetail(btn.dataset.platform, btn.dataset.account, detailEl);
        }
      });
    });
  } catch (err) {
    toast(`KOL load failed: ${err.message}`, "error");
    kolResults.innerHTML = `<p class="lore-meta">Failed to load KOL intelligence: ${esc(err.message)}</p>`;
  }
}

onTabFirstOpen("kol", loadKol);
