// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import "../interfaces.sol";

interface IUniswapV3RouterLegacy {
    struct ExactInputSingleParams {
        address tokenIn; address tokenOut; uint24 fee; address recipient; uint256 deadline;
        uint256 amountIn; uint256 amountOutMinimum; uint160 sqrtPriceLimitX96;
    }
    function exactInputSingle(ExactInputSingleParams calldata params) external payable returns (uint256 amountOut);
}

interface IUniswapV3Router02 {
    struct ExactInputSingleParams {
        address tokenIn; address tokenOut; uint24 fee; address recipient;
        uint256 amountIn; uint256 amountOutMinimum; uint160 sqrtPriceLimitX96;
    }
    function exactInputSingle(ExactInputSingleParams calldata params) external payable returns (uint256 amountOut);
}

contract UniswapV3Adapter is IExchangeAdapter {
    error Unauthorized();
    error SwapFailed();

    address public immutable executor;
    address public immutable router;
    bool public immutable router02;

    constructor(address _executor, address _router, bool _router02) {
        if (_executor == address(0) || _router == address(0)) revert Unauthorized();
        executor = _executor;
        router = _router;
        router02 = _router02;
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
        (uint24 fee, uint160 sqrtPriceLimitX96) = abi.decode(data, (uint24, uint160));
        _safeTransferFrom(tokenIn, msg.sender, address(this), amountIn);
        _forceApprove(tokenIn, router, amountIn);

        uint256 beforeOut = IERC20(tokenOut).balanceOf(recipient);
        if (router02) {
            IUniswapV3Router02(router).exactInputSingle(
                IUniswapV3Router02.ExactInputSingleParams({
                    tokenIn: tokenIn,
                    tokenOut: tokenOut,
                    fee: fee,
                    recipient: recipient,
                    amountIn: amountIn,
                    amountOutMinimum: minOut,
                    sqrtPriceLimitX96: sqrtPriceLimitX96
                })
            );
        } else {
            IUniswapV3RouterLegacy(router).exactInputSingle(
                IUniswapV3RouterLegacy.ExactInputSingleParams({
                    tokenIn: tokenIn,
                    tokenOut: tokenOut,
                    fee: fee,
                    recipient: recipient,
                    deadline: block.timestamp,
                    amountIn: amountIn,
                    amountOutMinimum: minOut,
                    sqrtPriceLimitX96: sqrtPriceLimitX96
                })
            );
        }
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
