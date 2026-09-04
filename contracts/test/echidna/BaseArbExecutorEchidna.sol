// SPDX-License-Identifier: MIT
pragma solidity 0.8.24;

import {BaseArbExecutor} from "../../src/BaseArbExecutor.sol";
import {MockPool, MockERC20} from "../../src/mocks.sol";

/// @title Echidna property fuzzing for BaseArbExecutor access control.
/// @notice Echidna deploys this contract (making it the executor owner) and
///         then calls every reachable function from unprivileged sender
///         addresses. The `echidna_*` properties assert the invariants an
///         attacker must never be able to break. Run with `allContracts: true`
///         so the fuzzer targets the executor and mocks directly.
contract BaseArbExecutorEchidna {
    BaseArbExecutor internal immutable executor;
    MockERC20 internal immutable token;
    MockPool internal immutable pool;

    uint256 internal immutable initialExecutorBalance;

    // Addresses an attacker might try to get allow-listed.
    address internal constant PROBE_ADAPTER = address(0xA11CE);
    address internal constant PROBE_TOKEN = address(0xB0B);

    constructor() {
        pool = new MockPool(9); // 9 bps flash premium
        executor = new BaseArbExecutor(address(pool), address(0)); // owner == this contract
        token = new MockERC20();
        token.mint(address(executor), 1_000_000 ether); // executor holds funds
        initialExecutorBalance = token.balanceOf(address(executor));
    }

    // --- Invariants (must always hold under any unprivileged tx sequence) ---

    /// Ownership cannot be taken by an unprivileged caller.
    function echidna_owner_unchanged() public view returns (bool) {
        return executor.owner() == address(this);
    }

    /// The executor is deployed paused and cannot be unpaused by an attacker.
    function echidna_stays_paused() public view returns (bool) {
        return executor.paused();
    }

    /// No attacker-chosen adapter can be allow-listed.
    function echidna_no_probe_adapter() public view returns (bool) {
        return !executor.adapterAllowed(PROBE_ADAPTER);
    }

    /// No attacker-chosen token can be allow-listed.
    function echidna_no_probe_token() public view returns (bool) {
        return !executor.tokenAllowed(PROBE_TOKEN);
    }

    /// Funds held by the executor can never be drained below the initial amount
    /// by an unprivileged caller.
    function echidna_funds_not_drained() public view returns (bool) {
        return token.balanceOf(address(executor)) >= initialExecutorBalance;
    }
}
