// SPDX-License-Identifier: MIT
pragma solidity 0.8.24;

import {BaseArbExecutorUpgradeable} from "../../src/BaseArbExecutorUpgradeable.sol";
import {MockPool, MockERC20} from "../../src/mocks.sol";
import {ERC1967Proxy} from "@openzeppelin/contracts/proxy/ERC1967/ERC1967Proxy.sol";

/// @title Echidna property fuzzing for BaseArbExecutorUpgradeable access control.
/// @notice Mirrors BaseArbExecutorEchidna, plus the invariant that only exists because this
///         variant is upgradeable: the implementation behind the proxy must be immovable by
///         an unprivileged caller. Upgrade authority is a total-loss capability -- an attacker
///         who can swap the implementation can drain everything the proxy holds, so this is
///         the single most important property in this file.
contract BaseArbExecutorUpgradeableEchidna {
    BaseArbExecutorUpgradeable internal immutable executor;
    MockERC20 internal immutable token;
    MockPool internal immutable pool;
    address internal immutable originalImplementation;
    /// A *valid* alternative implementation the attacker can actually pass the ERC-1967
    /// proxiableUUID check with. Without this, upgradeToAndCall always reverts on the
    /// implementation check and the upgrade path is unreachable -- the invariant would pass
    /// vacuously rather than because access control holds.
    address internal immutable candidateImplementation;
    UpgradeAttacker public attacker;

    uint256 internal immutable initialExecutorBalance;

    address internal constant PROBE_ADAPTER = address(0xA11CE);
    address internal constant PROBE_TOKEN = address(0xB0B);

    constructor() {
        pool = new MockPool(9); // 9 bps flash premium
        BaseArbExecutorUpgradeable impl = new BaseArbExecutorUpgradeable();
        originalImplementation = address(impl);
        ERC1967Proxy proxy = new ERC1967Proxy(
            address(impl),
            abi.encodeCall(BaseArbExecutorUpgradeable.initialize, (address(pool)))
        );
        executor = BaseArbExecutorUpgradeable(address(proxy)); // owner == this contract
        candidateImplementation = address(new BaseArbExecutorUpgradeable());
        attacker = new UpgradeAttacker(address(proxy), candidateImplementation);
        token = new MockERC20();
        token.mint(address(executor), 1_000_000 ether);
        initialExecutorBalance = token.balanceOf(address(executor));
    }

    // --- Invariants (must hold under any unprivileged tx sequence) ---

    /// THE upgradeable-specific invariant: nobody but the owner may move the implementation.
    function echidna_implementation_unchanged() public view returns (bool) {
        return executor.implementation() == originalImplementation;
    }

    /// Canary. A reverted attack rolls its own counter back, so this holding means no attack
    /// call ever completed. If it ever falsifies, an unprivileged upgrade attempt did NOT
    /// revert -- which is the same event echidna_implementation_unchanged catches, seen from
    /// the attacker's side.
    function echidna_attacker_never_ran() public view returns (bool) {
        return attacker.attempts() == 0;
    }

    function echidna_owner_unchanged() public view returns (bool) {
        return executor.owner() == address(this);
    }

    function echidna_stays_paused() public view returns (bool) {
        return executor.paused();
    }

    function echidna_no_probe_adapter() public view returns (bool) {
        return !executor.adapterAllowed(PROBE_ADAPTER);
    }

    function echidna_no_probe_token() public view returns (bool) {
        return !executor.tokenAllowed(PROBE_TOKEN);
    }

    function echidna_funds_not_drained() public view returns (bool) {
        return token.balanceOf(address(executor)) >= initialExecutorBalance;
    }

    /// The proxy must stay initialized; a re-initialization would reset `owner`.
    function echidna_pool_unchanged() public view returns (bool) {
        return address(executor.POOL()) == address(pool);
    }

}

/// Calls upgradeToAndCall on the proxy with a genuinely valid implementation. Echidna drives
/// this from unprivileged senders, so msg.sender at the proxy is this contract -- never the
/// owner. Every attempt must revert; if one lands, echidna_implementation_unchanged falsifies.
contract UpgradeAttacker {
    address public immutable target;
    address public immutable candidate;

    constructor(address target_, address candidate_) {
        target = target_;
        candidate = candidate_;
    }

    uint256 public attempts;

    function attackUpgrade() public {
        attempts++;
        BaseArbExecutorUpgradeable(target).upgradeToAndCall(candidate, "");
    }

    function attackUpgradeWithInit(address pool) public {
        attempts++;
        BaseArbExecutorUpgradeable(target).upgradeToAndCall(
            candidate, abi.encodeCall(BaseArbExecutorUpgradeable.initialize, (pool))
        );
    }
}
