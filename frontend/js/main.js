/** App entry — loads chain info, then wires the router + every page module. */
import { loadChainInfo } from "./api.js";
import "./router.js";
import "./pages/dashboard.js";
import "./pages/scan.js";
import "./pages/token.js";
import "./pages/wallets.js";
import "./pages/kol.js";

// Fetch chain info once so token-action URL builders resolve correctly.
loadChainInfo();
