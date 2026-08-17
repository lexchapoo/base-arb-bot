// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import "../interfaces.sol";

interface IAerodromeRouter {
    struct Route {
        address from;
        address to;
        bool stable;
        address factory;
    }
    function swapExactTokensForTokens(
        uint256 amountIn,
        uint256 amountOutMin,
        Route[] calldata routes,
        address to,
        uint256 deadline
    ) external returns (uint256[] memory amounts);
}

contract AerodromeAdapter is IExchangeAdapter {
    error Unauthorized();
    error SwapFailed();

    address public immutable executor;
    IAerodromeRouter public immutable router;
    address public immutable factory;

    constructor(address _executor, address _router, address _factory) {
        if (_executor == address(0) || _router == address(0) || _factory == address(0)) revert Unauthorized();
        executor = _executor;
        router = IAerodromeRouter(_router);
        factory = _factory;
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
        bool stable = abi.decode(data, (bool));
        _safeTransferFrom(tokenIn, msg.sender, address(this), amountIn);
        _forceApprove(tokenIn, address(router), amountIn);

        IAerodromeRouter.Route[] memory routes = new IAerodromeRouter.Route[](1);
        routes[0] = IAerodromeRouter.Route({from: tokenIn, to: tokenOut, stable: stable, factory: factory});
        uint256 beforeOut = IERC20(tokenOut).balanceOf(recipient);
        router.swapExactTokensForTokens(amountIn, minOut, routes, recipient, block.timestamp);
        uint256 afterOut = IERC20(tokenOut).balanceOf(recipient);
        amountOut = afterOut - beforeOut;
        if (amountOut < minOut) revert SwapFailed();
        _forceApprove(tokenIn, address(router), 0);
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
