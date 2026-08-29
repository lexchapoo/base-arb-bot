// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import "../src/BaseArbExecutorUpgradeable.sol";
import "../src/mocks.sol";
import {ERC1967Proxy} from "@openzeppelin/contracts/proxy/ERC1967/ERC1967Proxy.sol";

/// A second implementation, used only to prove state survives an upgrade.
contract BaseArbExecutorV2 is BaseArbExecutorUpgradeable {
    function version() external pure returns (uint256) {
        return 2;
    }
}

/// The storage layout of the implementation currently behind the live proxy (commit 408c56f),
/// reproduced exactly and in order.
///
/// The existing upgrade test goes from the current implementation to a subclass of it, so both
/// sides share a layout by construction and it cannot detect a layout change at all. Only an
/// upgrade *from the deployed shape* can, which is what this exists for. Declaration order here
/// is load-bearing -- do not tidy it.
contract LegacyBaseArbExecutor is Initializable, UUPSUpgradeable {
    IAavePool public POOL;
    address public owner;
    address public pendingOwner;
    mapping(address => bool) public adapterAllowed;
    mapping(address => bool) public tokenAllowed;
    mapping(bytes32 => bool) public commitmentUsed;
    bool public paused;

    bytes32 private activeRouteHash;
    bytes32 private activeBatchHash;
    uint256 private activePreLoanBalance;
    uint256 private activePremium;
    bytes32 private activeSelectedCandidateId;
    uint8 private activeSelectedCandidateIndex;
    uint256 private activeSelectedMinProfit;
    bool private callbackEntered;

    constructor() {
        _disableInitializers();
    }

    function initialize(address pool) external initializer {
        POOL = IAavePool(pool);
        owner = msg.sender;
        paused = true;
    }

    function setAdapter(address adapter, bool allowed) external {
        adapterAllowed[adapter] = allowed;
    }

    function setToken(address token, bool allowed) external {
        tokenAllowed[token] = allowed;
    }

    function _authorizeUpgrade(address) internal override {}
}

contract BaseArbExecutorUpgradeableTest {
    function _deploy() internal returns (BaseArbExecutorUpgradeable exec, MockPool pool) {
        pool = new MockPool(10);
        BaseArbExecutorUpgradeable impl = new BaseArbExecutorUpgradeable();
        ERC1967Proxy proxy = new ERC1967Proxy(
            address(impl), abi.encodeCall(BaseArbExecutorUpgradeable.initialize, (address(pool), address(0)))
        );
        exec = BaseArbExecutorUpgradeable(address(proxy));
    }

    /// The proxy must come up owned, pointed at the pool, and PAUSED -- same "deploy dark"
    /// posture as the immutable executor. An upgradeable contract that armed itself on deploy
    /// would be live before its adapters and tokens were allowlisted.
    function testInitializeSetsOwnerPoolAndDeploysPaused() public {
        (BaseArbExecutorUpgradeable exec, MockPool pool) = _deploy();
        require(exec.owner() == address(this), "owner not set");
        require(address(exec.POOL()) == address(pool), "pool not set");
        require(exec.paused(), "must deploy paused");
    }

    /// Re-initialising a live proxy would reset `owner` to the caller and hand over the
    /// contract, so it must be impossible after the first call.
    function testInitializeCannotBeCalledTwice() public {
        (BaseArbExecutorUpgradeable exec, MockPool pool) = _deploy();
        (bool ok,) = address(exec).call(
            abi.encodeCall(BaseArbExecutorUpgradeable.initialize, (address(pool), address(0)))
        );
        require(!ok, "re-initialization must revert");
    }

    /// An attacker who initialises a bare implementation owns it, and under UUPS can then call
    /// upgradeToAndCall on the implementation itself. `_disableInitializers()` in the
    /// constructor is what forecloses that.
    function testBareImplementationCannotBeInitialized() public {
        MockPool pool = new MockPool(10);
        BaseArbExecutorUpgradeable impl = new BaseArbExecutorUpgradeable();
        (bool ok,) = address(impl).call(
            abi.encodeCall(BaseArbExecutorUpgradeable.initialize, (address(pool), address(0)))
        );
        require(!ok, "implementation must not be initializable");
        require(impl.owner() == address(0), "implementation must be unowned");
    }

    /// Upgrade authority is a total-loss key: a non-owner who could swap the implementation
    /// could sweep everything the proxy holds.
    function testNonOwnerCannotUpgrade() public {
        (BaseArbExecutorUpgradeable exec,) = _deploy();
        BaseArbExecutorV2 v2 = new BaseArbExecutorV2();
        Outsider outsider = new Outsider();
        (bool ok,) = address(outsider).call(
            abi.encodeCall(Outsider.tryUpgrade, (address(exec), address(v2)))
        );
        require(!ok, "non-owner upgrade must revert");
    }

    /// The allowlist is the last boundary between a routing bug and real funds. If it did not
    /// survive an upgrade the executor would silently come back with every token disallowed --
    /// or, far worse, a stale layout that read someone else's slot as `tokenAllowed`.
    function testAllowlistAndOwnershipSurviveUpgrade() public {
        (BaseArbExecutorUpgradeable exec, MockPool pool) = _deploy();
        MockERC20 tokenA = new MockERC20();
        MockExchangeAdapter adapter = new MockExchangeAdapter(2, 1);
        exec.setToken(address(tokenA), true);
        exec.setAdapter(address(adapter), true);

        BaseArbExecutorV2 v2 = new BaseArbExecutorV2();
        exec.upgradeToAndCall(address(v2), "");

        require(BaseArbExecutorV2(address(exec)).version() == 2, "upgrade did not take");
        require(exec.tokenAllowed(address(tokenA)), "token allowlist lost");
        require(exec.adapterAllowed(address(adapter)), "adapter allowlist lost");
        require(exec.owner() == address(this), "owner lost");
        require(address(exec.POOL()) == address(pool), "POOL lost across upgrade");
        require(exec.paused(), "pause state lost");
    }

    // --- upgrading the live proxy to add the second provider ---------------------------

    /// Simulates the actual mainnet upgrade: deploy the layout that is live today, populate it,
    /// then move to the current implementation.
    ///
    /// This is the test that catches a shifted slot. MORPHO was first declared directly after
    /// POOL, which put it where `owner` lives; every unit test still passed, because they all
    /// deploy a fresh contract whose layout is self-consistent. Under this test `owner` comes
    /// back as the old `pendingOwner` -- zero -- and the proxy is bricked: no unpause, no sweep,
    /// no second upgrade.
    function testUpgradeFromTheDeployedLayoutPreservesEveryField() public {
        MockPool pool = new MockPool(10);
        MockERC20 tokenA = new MockERC20();
        MockExchangeAdapter adapter = new MockExchangeAdapter(2, 1);

        LegacyBaseArbExecutor legacyImpl = new LegacyBaseArbExecutor();
        ERC1967Proxy proxy = new ERC1967Proxy(
            address(legacyImpl), abi.encodeCall(LegacyBaseArbExecutor.initialize, (address(pool)))
        );
        LegacyBaseArbExecutor legacy = LegacyBaseArbExecutor(address(proxy));
        legacy.setToken(address(tokenA), true);
        legacy.setAdapter(address(adapter), true);
        require(legacy.owner() == address(this), "precondition: owner");

        BaseArbExecutorUpgradeable next = new BaseArbExecutorUpgradeable();
        legacy.upgradeToAndCall(address(next), "");
        BaseArbExecutorUpgradeable exec = BaseArbExecutorUpgradeable(address(proxy));

        require(exec.owner() == address(this), "owner moved: layout shifted");
        require(exec.pendingOwner() == address(0), "pendingOwner moved: layout shifted");
        require(address(exec.POOL()) == address(pool), "POOL moved: layout shifted");
        require(exec.paused(), "paused moved: layout shifted");
        require(exec.tokenAllowed(address(tokenA)), "token allowlist moved: layout shifted");
        require(exec.adapterAllowed(address(adapter)), "adapter allowlist moved: layout shifted");
    }

    /// The new slot is fresh storage, so it reads zero on an upgraded proxy -- and initialize()
    /// is already spent, so it cannot be back-filled by re-initialising. setMorpho is the only
    /// way in, and until it is called Morpho routes fail closed rather than falling back to Aave.
    function testUpgradedProxyHasNoMorphoUntilItIsSet() public {
        MockPool pool = new MockPool(10);
        LegacyBaseArbExecutor legacyImpl = new LegacyBaseArbExecutor();
        ERC1967Proxy proxy = new ERC1967Proxy(
            address(legacyImpl), abi.encodeCall(LegacyBaseArbExecutor.initialize, (address(pool)))
        );
        LegacyBaseArbExecutor(address(proxy)).upgradeToAndCall(
            address(new BaseArbExecutorUpgradeable()), ""
        );
        BaseArbExecutorUpgradeable exec = BaseArbExecutorUpgradeable(address(proxy));

        require(address(exec.MORPHO()) == address(0), "fresh slot must read zero");
        (bool ok,) = address(exec).call(
            abi.encodeCall(BaseArbExecutorUpgradeable.initialize, (address(pool), address(0xdead)))
        );
        require(!ok, "initialize must stay spent");

        MockMorpho morpho = new MockMorpho();
        exec.setMorpho(address(morpho));
        require(address(exec.MORPHO()) == address(morpho), "setMorpho did not take");
    }

    function testOnlyTheOwnerCanSetMorpho() public {
        (BaseArbExecutorUpgradeable exec,) = _deploy();
        Outsider outsider = new Outsider();
        (bool ok,) = address(outsider).call(
            abi.encodeCall(Outsider.trySetMorpho, (address(exec), address(0xbeef)))
        );
        require(!ok, "non-owner setMorpho must revert");
        require(address(exec.MORPHO()) == address(0), "MORPHO changed anyway");
    }

    /// Zero is a permitted value: it is how an operator turns Morpho off without an upgrade.
    function testMorphoCanBeUnset() public {
        (BaseArbExecutorUpgradeable exec,) = _deploy();
        MockMorpho morpho = new MockMorpho();
        exec.setMorpho(address(morpho));
        exec.setMorpho(address(0));
        require(address(exec.MORPHO()) == address(0), "must be able to disable morpho");
    }
}

contract Outsider {
    function tryUpgrade(address exec, address impl) external {
        BaseArbExecutorUpgradeable(exec).upgradeToAndCall(impl, "");
    }

    function trySetMorpho(address exec, address morpho) external {
        BaseArbExecutorUpgradeable(exec).setMorpho(morpho);
    }
}
