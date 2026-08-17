// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import "./interfaces.sol";

contract MockERC20 is IERC20 {
    mapping(address => uint256) public balanceOf;
    mapping(address => mapping(address => uint256)) public allowance;

    function mint(address to, uint256 amount) external {
        balanceOf[to] += amount;
    }

    function approve(address spender, uint256 amount) external returns (bool) {
        allowance[msg.sender][spender] = amount;
        return true;
    }

    function transfer(address to, uint256 amount) external returns (bool) {
        require(balanceOf[msg.sender] >= amount, "balance");
        balanceOf[msg.sender] -= amount;
        balanceOf[to] += amount;
        return true;
    }

    function transferFrom(address from, address to, uint256 amount) external returns (bool) {
        uint256 allowed = allowance[from][msg.sender];
        require(allowed >= amount, "allowance");
        require(balanceOf[from] >= amount, "balance");
        if (allowed != type(uint256).max) allowance[from][msg.sender] = allowed - amount;
        balanceOf[from] -= amount;
        balanceOf[to] += amount;
        return true;
    }
}

contract MockPool is IAavePool {
    uint128 public premiumBps;

    constructor(uint128 p) {
        premiumBps = p;
    }

    function FLASHLOAN_PREMIUM_TOTAL() external view returns (uint128) {
        return premiumBps;
    }

    function flashLoanSimple(address receiver, address asset, uint256 amount, bytes calldata params, uint16) external {
        uint256 premium = (amount * premiumBps + 5000) / 10000;
        MockERC20(asset).mint(receiver, amount);
        require(
            IFlashLoanSimpleReceiver(receiver).executeOperation(asset, amount, premium, receiver, params), "callback"
        );
        require(MockERC20(asset).transferFrom(receiver, address(this), amount + premium), "repay");
    }
}

contract MockExchangeAdapter is IExchangeAdapter {
    uint256 public numerator;
    uint256 public denominator;

    constructor(uint256 n, uint256 d) {
        numerator = n;
        denominator = d;
    }

    function swap(
        address tokenIn,
        address tokenOut,
        uint256 amountIn,
        uint256 minOut,
        address recipient,
        bytes calldata
    ) external returns (uint256 amountOut) {
        require(MockERC20(tokenIn).transferFrom(msg.sender, address(this), amountIn), "pull");
        amountOut = amountIn * numerator / denominator;
        require(amountOut >= minOut, "minOut");
        MockERC20(tokenOut).mint(recipient, amountOut);
    }
}
