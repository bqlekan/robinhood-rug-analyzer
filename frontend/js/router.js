/**
 * Client-side tab router + app-shell navigation. Reproduces the original tabbed
 * navigation (Ranked Scanner / Analyze / Smart Wallets) with the WAI-ARIA tabs
 * keyboard pattern. Modules are deferred, so the DOM is ready at import time.
 */
const tabs = Array.from(document.querySelectorAll(".tab"));
const panels = document.querySelectorAll(".tab-panel");

// Switch to a tab by its data-tab name. Central so cross-tab navigation
// (Smart Wallets -> Analyze) and keyboard nav both route through the same path.
export function activateTab(name) {
  tabs.forEach((t) => {
    const on = t.dataset.tab === name;
    t.classList.toggle("active", on);
    t.setAttribute("aria-selected", on ? "true" : "false");
    t.tabIndex = on ? 0 : -1;
  });
  panels.forEach((p) => p.classList.remove("active"));
  const panel = document.querySelector(`#tab-${name}`);
  if (panel) {
    panel.classList.add("active");
    // Re-trigger the subtle enter animation each time the panel is shown.
    panel.classList.remove("panel-enter");
    void panel.offsetWidth;
    panel.classList.add("panel-enter");
  }
}

// Register a callback the first time a given tab is opened (lazy loads).
const onceHandlers = {};
export function onTabFirstOpen(name, fn) {
  onceHandlers[name] = fn;
}

tabs.forEach((tab) => {
  tab.addEventListener("click", () => {
    activateTab(tab.dataset.tab);
    const fn = onceHandlers[tab.dataset.tab];
    if (fn) { delete onceHandlers[tab.dataset.tab]; fn(); }
  });
});

// Keyboard support: Left/Right/Home/End move focus between tabs (roving tabindex),
// matching the WAI-ARIA tabs pattern. Enter/Space already activate (native buttons).
const tablist = document.querySelector(".tabs");
if (tablist) {
  tablist.addEventListener("keydown", (e) => {
    const i = tabs.indexOf(document.activeElement);
    if (i === -1) return;
    let next = null;
    if (e.key === "ArrowRight") next = tabs[(i + 1) % tabs.length];
    else if (e.key === "ArrowLeft") next = tabs[(i - 1 + tabs.length) % tabs.length];
    else if (e.key === "Home") next = tabs[0];
    else if (e.key === "End") next = tabs[tabs.length - 1];
    if (next) {
      e.preventDefault();
      next.focus();
      activateTab(next.dataset.tab);
    }
  });
}
