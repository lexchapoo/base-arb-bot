// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import {BaseArbExecutor} from "../src/BaseArbExecutor.sol";
import {MockPool} from "../src/mocks.sol";

contract UnprivilegedPauseHandler {
    BaseArbExecutor internal immutable executor;

    constructor(BaseArbExecutor target) {
        executor = target;
    }

    function attemptPauseChange(bool value) external {
        (bool ok,) = address(executor).call(abi.encodeCall(executor.setPaused, (value)));
        require(!ok, "unprivileged pause change accepted");
    }

    function attemptOwnershipTransfer(address next) external {
        (bool ok,) = address(executor).call(abi.encodeCall(executor.transferOwnership, (next)));
        require(!ok, "unprivileged ownership transfer accepted");
    }
}

contract BaseArbExecutorInvariantTest {
    BaseArbExecutor private executor;
    UnprivilegedPauseHandler private handler;
    address[] private targets;

    function setUp() public {
        executor = new BaseArbExecutor(address(new MockPool(10)), address(0));
        handler = new UnprivilegedPauseHandler(executor);
        targets.push(address(handler));
    }

    function targetContracts() public view returns (address[] memory) {
        return targets;
    }

    function invariantDeploymentStaysPausedUnderUnprivilegedCalls() public view {
        require(executor.paused(), "executor unexpectedly activated");
        require(executor.owner() == address(this), "owner unexpectedly changed");
        require(executor.pendingOwner() == address(0), "pending owner unexpectedly changed");
    }
}
