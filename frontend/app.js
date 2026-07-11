const form = document.querySelector("#analyze-form");
const result = document.querySelector("#result");

form.addEventListener("submit", async (event) => {
  event.preventDefault();
  const contractAddress = new FormData(form).get("contractAddress");
  result.textContent = "Detecting blockchain...";

  try {
    const response = await fetch("/api/v1/analyze", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ contract_address: contractAddress }),
    });

    const data = await response.json();
    result.textContent = JSON.stringify(data, null, 2);
  } catch (error) {
    result.textContent = `Request failed: ${error.message}`;
  }
});
