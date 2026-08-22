// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import "../interfaces.sol";

/// Aerodrome Slipstream is a concentrated-liquidity fork of Uniswap V3 whose pool key is
/// `tickSpacing` (int24) rather than `fee` (uint24). The swap router is otherwise
/// SwapRouter-shaped, so the calldata differs only in that one field.
interface ISlipstreamRouter {
    struct ExactInputSingleParams {
        address tokenIn;
        address tokenOut;
        int24 tickSpacing;
        address recipient;
        uint256 deadline;
        uint256 amountIn;
        uint256 amountOutMinimum;
        uint160 sqrtPriceLimitX96;
    }
    function exactInputSingle(ExactInputSingleParams calldata params) external payable returns (uint256 amountOut);
}

contract SlipstreamAdapter is IExchangeAdapter {
    error Unauthorized();
    error SwapFailed();

    address public immutable executor;
    address public immutable router;

    constructor(address _executor, address _router) {
        if (_executor == address(0) || _router == address(0)) revert Unauthorized();
        executor = _executor;
        router = _router;
    }

    function swap(
        address tokenIn,
        address tokenOut,
        uint256 amountIn,
        uint256 minOut,
        address recipient,
        bytes calldata data
    ) external returns (uint256 amountOut) {
        if (msg.sender != executor) revert Unauthorized();
        (int24 tickSpacing, uint160 sqrtPriceLimitX96) = abi.decode(data, (int24, uint160));
        _safeTransferFrom(tokenIn, msg.sender, address(this), amountIn);
        _forceApprove(tokenIn, router, amountIn);

        // Measure the recipient's real delta rather than trusting the router return value,
        // so fee-on-transfer or rebasing tokens cannot overstate the output.
        uint256 beforeOut = IERC20(tokenOut).balanceOf(recipient);
        ISlipstreamRouter(router).exactInputSingle(
            ISlipstreamRouter.ExactInputSingleParams({
                tokenIn: tokenIn,
                tokenOut: tokenOut,
                tickSpacing: tickSpacing,
                recipient: recipient,
                deadline: block.timestamp,
                amountIn: amountIn,
                amountOutMinimum: minOut,
                sqrtPriceLimitX96: sqrtPriceLimitX96
            })
        );
        uint256 afterOut = IERC20(tokenOut).balanceOf(recipient);
        amountOut = afterOut - beforeOut;
        if (amountOut < minOut) revert SwapFailed();
        _forceApprove(tokenIn, router, 0);
    }

    function _safeTransferFrom(address token, address from, address to, uint256 amount) internal {
        (bool ok, bytes memory ret) = token.call(abi.encodeCall(IERC20.transferFrom, (from, to, amount)));
        if (!ok || (ret.length != 0 && !abi.decode(ret, (bool)))) revert SwapFailed();
    }

    function _forceApprove(address token, address spender, uint256 amount) internal {
        (bool ok, bytes memory ret) = token.call(abi.encodeCall(IERC20.approve, (spender, 0)));
        if (!ok || (ret.length != 0 && !abi.decode(ret, (bool)))) revert SwapFailed();
        if (amount != 0) {
            (ok, ret) = token.call(abi.encodeCall(IERC20.approve, (spender, amount)));
            if (!ok || (ret.length != 0 && !abi.decode(ret, (bool)))) revert SwapFailed();
        }
    }
}
