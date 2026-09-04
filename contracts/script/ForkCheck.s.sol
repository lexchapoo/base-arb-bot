// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

interface IAavePoolProbe { function FLASHLOAN_PREMIUM_TOTAL() external view returns (uint128); }

contract ForkCheck {
    function run(address pool) external view returns (uint128) {
        require(pool != address(0) && pool.code.length > 0, "Aave pool missing");
        return IAavePoolProbe(pool).FLASHLOAN_PREMIUM_TOTAL();
    }
}
