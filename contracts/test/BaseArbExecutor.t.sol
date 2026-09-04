// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import "../src/BaseArbExecutor.sol";
import "../src/mocks.sol";

contract OwnershipAcceptor {
    function accept(BaseArbExecutor exec) external {
        exec.acceptOwnership();
    }
}

contract BaseArbExecutorTest {
    function testProfitableTwoLegRouteRepaysAndLeavesNetProfit() public {
        MockPool pool = new MockPool(10); // test fixture: 10 bps
        MockERC20 tokenA = new MockERC20();
        MockERC20 tokenB = new MockERC20();
        BaseArbExecutor exec = new BaseArbExecutor(address(pool), address(0));
        MockExchangeAdapter a1 = new MockExchangeAdapter(2, 1);
        MockExchangeAdapter a2 = new MockExchangeAdapter(55, 100);

        exec.setAdapter(address(a1), true);
        exec.setAdapter(address(a2), true);
        exec.setToken(address(tokenA), true);
        exec.setToken(address(tokenB), true);
        exec.setPaused(false);

        BaseArbExecutor.Leg[] memory legs = new BaseArbExecutor.Leg[](2);
        legs[0] = BaseArbExecutor.Leg(address(a1), address(tokenA), address(tokenB), 2000, "");
        legs[1] = BaseArbExecutor.Leg(address(a2), address(tokenB), address(tokenA), 1100, "");

        BaseArbExecutor.Route memory route =
            BaseArbExecutor.Route(bytes32(0), address(tokenA), 1000, 99, uint64(block.number), type(uint64).max, legs);
        route.routeHash = hashRoute(route);
        exec.start(route, BaseArbExecutor.FlashProvider.Aave);

        require(tokenA.balanceOf(address(exec)) == 99, "net profit mismatch");
        require(tokenA.balanceOf(address(pool)) == 1001, "pool not repaid");
        require(tokenB.balanceOf(address(exec)) == 0, "intermediate residue");
    }

    function testRouteHashRejectsMutation() public {
        MockPool pool = new MockPool(10);
        MockERC20 tokenA = new MockERC20();
        MockERC20 tokenB = new MockERC20();
        BaseArbExecutor exec = new BaseArbExecutor(address(pool), address(0));
        MockExchangeAdapter a1 = new MockExchangeAdapter(2, 1);
        MockExchangeAdapter a2 = new MockExchangeAdapter(55, 100);
        exec.setAdapter(address(a1), true);
        exec.setAdapter(address(a2), true);
        exec.setToken(address(tokenA), true);
        exec.setToken(address(tokenB), true);
        exec.setPaused(false);

        BaseArbExecutor.Leg[] memory legs = new BaseArbExecutor.Leg[](2);
        legs[0] = BaseArbExecutor.Leg(address(a1), address(tokenA), address(tokenB), 2000, "");
        legs[1] = BaseArbExecutor.Leg(address(a2), address(tokenB), address(tokenA), 1100, "");
        BaseArbExecutor.Route memory route =
            BaseArbExecutor.Route(bytes32(0), address(tokenA), 1000, 99, uint64(block.number), type(uint64).max, legs);
        route.routeHash = hashRoute(route);
        route.minProfit = 98;
        (bool ok,) = address(exec).call(abi.encodeCall(exec.start, (route, BaseArbExecutor.FlashProvider.Aave)));
        require(!ok, "mutated route accepted");
    }

    function testPackedBatchFallsBackToSecondCandidate() public {
        MockPool pool = new MockPool(10);
        MockERC20 tokenA = new MockERC20();
        MockERC20 tokenB = new MockERC20();
        BaseArbExecutor exec = new BaseArbExecutor(address(pool), address(0));
        MockExchangeAdapter weak = new MockExchangeAdapter(1, 2);
        MockExchangeAdapter good1 = new MockExchangeAdapter(2, 1);
        MockExchangeAdapter good2 = new MockExchangeAdapter(55, 100);

        exec.setAdapter(address(weak), true);
        exec.setAdapter(address(good1), true);
        exec.setAdapter(address(good2), true);
        exec.setToken(address(tokenA), true);
        exec.setToken(address(tokenB), true);
        exec.setPaused(false);

        bytes memory badCandidate =
            encodeTwoLegCandidate(1, 1, address(weak), address(good2), address(tokenA), address(tokenB), 900, 1000);
        bytes memory goodCandidate =
            encodeTwoLegCandidate(2, 99, address(good1), address(good2), address(tokenA), address(tokenB), 2000, 1100);
        bytes memory packed = abi.encodePacked(
            uint8(1),
            uint8(2),
            address(tokenA),
            uint256(1000),
            uint64(block.number),
            type(uint64).max,
            badCandidate,
            goodCandidate
        );

        exec.startPacked(packed, BaseArbExecutor.FlashProvider.Aave);
        require(tokenA.balanceOf(address(exec)) == 99, "packed net profit mismatch");
        require(tokenA.balanceOf(address(pool)) == 1001, "packed pool not repaid");
        require(tokenB.balanceOf(address(exec)) == 0, "packed intermediate residue");
    }

    function testPackedBatchRejectsTrailingBytes() public {
        MockPool pool = new MockPool(10);
        BaseArbExecutor exec = new BaseArbExecutor(address(pool), address(0));
        bytes memory malformed = abi.encodePacked(
            uint8(1), uint8(1), bytes20(0), uint256(1), uint64(block.number), type(uint64).max, bytes1(0)
        );
        (bool ok,) = address(exec).call(abi.encodeCall(exec.startPacked, (malformed, BaseArbExecutor.FlashProvider.Aave)));
        require(!ok, "malformed packed batch accepted");
    }

    function testDeploysPausedAndTransfersOwnershipInTwoSteps() public {
        BaseArbExecutor exec = new BaseArbExecutor(address(new MockPool(10)), address(0));
        OwnershipAcceptor next = new OwnershipAcceptor();

        require(exec.paused(), "executor must deploy paused");
        exec.transferOwnership(address(next));
        require(exec.owner() == address(this), "ownership transferred before acceptance");
        require(exec.pendingOwner() == address(next), "pending owner mismatch");

        next.accept(exec);
        require(exec.owner() == address(next), "ownership acceptance failed");
        require(exec.pendingOwner() == address(0), "pending owner not cleared");
    }

    function testRouteCommitmentCannotBeReplayed() public {
        MockPool pool = new MockPool(10);
        MockERC20 tokenA = new MockERC20();
        MockERC20 tokenB = new MockERC20();
        BaseArbExecutor exec = new BaseArbExecutor(address(pool), address(0));
        MockExchangeAdapter a1 = new MockExchangeAdapter(2, 1);
        MockExchangeAdapter a2 = new MockExchangeAdapter(55, 100);

        exec.setAdapter(address(a1), true);
        exec.setAdapter(address(a2), true);
        exec.setToken(address(tokenA), true);
        exec.setToken(address(tokenB), true);
        exec.setPaused(false);

        BaseArbExecutor.Leg[] memory legs = new BaseArbExecutor.Leg[](2);
        legs[0] = BaseArbExecutor.Leg(address(a1), address(tokenA), address(tokenB), 2000, "");
        legs[1] = BaseArbExecutor.Leg(address(a2), address(tokenB), address(tokenA), 1100, "");
        BaseArbExecutor.Route memory route =
            BaseArbExecutor.Route(bytes32(0), address(tokenA), 1000, 99, uint64(block.number), type(uint64).max, legs);
        route.routeHash = hashRoute(route);

        exec.start(route, BaseArbExecutor.FlashProvider.Aave);
        (bool ok,) = address(exec).call(abi.encodeCall(exec.start, (route, BaseArbExecutor.FlashProvider.Aave)));
        require(!ok, "route commitment replayed");
    }

    function testFuzzRouteHashCommitsMinimumProfit(uint256 mutatedMinProfit) public pure {
        BaseArbExecutor.Leg[] memory legs = new BaseArbExecutor.Leg[](2);
        legs[0] = BaseArbExecutor.Leg(address(1), address(2), address(3), 4, "");
        legs[1] = BaseArbExecutor.Leg(address(5), address(3), address(2), 6, "");
        BaseArbExecutor.Route memory route = BaseArbExecutor.Route(bytes32(0), address(2), 7, 8, 9, 10, legs);
        bytes32 original = hashRoute(route);

        route.minProfit = mutatedMinProfit == 8 ? 9 : mutatedMinProfit;
        require(hashRoute(route) != original, "minimum profit missing from commitment");
    }

    // --- Morpho as a second flash-loan provider -------------------------------------

    /// The whole economic point: Morpho charges no premium, so the executor repays exactly what
    /// it borrowed and the 5bp Aave takes stays with the route instead.
    function testMorphoRouteRepaysPrincipalOnlyAndKeepsThePremium() public {
        MockPool pool = new MockPool(10); // 10 bps, deliberately non-zero
        MockMorpho morpho = new MockMorpho();
        MockERC20 tokenA = new MockERC20();
        MockERC20 tokenB = new MockERC20();
        BaseArbExecutor exec = new BaseArbExecutor(address(pool), address(morpho));
        MockExchangeAdapter a1 = new MockExchangeAdapter(2, 1);
        MockExchangeAdapter a2 = new MockExchangeAdapter(55, 100);

        exec.setAdapter(address(a1), true);
        exec.setAdapter(address(a2), true);
        exec.setToken(address(tokenA), true);
        exec.setToken(address(tokenB), true);
        exec.setPaused(false);

        BaseArbExecutor.Leg[] memory legs = new BaseArbExecutor.Leg[](2);
        legs[0] = BaseArbExecutor.Leg(address(a1), address(tokenA), address(tokenB), 2000, "");
        legs[1] = BaseArbExecutor.Leg(address(a2), address(tokenB), address(tokenA), 1100, "");

        BaseArbExecutor.Route memory route =
            BaseArbExecutor.Route(bytes32(0), address(tokenA), 1000, 99, uint64(block.number), type(uint64).max, legs);
        route.routeHash = hashRoute(route);
        exec.start(route, BaseArbExecutor.FlashProvider.Morpho);

        // Same route on Aave nets 99 and repays 1001 (see the Aave test above). Here the
        // borrower keeps the extra 1 that Aave would have charged as premium.
        require(tokenA.balanceOf(address(morpho)) == 1000, "morpho must be repaid principal only");
        require(tokenA.balanceOf(address(exec)) == 100, "premium should have stayed with the route");
        require(tokenA.balanceOf(address(pool)) == 0, "aave must not be touched");
        require(tokenB.balanceOf(address(exec)) == 0, "intermediate residue");
    }

    function testPackedBatchRunsOnMorpho() public {
        MockPool pool = new MockPool(10);
        MockMorpho morpho = new MockMorpho();
        MockERC20 tokenA = new MockERC20();
        MockERC20 tokenB = new MockERC20();
        BaseArbExecutor exec = new BaseArbExecutor(address(pool), address(morpho));
        MockExchangeAdapter a1 = new MockExchangeAdapter(2, 1);
        MockExchangeAdapter a2 = new MockExchangeAdapter(55, 100);

        exec.setAdapter(address(a1), true);
        exec.setAdapter(address(a2), true);
        exec.setToken(address(tokenA), true);
        exec.setToken(address(tokenB), true);
        exec.setPaused(false);

        bytes memory candidate =
            encodeTwoLegCandidate(1, 50, address(a1), address(a2), address(tokenA), address(tokenB), 2000, 1050);
        bytes memory packed = abi.encodePacked(
            uint8(1), uint8(1), address(tokenA), uint256(1000), uint64(block.number), type(uint64).max, candidate
        );
        exec.startPacked(packed, BaseArbExecutor.FlashProvider.Morpho);

        require(tokenA.balanceOf(address(morpho)) == 1000, "morpho must be repaid principal only");
        require(tokenA.balanceOf(address(pool)) == 0, "aave must not be touched");
    }

    /// Morpho is opt-in. An executor deployed without it must refuse rather than quietly fall
    /// back to Aave and bill a premium the caller did not ask for.
    function testMorphoRouteRevertsWhenNoMorphoConfigured() public {
        MockPool pool = new MockPool(10);
        MockERC20 tokenA = new MockERC20();
        MockERC20 tokenB = new MockERC20();
        BaseArbExecutor exec = new BaseArbExecutor(address(pool), address(0));
        MockExchangeAdapter a1 = new MockExchangeAdapter(2, 1);
        MockExchangeAdapter a2 = new MockExchangeAdapter(55, 100);

        exec.setAdapter(address(a1), true);
        exec.setAdapter(address(a2), true);
        exec.setToken(address(tokenA), true);
        exec.setToken(address(tokenB), true);
        exec.setPaused(false);

        BaseArbExecutor.Leg[] memory legs = new BaseArbExecutor.Leg[](2);
        legs[0] = BaseArbExecutor.Leg(address(a1), address(tokenA), address(tokenB), 2000, "");
        legs[1] = BaseArbExecutor.Leg(address(a2), address(tokenB), address(tokenA), 1100, "");
        BaseArbExecutor.Route memory route =
            BaseArbExecutor.Route(bytes32(0), address(tokenA), 1000, 99, uint64(block.number), type(uint64).max, legs);
        route.routeHash = hashRoute(route);

        (bool ok,) = address(exec).call(
            abi.encodeCall(exec.start, (route, BaseArbExecutor.FlashProvider.Morpho))
        );
        require(!ok, "must not fall back to aave");
        require(tokenA.balanceOf(address(pool)) == 0, "aave must not be touched");
    }

    /// FlashProvider.None is the idle marker, never a selectable venue.
    function testProviderNoneIsRejected() public {
        MockPool pool = new MockPool(10);
        MockMorpho morpho = new MockMorpho();
        MockERC20 tokenA = new MockERC20();
        MockERC20 tokenB = new MockERC20();
        BaseArbExecutor exec = new BaseArbExecutor(address(pool), address(morpho));
        MockExchangeAdapter a1 = new MockExchangeAdapter(2, 1);
        MockExchangeAdapter a2 = new MockExchangeAdapter(55, 100);
        exec.setAdapter(address(a1), true);
        exec.setAdapter(address(a2), true);
        exec.setToken(address(tokenA), true);
        exec.setToken(address(tokenB), true);
        exec.setPaused(false);

        BaseArbExecutor.Leg[] memory legs = new BaseArbExecutor.Leg[](2);
        legs[0] = BaseArbExecutor.Leg(address(a1), address(tokenA), address(tokenB), 2000, "");
        legs[1] = BaseArbExecutor.Leg(address(a2), address(tokenB), address(tokenA), 1100, "");
        BaseArbExecutor.Route memory route =
            BaseArbExecutor.Route(bytes32(0), address(tokenA), 1000, 99, uint64(block.number), type(uint64).max, legs);
        route.routeHash = hashRoute(route);

        (bool ok,) = address(exec).call(
            abi.encodeCall(exec.start, (route, BaseArbExecutor.FlashProvider.None))
        );
        require(!ok, "None must not be borrowable");
    }

    /// Both callbacks are unreachable while no borrow of their kind is in flight. Morpho aims
    /// its callback at whoever called flashLoan(), so an attacker cannot direct it here -- but
    /// the idle guard means even a compromised provider address cannot drive a stale route.
    function testCallbacksRejectDirectInvocationWhileIdle() public {
        MockPool pool = new MockPool(10);
        MockMorpho morpho = new MockMorpho();
        BaseArbExecutor exec = new BaseArbExecutor(address(pool), address(morpho));

        (bool morphoOk,) = address(exec).call(abi.encodeCall(exec.onMorphoFlashLoan, (1000, "")));
        require(!morphoOk, "morpho callback must be idle-guarded");

        (bool aaveOk,) = address(exec).call(
            abi.encodeCall(exec.executeOperation, (address(0), 1000, 0, address(exec), ""))
        );
        require(!aaveOk, "aave callback must be idle-guarded");
    }

    function hashRoute(BaseArbExecutor.Route memory r) internal pure returns (bytes32) {
        bytes32 legsHash;
        for (uint256 i; i < r.legs.length; ++i) {
            BaseArbExecutor.Leg memory leg = r.legs[i];
            legsHash = keccak256(
                abi.encode(legsHash, leg.adapter, leg.tokenIn, leg.tokenOut, leg.minOut, keccak256(leg.data))
            );
        }
        return keccak256(abi.encode(r.asset, r.amount, r.minProfit, r.targetBlock, r.deadline, legsHash));
    }

    function encodeTwoLegCandidate(
        uint256 candidateId,
        uint256 minProfit,
        address firstAdapter,
        address secondAdapter,
        address tokenA,
        address tokenB,
        uint256 firstMinOut,
        uint256 secondMinOut
    ) internal pure returns (bytes memory) {
        bytes memory firstLeg = abi.encodePacked(firstAdapter, tokenA, tokenB, firstMinOut, uint16(0));
        bytes memory secondLeg = abi.encodePacked(secondAdapter, tokenB, tokenA, secondMinOut, uint16(0));
        return abi.encodePacked(bytes32(candidateId), minProfit, uint8(2), firstLeg, secondLeg);
    }
}
