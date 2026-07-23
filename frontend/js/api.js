/**
 * apiClient — single module wrapping every backend endpoint.
 * Base URL is same-origin (FastAPI serves both API and static files).
 * All helpers return parsed JSON or throw an Error with a human message.
 */

// Chain info cached after first successful fetch; used by URL builders.
export let chainInfo = {
  explorer: "https://robinhoodchain.blockscout.com",
  dexscreener_chain: "robinhood",
};

async function _fetch(path, init) {
  const resp = await fetch(path, init);
  const data = await resp.json();
  if (!resp.ok) throw new Error(data.detail || `Request failed (${resp.status})`);
  return data;
}

export const apiClient = {
  health: () => _fetch("/health"),

  chain: () => _fetch("/api/v1/chain"),

  analyze: (contractAddress, includeLore) =>
    _fetch("/api/v1/analyze", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ contract_address: contractAddress, include_lore: includeLore }),
    }),

  scan: (limit, includeLore) =>
    _fetch("/api/v1/scan", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ limit, include_lore: includeLore }),
    }),

  watchlist: (kind, sort) => {
    const params = new URLSearchParams({ sort: sort || "score" });
    if (kind) params.set("kind", kind);
    return _fetch(`/api/v1/watchlist?${params}`);
  },

  watchlistRefresh: () =>
    _fetch("/api/v1/watchlist/refresh", { method: "POST" }),

  wallet: (address) => _fetch(`/api/v1/wallet/${address}`),

  history: (address, limit) => {
    const params = limit ? `?limit=${limit}` : "";
    return _fetch(`/api/v1/history/${address}${params}`);
  },
};

export async function loadChainInfo() {
  try {
    const c = await apiClient.chain();
    chainInfo = { ...chainInfo, ...c };
  } catch {
    /* keep defaults */
  }
}

export function blockscoutTokenUrl(address) {
  return `${chainInfo.explorer.replace(/\/$/, "")}/token/${address}`;
}

export function dexscreenerUrl(address) {
  return `https://dexscreener.com/${encodeURIComponent(chainInfo.dexscreener_chain)}/${address}`;
}
