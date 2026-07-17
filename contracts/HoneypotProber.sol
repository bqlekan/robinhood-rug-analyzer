// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

// M10-B honeypot prober (Uniswap v3 / SwapRouter02, path-based).
//
// Injected at a synthetic address via an `eth_call` `code` state override and funded
// with native balance. It performs a full buy->sell round-trip atomically in ONE call
// (two separate eth_calls cannot share state), then returns (bought, soldBack). A sell
// that reverts is caught and reported as soldBack=0 — the honeypot signature.
//
// Routing is fully externalized: the caller passes opaque Uniswap v3 `path` bytes for
// the buy and sell legs (built by the off-chain route_discovery service). The prober
// executes exactInput(path) and never decides fee tiers or quote assets itself, so a
// single pinned bytecode serves direct (WETH->token) and multi-hop (WETH->USDG->token)
// routes and any future quote asset — no recompile.
//
// No keys, no real funds, no transaction: this runtime bytecode only ever executes
// inside a read-only eth_call with ephemeral state overrides.

interface IWETH {
    function deposit() external payable;
    function approve(address spender, uint256 amount) external returns (bool);
}

interface IERC20 {
    function approve(address spender, uint256 amount) external returns (bool);
}

// SwapRouter02 path-based swap: exactInput has NO deadline field (unlike the original SwapRouter).
interface ISwapRouter02 {
    struct ExactInputParams {
        bytes path;
        address recipient;
        uint256 amountIn;
        uint256 amountOutMinimum;
    }

    function exactInput(ExactInputParams calldata params) external payable returns (uint256 amountOut);
}

contract HoneypotProber {
    /// Buy `buyWei` of native into `token` along `buyPath`, then sell it all back along
    /// `sellPath`. Returns (bought token amount, native/WETH received on sell). soldBack=0
    /// if the sell reverts (unsellable). Reverts only if the buy leg cannot execute at all —
    /// the caller treats a whole-call revert as "could not simulate" (unknown).
    ///
    /// `buyPath`  = abi.encodePacked(weth, fee, [mid, fee,] token)
    /// `sellPath` = the reverse (token ... weth)
    function probe(
        address router,
        address weth,
        address token,
        uint256 buyWei,
        bytes calldata buyPath,
        bytes calldata sellPath
    ) external returns (uint256 bought, uint256 soldBack) {
        // 1. Wrap native -> WETH and approve the router.
        IWETH(weth).deposit{value: buyWei}();
        IWETH(weth).approve(router, type(uint256).max);

        // 2. Buy along the caller-supplied path.
        bought = ISwapRouter02(router).exactInput(
            ISwapRouter02.ExactInputParams({path: buyPath, recipient: address(this), amountIn: buyWei, amountOutMinimum: 0})
        );
        require(bought > 0, "buy failed"); // no route / unbuyable -> unknown upstream

        // 3. Approve the token and sell it all back. A honeypot reverts here; catch it
        //    and report soldBack=0.
        IERC20(token).approve(router, type(uint256).max);
        try this.sell(router, sellPath, bought) returns (uint256 out) {
            soldBack = out;
        } catch {
            soldBack = 0;
        }
    }

    /// External so it can be wrapped in try/catch above. Only callable by this contract.
    function sell(address router, bytes calldata sellPath, uint256 amountIn) external returns (uint256) {
        require(msg.sender == address(this), "internal");
        return ISwapRouter02(router).exactInput(
            ISwapRouter02.ExactInputParams({path: sellPath, recipient: address(this), amountIn: amountIn, amountOutMinimum: 0})
        );
    }
}
