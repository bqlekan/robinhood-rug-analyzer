const form = document.querySelector("#analyze-form");
const result = document.querySelector("#result");

function formatCurrency(value) {
  if (value === null || value === undefined) return "N/A";
  return new Intl.NumberFormat("en-US", {
    style: "currency",
    currency: "USD",
    maximumFractionDigits: 2,
  }).format(value);
}

function renderSignals(signals) {
  if (!signals.length) {
    return "<li>No major warning signals were detected from the available public data.</li>";
  }

  return signals
    .map(
      (signal) => `
        <li class="signal signal-${signal.severity}">
          <strong>${signal.name}</strong>
          <span>${signal.severity.toUpperCase()} - +${signal.points}</span>
          <p>${signal.description}</p>
        </li>
      `,
    )
    .join("");
}

function renderAnalysis(data) {
  const market = data.market_data;
  const analysis = data.analysis;
  const honeypot = data.honeypot_data;

  result.innerHTML = `
    <section class="analysis-summary risk-${analysis.risk_level}">
      <div>
        <span class="label">Risk Score</span>
        <strong>${analysis.risk_score}/100</strong>
      </div>
      <div>
        <span class="label">Risk Level</span>
        <strong>${analysis.risk_level.toUpperCase()}</strong>
      </div>
      <div>
        <span class="label">Blockchain</span>
        <strong>${data.detected_blockchain}</strong>
      </div>
    </section>

    <section class="market-grid">
      <article>
        <span class="label">Token</span>
        <strong>${market?.base_token_name || "Unknown"} (${market?.base_token_symbol || "N/A"})</strong>
      </article>
      <article>
        <span class="label">DEX</span>
        <strong>${market?.dex_id || "N/A"}</strong>
      </article>
      <article>
        <span class="label">Price</span>
        <strong>${market?.price_usd ? `$${market.price_usd}` : "N/A"}</strong>
      </article>
      <article>
        <span class="label">Liquidity</span>
        <strong>${formatCurrency(market?.liquidity?.usd)}</strong>
      </article>
      <article>
        <span class="label">24h Volume</span>
        <strong>${formatCurrency(market?.volume?.h24)}</strong>
      </article>
      <article>
        <span class="label">Sell Tax</span>
        <strong>${honeypot?.sell_tax ?? "N/A"}%</strong>
      </article>
    </section>

    <section>
      <h2>Risk Signals</h2>
      <ul class="signals">${renderSignals(analysis.signals)}</ul>
    </section>

    <section>
      <h2>Limitations</h2>
      <ul class="limitations">
        ${analysis.limitations.map((item) => `<li>${item}</li>`).join("")}
      </ul>
    </section>

    ${market?.url ? `<a class="source-link" href="${market.url}" target="_blank" rel="noopener">View pair on DexScreener</a>` : ""}
  `;
}

form.addEventListener("submit", async (event) => {
  event.preventDefault();
  const contractAddress = new FormData(form).get("contractAddress");
  result.textContent = "Running rug-risk analysis...";

  try {
    const response = await fetch("/api/v1/analyze", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ contract_address: contractAddress }),
    });

    const data = await response.json();
    if (!response.ok) {
      throw new Error(data.detail || "Analysis request failed");
    }
    renderAnalysis(data);
  } catch (error) {
    result.textContent = `Request failed: ${error.message}`;
  }
});
