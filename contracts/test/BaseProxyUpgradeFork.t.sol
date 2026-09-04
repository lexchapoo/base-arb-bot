// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import {BaseArbExecutorUpgradeable} from "../src/BaseArbExecutorUpgradeable.sol";
import {IERC20, IMorpho, IMorphoFlashLoanCallback} from "../src/interfaces.sol";

interface Vm {
    function createSelectFork(string calldata rpcUrl, uint256 blockNumber) external returns (uint256);
    function envString(string calldata name) external returns (string memory);
    function envOr(string calldata name, address defaultValue) external returns (address);
    function prank(address sender) external;
    function load(address target, bytes32 slot) external view returns (bytes32);
    function skip(bool skipTest) external;
}

/// A rehearsal of the mainnet upgrade, run against the live proxy and its real state.
///
/// Every other upgrade test deploys its own proxy. That proves the layout is self-consistent,
/// which is not the property that matters here -- the deployed proxy already holds state laid
/// out by the *old* implementation, and the only question is whether the new one reads it back
/// correctly. This forks Base, impersonates the owner, and performs the exact sequence the
/// runbook prescribes.
///
/// The bug this guards against was real and shipped: MORPHO was declared after POOL, landing at
/// slot 1 where `owner` lives. Under this test `owner()` comes back as slot 2 -- the live
/// `pendingOwner`, which is zero -- and the proxy is bricked past recovery.
contract BaseProxyUpgradeForkTest {
    Vm private constant vm = Vm(address(uint160(uint256(keccak256("hevm cheat code")))));

    // The proxy was deployed at block 50,366,060; pinned comfortably after it so the fork is
    // reproducible.
    uint256 private constant PINNED_BLOCK = 50_371_060;

    // Read from the environment rather than hardcoded. This repository is public and
    // .env.example deliberately ships EXECUTOR_ADDRESS blank, so committing the live proxy and
    // its owner would tie the repo to a specific mainnet deployment for no benefit -- both are
    // already readable on chain by anyone who has them, but nothing here needs to hand them
    // over. Unset means these tests skip; they never silently pass.
    address private PROXY;
    address private OWNER;

    address private constant AAVE_POOL = 0xA238Dd80C259a72e81d7e4664a9801593F98d1c5;
    address private constant MORPHO = 0xBBBBBbbBBb9cC5e90e3b3Af64bdAF62C37EEFFCb;
    address private constant WETH = 0x4200000000000000000000000000000000000006;

    // ERC-1967 implementation slot.
    bytes32 private constant IMPL_SLOT =
        0x360894a13ba1a3210667c828492db98dca3e2076cc3735a920a3ca505d382bbc;

    function setUp() public {
        PROXY = vm.envOr("EXECUTOR_ADDRESS", address(0));
        OWNER = vm.envOr("EXECUTOR_OWNER_ADDRESS", address(0));
        if (PROXY == address(0) || OWNER == address(0)) {
            // Reported as skipped, not passed: a rehearsal that did not run must not read as
            // one that succeeded.
            vm.skip(true);
            return;
        }
        vm.createSelectFork(vm.envString("BASE_HTTP_RPC"), PINNED_BLOCK);
    }

    function _upgrade() internal returns (BaseArbExecutorUpgradeable exec) {
        BaseArbExecutorUpgradeable next = new BaseArbExecutorUpgradeable();
        vm.prank(OWNER);
        BaseArbExecutorUpgradeable(PROXY).upgradeToAndCall(address(next), "");
        return BaseArbExecutorUpgradeable(PROXY);
    }

    /// The state the live proxy actually holds, before anything is changed. If these drift the
    /// rest of this file is asserting against the wrong contract.
    function testLiveProxyPreconditions() public view {
        BaseArbExecutorUpgradeable exec = BaseArbExecutorUpgradeable(PROXY);
        require(exec.owner() == OWNER, "unexpected owner");
        require(exec.pendingOwner() == address(0), "handover already in progress");
        require(address(exec.POOL()) == AAVE_POOL, "unexpected pool");
        require(exec.paused(), "proxy is armed; it should still be dark");
        // Slot 1 is `owner` and slot 2 is `pendingOwner` == 0. That zero is precisely what a
        // shifted layout would install as the new owner.
        require(address(uint160(uint256(vm.load(PROXY, bytes32(uint256(1)))))) == OWNER, "slot 1 != owner");
        require(uint256(vm.load(PROXY, bytes32(uint256(2)))) == 0, "slot 2 != empty pendingOwner");
    }

    function testUpgradingTheLiveProxyPreservesOwnership() public {
        BaseArbExecutorUpgradeable exec = _upgrade();
        require(exec.owner() == OWNER, "owner lost: storage layout shifted");
        require(exec.pendingOwner() == address(0), "pendingOwner corrupted");
    }

    function testUpgradingTheLiveProxyPreservesConfigAndPause() public {
        BaseArbExecutorUpgradeable exec = _upgrade();
        require(address(exec.POOL()) == AAVE_POOL, "POOL lost: storage layout shifted");
        // Must come back still dark. An upgrade that silently unpaused would arm an executor
        // whose adapters have not been re-verified against the new implementation.
        require(exec.paused(), "upgrade unpaused the executor");
    }

    function testUpgradeMovesTheImplementationPointer() public {
        bytes32 before = vm.load(PROXY, IMPL_SLOT);
        BaseArbExecutorUpgradeable exec = _upgrade();
        bytes32 afterUpgrade = vm.load(PROXY, IMPL_SLOT);
        require(before != afterUpgrade, "implementation did not move");
        require(address(uint160(uint256(afterUpgrade))) == exec.implementation(), "pointer mismatch");
    }

    /// MORPHO is fresh storage, so it reads zero until set -- and initialize() is spent, so
    /// setMorpho is the only way in. Morpho routes fail closed in the gap.
    function testMorphoIsZeroAfterUpgradeAndSettableByTheOwner() public {
        BaseArbExecutorUpgradeable exec = _upgrade();
        require(address(exec.MORPHO()) == address(0), "fresh slot should read zero");

        vm.prank(OWNER);
        exec.setMorpho(MORPHO);
        require(address(exec.MORPHO()) == MORPHO, "setMorpho did not take");
        // Everything else must survive the write; setMorpho touches slot 15 and nothing else.
        require(exec.owner() == OWNER, "setMorpho disturbed owner");
        require(address(exec.POOL()) == AAVE_POOL, "setMorpho disturbed POOL");
        require(exec.paused(), "setMorpho disturbed pause state");
    }

    /// Upgrade and configure in ONE transaction, by passing setMorpho calldata as the
    /// upgradeToAndCall payload.
    ///
    /// upgradeToAndCall delegatecalls `data` against the *new* implementation immediately after
    /// switching the pointer, and delegatecall preserves msg.sender -- so setMorpho's onlyOwner
    /// sees the owner who sent the upgrade, not the proxy. That removes the window in which the
    /// proxy is upgraded but MORPHO is still zero, and it is why the payload does not have to be
    /// empty: `initialize` is spent, but an ordinary owner function is not.
    function testUpgradeAndSetMorphoInOneTransaction() public {
        BaseArbExecutorUpgradeable next = new BaseArbExecutorUpgradeable();
        vm.prank(OWNER);
        BaseArbExecutorUpgradeable(PROXY).upgradeToAndCall(
            address(next), abi.encodeCall(BaseArbExecutorUpgradeable.setMorpho, (MORPHO))
        );
        BaseArbExecutorUpgradeable exec = BaseArbExecutorUpgradeable(PROXY);

        require(address(exec.MORPHO()) == MORPHO, "atomic setMorpho did not take");
        require(exec.owner() == OWNER, "owner lost");
        require(address(exec.POOL()) == AAVE_POOL, "POOL lost");
        require(exec.paused(), "pause state lost");
        require(exec.implementation() == address(next), "implementation did not move");
    }

    /// The payload runs as the caller, so a non-owner cannot smuggle setMorpho through it --
    /// and cannot upgrade at all, which is the outer guard.
    function testNonOwnerCannotUpgradeWithAPayload() public {
        BaseArbExecutorUpgradeable next = new BaseArbExecutorUpgradeable();
        (bool ok,) = PROXY.call(
            abi.encodeWithSignature(
                "upgradeToAndCall(address,bytes)",
                address(next),
                abi.encodeCall(BaseArbExecutorUpgradeable.setMorpho, (MORPHO))
            )
        );
        require(!ok, "non-owner upgrade must revert");
    }

    /// Re-running initialize as the payload must still fail: it is spent, and an upgrade that
    /// appeared to succeed while silently skipping configuration would be worse than a revert.
    function testInitializeAsThePayloadStillReverts() public {
        BaseArbExecutorUpgradeable next = new BaseArbExecutorUpgradeable();
        vm.prank(OWNER);
        (bool ok,) = PROXY.call(
            abi.encodeWithSignature(
                "upgradeToAndCall(address,bytes)",
                address(next),
                abi.encodeCall(BaseArbExecutorUpgradeable.initialize, (AAVE_POOL, MORPHO))
            )
        );
        require(!ok, "initialize must stay spent even as an upgrade payload");
    }

    function testSetMorphoIsOwnerOnlyOnTheLiveProxy() public {
        BaseArbExecutorUpgradeable exec = _upgrade();
        (bool ok,) = PROXY.call(abi.encodeCall(BaseArbExecutorUpgradeable.setMorpho, (MORPHO)));
        require(!ok, "non-owner setMorpho must revert");
        require(address(exec.MORPHO()) == address(0), "MORPHO changed anyway");
    }

    /// The configured Morpho must be able to lend the asset the router borrows, at the block
    /// the upgrade would happen. Checked through the real singleton, not the executor, so a
    /// failure here means "Morpho is thin" rather than "the executor is wrong".
    function testConfiguredMorphoCanActuallyLendWeth() public {
        BaseArbExecutorUpgradeable exec = _upgrade();
        vm.prank(OWNER);
        exec.setMorpho(MORPHO);

        ForkBorrower borrower = new ForkBorrower(address(exec.MORPHO()), WETH);
        require(IERC20(WETH).balanceOf(address(borrower)) == 0, "borrower should start empty");
        borrower.borrow(100 ether);
        require(IERC20(WETH).balanceOf(address(borrower)) == 0, "flash repayment residue");
    }
}

/// Borrows from whatever address the upgraded proxy reports as MORPHO, so the test exercises
/// the value that was actually written rather than the constant it was written from.
contract ForkBorrower is IMorphoFlashLoanCallback {
    IMorpho private immutable morpho;
    address private immutable token;

    constructor(address morphoAddress, address tokenAddress) {
        morpho = IMorpho(morphoAddress);
        token = tokenAddress;
    }

    function borrow(uint256 amount) external {
        morpho.flashLoan(token, amount, "");
    }

    function onMorphoFlashLoan(uint256 assets, bytes calldata) external {
        require(msg.sender == address(morpho), "unexpected morpho");
        IERC20(token).approve(address(morpho), assets);
    }
}
