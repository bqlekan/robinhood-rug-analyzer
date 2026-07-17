// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

// M10-B honeypot prober (Uniswap v3 / SwapRouter02).
//
// Injected at a synthetic address via an `eth_call` `code` state override and funded
// with native balance. It performs a full buy->sell round-trip atomically in ONE call
// (two separate eth_calls cannot share state), then returns (bought, soldBack). A sell
// that reverts is caught and reported as soldBack=0 — the honeypot signature.
//
// No keys, no real funds, no transaction: this runtime bytecode only ever executes
// inside a read-only eth_call with ephemeral state overrides.

interface IWETH {
    function deposit() external payable;
    function approve(address spender, uint256 amount) external returns (bool);
}

interface IERC20 {
    function approve(address spender, uint256 amount) external returns (bool);
    function balanceOf(address account) external view returns (uint256);
}

// SwapRouter02: exactInputSingle has NO deadline field (unlike the original SwapRouter).
interface ISwapRouter02 {
    struct ExactInputSingleParams {
        address tokenIn;
        address tokenOut;
        uint24 fee;
        address recipient;
        uint256 amountIn;
        uint256 amountOutMinimum;
        uint160 sqrtPriceLimitX96;
    }

    function exactInputSingle(ExactInputSingleParams calldata params)
        external
        payable
        returns (uint256 amountOut);
}

contract HoneypotProber {
    /// Buy `buyWei` of native into `token` via WETH, then sell all of it back.
    /// Returns (bought token amount, native/WETH received on sell). soldBack=0 if the
    /// sell reverts (unsellable). Reverts only if the buy leg cannot execute at all —
    /// the caller treats a whole-call revert as "could not simulate" (unknown).
    function probe(address router, address weth, address token, uint256 buyWei)
        external
        returns (uint256 bought, uint256 soldBack)
    {
        // 1. Wrap native -> WETH and approve the router.
        IWETH(weth).deposit{value: buyWei}();
        IWETH(weth).approve(router, type(uint256).max);

        // 2. Buy: WETH -> token, trying fee tiers until one succeeds.
        bought = _swap(router, weth, token, buyWei);
        require(bought > 0, "buy failed"); // no pool / unbuyable -> unknown upstream

        // 3. Approve token and sell it all back: token -> WETH.
        //    A honeypot reverts here; catch it and report soldBack=0.
        IERC20(token).approve(router, type(uint256).max);
        try this.sell(router, token, weth, bought) returns (uint256 out) {
            soldBack = out;
        } catch {
            soldBack = 0;
        }
    }

    /// External so it can be wrapped in try/catch above. Only callable by this contract.
    function sell(address router, address token, address weth, uint256 amountIn)
        external
        returns (uint256)
    {
        require(msg.sender == address(this), "internal");
        return _swap(router, token, weth, amountIn);
    }

    /// exactInputSingle across fee tiers; returns the first non-zero output, else 0.
    function _swap(address router, address tokenIn, address tokenOut, uint256 amountIn)
        private
        returns (uint256)
    {
        // Fee tiers live in a function-local memory literal, NOT a storage array with an
        // inline initializer: injecting runtime bytecode via a `code` state override does
        // not run the constructor, so constructor-set storage would read as all-zero.
        uint24[4] memory fees = [uint24(500), 3000, 10000, 100];
        for (uint256 i = 0; i < fees.length; i++) {
            ISwapRouter02.ExactInputSingleParams memory p = ISwapRouter02.ExactInputSingleParams({
                tokenIn: tokenIn,
                tokenOut: tokenOut,
                fee: fees[i],
                recipient: address(this),
                amountIn: amountIn,
                amountOutMinimum: 0,
                sqrtPriceLimitX96: 0
            });
            try ISwapRouter02(router).exactInputSingle(p) returns (uint256 out) {
                if (out > 0) {
                    return out;
                }
            } catch {
                // try the next fee tier
            }
        }
        return 0;
    }
}
