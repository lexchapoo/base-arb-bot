// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

interface IERC20 {
    function balanceOf(address) external view returns (uint256);
    function approve(address,uint256) external returns (bool);
    function transfer(address,uint256) external returns (bool);
    function transferFrom(address,address,uint256) external returns (bool);
}

interface IAavePool {
    function flashLoanSimple(address receiver,address asset,uint256 amount,bytes calldata params,uint16 referralCode) external;
    function FLASHLOAN_PREMIUM_TOTAL() external view returns (uint128);
}

/// Morpho Blue's flash loan surface (Base: 0xBBBBBbbBBb9cC5e90e3b3Af64bdAF62C37EEFFCb).
///
/// Two differences from Aave that matter at the call site:
///   * No premium. Morpho pulls back exactly `assets`, so the amount owed equals the amount
///     borrowed. Morpho Blue's core is immutable, so unlike a governance-settable fee this
///     cannot be switched on later.
///   * No receiver argument. The callback always lands on the caller of flashLoan(), which is
///     what makes `msg.sender == MORPHO` sufficient authentication in the receiver -- there is
///     no `initiator` to check because no other account can direct the callback here.
interface IMorpho {
    function flashLoan(address token, uint256 assets, bytes calldata data) external;
}

/// Note the callback carries no token address: the borrower is expected to already know what
/// it asked for. Repayment is pull-based (safeTransferFrom), same as Aave, so the receiver
/// approves rather than transfers.
interface IMorphoFlashLoanCallback {
    function onMorphoFlashLoan(uint256 assets, bytes calldata data) external;
}

interface IFlashLoanSimpleReceiver {
    function executeOperation(address asset,uint256 amount,uint256 premium,address initiator,bytes calldata params) external returns(bool);
}

interface IExchangeAdapter {
    function swap(
        address tokenIn,
        address tokenOut,
        uint256 amountIn,
        uint256 minOut,
        address recipient,
        bytes calldata data
    ) external returns (uint256 amountOut);
}
