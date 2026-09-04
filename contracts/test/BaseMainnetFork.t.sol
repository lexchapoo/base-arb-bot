// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import {BaseArbExecutor} from "../src/BaseArbExecutor.sol";
import {AerodromeAdapter} from "../src/adapters/AerodromeAdapter.sol";
import {UniswapV3Adapter} from "../src/adapters/UniswapV3Adapter.sol";
import {IERC20, IAavePool, IFlashLoanSimpleReceiver, IMorpho, IMorphoFlashLoanCallback} from "../src/interfaces.sol";

interface Vm {
    function createSelectFork(string calldata rpcUrl, uint256 blockNumber) external returns (uint256 forkId);
    function deal(address account, uint256 newBalance) external;
    function envString(string calldata name) external returns (string memory value);
}

interface IWETH is IERC20 {
    function deposit() external payable;
}

contract AaveForkReceiver is IFlashLoanSimpleReceiver {
    IAavePool internal immutable pool;

    constructor(address poolAddress) {
        pool = IAavePool(poolAddress);
    }

    function borrow(address asset, uint256 amount) external {
        pool.flashLoanSimple(address(this), asset, amount, "", 0);
    }

    function executeOperation(address asset, uint256 amount, uint256 premium, address initiator, bytes calldata)
        external
        returns (bool)
    {
        require(msg.sender == address(pool), "unexpected pool");
        require(initiator == address(this), "unexpected initiator");
        IERC20(asset).approve(address(pool), amount + premium);
        return true;
    }
}

/// Deliberately mirrors AaveForkReceiver so the two are comparable line for line: the only
/// substantive differences are that nothing is transferred in to cover a premium, and the
/// callback takes no initiator (Morpho calls back on whoever called flashLoan, so there is
/// no third party who could aim it here).
contract MorphoForkReceiver is IMorphoFlashLoanCallback {
    IMorpho internal immutable morpho;
    address internal immutable token;

    constructor(address morphoAddress, address tokenAddress) {
        morpho = IMorpho(morphoAddress);
        token = tokenAddress;
    }

    function borrow(uint256 amount) external {
        morpho.flashLoan(token, amount, "");
    }

    function onMorphoFlashLoan(uint256 assets, bytes calldata) external {
        require(msg.sender == address(morpho), "unexpected morpho");
        // Exactly `assets` -- no premium term, which is the entire point.
        IERC20(token).approve(address(morpho), assets);
    }
}

contract BaseMainnetForkTest {
    Vm private constant vm = Vm(address(uint160(uint256(keccak256("hevm cheat code")))));

    uint256 private constant PINNED_BLOCK = 50_048_341;
    bytes32 private constant PINNED_BLOCK_HASH = 0x113715da2993561eca7da017a159cc3ca24fc83f0a1e66632ac6447ed5ea7814;

    address private constant AAVE_POOL = 0xA238Dd80C259a72e81d7e4664a9801593F98d1c5;
    address private constant AAVE_AWETH = 0xD4a0e0b9149BCee3C920d2E00b5dE09138fd8bb7;
    address private constant MORPHO = 0xBBBBBbbBBb9cC5e90e3b3Af64bdAF62C37EEFFCb;
    address private constant WETH = 0x4200000000000000000000000000000000000006;
    address private constant USDC = 0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913;
    address private constant AERODROME_ROUTER = 0xcF77a3Ba9A5CA399B7c97c74d54e5b1Beb874E43;
    address private constant AERODROME_FACTORY = 0x420DD381b31aEf6683db6B902084cB0FFECe40Da;
    address private constant UNISWAP_SWAP_ROUTER_02 = 0x2626664c2603336E57B271c5C0b26F421741e481;

    uint256 private constant SWAP_INPUT = 0.01 ether;

    function setUp() public {
        vm.createSelectFork(vm.envString("BASE_HTTP_RPC"), PINNED_BLOCK);
        require(block.number == PINNED_BLOCK, "fork block mismatch");
        require(PINNED_BLOCK_HASH != bytes32(0), "fork hash metadata missing");
        require(AAVE_POOL.code.length > 0, "Aave pool missing");
        require(WETH.code.length > 0 && USDC.code.length > 0, "token missing");
        require(AERODROME_ROUTER.code.length > 0 && AERODROME_FACTORY.code.length > 0, "Aerodrome missing");
        require(UNISWAP_SWAP_ROUTER_02.code.length > 0, "Uniswap router missing");
        vm.deal(address(this), 1 ether);
    }

    function testRealAaveFlashLoanRepaysExactPremium() public {
        uint256 amount = SWAP_INPUT;
        uint256 premium = (amount * IAavePool(AAVE_POOL).FLASHLOAN_PREMIUM_TOTAL()) / 10_000;
        AaveForkReceiver receiver = new AaveForkReceiver(AAVE_POOL);

        IWETH(WETH).deposit{value: premium}();
        require(IERC20(WETH).transfer(address(receiver), premium), "premium funding failed");
        receiver.borrow(WETH, amount);

        require(IERC20(WETH).balanceOf(address(receiver)) == 0, "flash repayment residue");
    }

    /// The same borrow against the real Morpho Blue singleton, funded with nothing.
    ///
    /// This is the claim the whole second provider rests on, checked against the deployed
    /// contract rather than a mock: a receiver holding zero WETH can borrow and repay, because
    /// there is no premium to find. The Aave twin above has to be pre-funded with the premium
    /// or it reverts -- see testRealAaveFlashLoanFailsWithoutPremium.
    function testRealMorphoFlashLoanCostsNothing() public {
        MorphoForkReceiver receiver = new MorphoForkReceiver(MORPHO, WETH);

        require(IERC20(WETH).balanceOf(address(receiver)) == 0, "receiver should start empty");
        receiver.borrow(SWAP_INPUT);
        require(IERC20(WETH).balanceOf(address(receiver)) == 0, "flash repayment residue");
    }

    /// Sizing depends on this: Morpho lends what the singleton holds, and on Base that is far
    /// more WETH than the Aave reserve. Asserted as a floor rather than a figure so the test
    /// does not fail every time liquidity moves.
    function testMorphoHoldsMoreFlashableWethThanAave() public view {
        uint256 morphoWeth = IERC20(WETH).balanceOf(MORPHO);
        uint256 aaveWeth = IERC20(WETH).balanceOf(AAVE_AWETH);
        require(morphoWeth > aaveWeth, "morpho no longer the deeper venue");
        require(morphoWeth > 1_000 ether, "morpho weth unexpectedly shallow");
    }

    function testRealAaveFlashLoanFailsWithoutPremium() public {
        AaveForkReceiver receiver = new AaveForkReceiver(AAVE_POOL);
        (bool ok,) = address(receiver).call(abi.encodeCall(receiver.borrow, (WETH, SWAP_INPUT)));
        require(!ok, "underfunded flash repayment accepted");
    }

    function testUniswapAdapterUsesRealPoolAndExactInput() public {
        UniswapV3Adapter adapter = new UniswapV3Adapter(address(this), UNISWAP_SWAP_ROUTER_02, true);
        IWETH(WETH).deposit{value: SWAP_INPUT}();
        IERC20(WETH).approve(address(adapter), SWAP_INPUT);

        uint256 wethBefore = IERC20(WETH).balanceOf(address(this));
        uint256 usdcBefore = IERC20(USDC).balanceOf(address(this));
        uint256 reported =
            adapter.swap(WETH, USDC, SWAP_INPUT, 18_000_000, address(this), abi.encode(uint24(500), uint160(0)));
        uint256 usdcDelta = IERC20(USDC).balanceOf(address(this)) - usdcBefore;

        require(wethBefore - IERC20(WETH).balanceOf(address(this)) == SWAP_INPUT, "Uniswap input mismatch");
        require(reported == usdcDelta && usdcDelta >= 18_000_000, "Uniswap output mismatch");
        require(IERC20(WETH).balanceOf(address(adapter)) == 0, "Uniswap adapter input residue");
        require(IERC20(USDC).balanceOf(address(adapter)) == 0, "Uniswap adapter output residue");
    }

    function testAerodromeAdapterUsesRealPoolAndExactInput() public {
        AerodromeAdapter adapter = new AerodromeAdapter(address(this), AERODROME_ROUTER, AERODROME_FACTORY);
        IWETH(WETH).deposit{value: SWAP_INPUT}();
        IERC20(WETH).approve(address(adapter), SWAP_INPUT);

        uint256 wethBefore = IERC20(WETH).balanceOf(address(this));
        uint256 usdcBefore = IERC20(USDC).balanceOf(address(this));
        uint256 reported = adapter.swap(WETH, USDC, SWAP_INPUT, 18_000_000, address(this), abi.encode(false));
        uint256 usdcDelta = IERC20(USDC).balanceOf(address(this)) - usdcBefore;

        require(wethBefore - IERC20(WETH).balanceOf(address(this)) == SWAP_INPUT, "Aerodrome input mismatch");
        require(reported == usdcDelta && usdcDelta >= 18_000_000, "Aerodrome output mismatch");
        require(IERC20(WETH).balanceOf(address(adapter)) == 0, "Aerodrome adapter input residue");
        require(IERC20(USDC).balanceOf(address(adapter)) == 0, "Aerodrome adapter output residue");
    }

    function testExecutorRejectsUnprofitableRealProtocolCycle() public {
        BaseArbExecutor exec = new BaseArbExecutor(AAVE_POOL, address(0));
        AerodromeAdapter aero = new AerodromeAdapter(address(exec), AERODROME_ROUTER, AERODROME_FACTORY);
        UniswapV3Adapter uni = new UniswapV3Adapter(address(exec), UNISWAP_SWAP_ROUTER_02, true);

        exec.setAdapter(address(aero), true);
        exec.setAdapter(address(uni), true);
        exec.setToken(WETH, true);
        exec.setToken(USDC, true);
        exec.setPaused(false);

        BaseArbExecutor.Leg[] memory legs = new BaseArbExecutor.Leg[](2);
        legs[0] = BaseArbExecutor.Leg(address(aero), WETH, USDC, 1, abi.encode(false));
        legs[1] = BaseArbExecutor.Leg(address(uni), USDC, WETH, 1, abi.encode(uint24(500), uint160(0)));
        BaseArbExecutor.Route memory route =
            BaseArbExecutor.Route(bytes32(0), WETH, SWAP_INPUT, 0, uint64(block.number), type(uint64).max, legs);
        route.routeHash = hashRoute(route);

        (bool ok,) = address(exec).call(abi.encodeCall(exec.start, (route, BaseArbExecutor.FlashProvider.Aave)));
        require(!ok, "unprofitable real cycle accepted");
        require(!exec.commitmentUsed(route.routeHash), "reverted commitment remained consumed");
    }

    function hashRoute(BaseArbExecutor.Route memory route) internal pure returns (bytes32) {
        bytes32 legsHash;
        for (uint256 i; i < route.legs.length; ++i) {
            BaseArbExecutor.Leg memory leg = route.legs[i];
            legsHash = keccak256(
                abi.encode(legsHash, leg.adapter, leg.tokenIn, leg.tokenOut, leg.minOut, keccak256(leg.data))
            );
        }
        return
            keccak256(
                abi.encode(route.asset, route.amount, route.minProfit, route.targetBlock, route.deadline, legsHash)
            );
    }
}
