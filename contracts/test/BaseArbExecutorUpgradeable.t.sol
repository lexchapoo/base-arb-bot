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

contract BaseArbExecutorUpgradeableTest {
    function _deploy() internal returns (BaseArbExecutorUpgradeable exec, MockPool pool) {
        pool = new MockPool(10);
        BaseArbExecutorUpgradeable impl = new BaseArbExecutorUpgradeable();
        ERC1967Proxy proxy = new ERC1967Proxy(
            address(impl), abi.encodeCall(BaseArbExecutorUpgradeable.initialize, (address(pool)))
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
            abi.encodeCall(BaseArbExecutorUpgradeable.initialize, (address(pool)))
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
            abi.encodeCall(BaseArbExecutorUpgradeable.initialize, (address(pool)))
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
}

contract Outsider {
    function tryUpgrade(address exec, address impl) external {
        BaseArbExecutorUpgradeable(exec).upgradeToAndCall(impl, "");
    }
}
