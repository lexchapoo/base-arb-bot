// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import "./interfaces.sol";

contract BaseArbExecutor is IFlashLoanSimpleReceiver {
    error Unauthorized();
    error InvalidRoute();
    error StaleRoute();
    error InsufficientRepayment();
    error ProfitTooLow();
    error Reentrant();
    error Paused();
    error DirtyIntermediateBalance(address token, uint256 balance);
    error SwapOutputMismatch(uint256 leg);
    error TokenCallFailed(address token);
    error InvalidPackedBatch();
    error NoCandidateSucceeded();
    error CommitmentAlreadyUsed();

    uint8 public constant PACKED_VERSION = 1;
    uint8 public constant MAX_PACKED_CANDIDATES = 8;
    uint8 public constant MAX_PACKED_LEGS = 4;
    uint16 public constant MAX_PACKED_LEG_DATA = 2048;

    struct Leg {
        address adapter;
        address tokenIn;
        address tokenOut;
        uint256 minOut;
        bytes data;
    }

    struct Route {
        bytes32 routeHash;
        address asset;
        uint256 amount;
        uint256 minProfit;
        uint64 targetBlock;
        uint64 deadline;
        Leg[] legs;
    }

    struct PackedCandidate {
        bytes32 candidateId;
        uint256 minProfit;
        Leg[] legs;
    }

    struct PackedHeader {
        address asset;
        uint256 amount;
        uint64 targetBlock;
        uint64 deadline;
        uint8 candidateCount;
    }

    IAavePool public immutable POOL;
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

    event RouteExecuted(bytes32 indexed routeHash, address indexed asset, uint256 amount, uint256 premium, uint256 netProfit);
    event PackedRouteExecuted(
        bytes32 indexed batchHash,
        bytes32 indexed candidateId,
        uint8 candidateIndex,
        address indexed asset,
        uint256 amount,
        uint256 premium,
        uint256 netProfit
    );
    event AdapterPermission(address indexed adapter, bool allowed);
    event TokenPermission(address indexed token, bool allowed);
    event PauseChanged(bool paused);
    event OwnershipTransferStarted(address indexed currentOwner, address indexed pendingOwner);
    event OwnershipTransferred(address indexed previousOwner, address indexed newOwner);

    modifier onlyOwner() {
        if (msg.sender != owner) revert Unauthorized();
        _;
    }

    modifier onlySelf() {
        if (msg.sender != address(this)) revert Unauthorized();
        _;
    }

    /// Admin surface may only move while no flash loan is in flight. Without this, a route
    /// validated by _validateRoute under one allowlist could finish executing its legs under
    /// a different one (or with `paused` flipped) because an adapter callback re-entered an
    /// owner function mid-loan.
    modifier onlyIdle() {
        if (callbackEntered || activeRouteHash != bytes32(0) || activeBatchHash != bytes32(0)) {
            revert Reentrant();
        }
        _;
    }

    constructor(address pool) {
        if (pool == address(0)) revert Unauthorized();
        POOL = IAavePool(pool);
        owner = msg.sender;
        // Deploy dark: the operator must explicitly arm the executor with setPaused(false)
        // after adapters/tokens are allowlisted and verified.
        paused = true;
    }

    function setAdapter(address adapter, bool allowed) external onlyOwner onlyIdle {
        adapterAllowed[adapter] = allowed;
        emit AdapterPermission(adapter, allowed);
    }

    function setToken(address token, bool allowed) external onlyOwner onlyIdle {
        tokenAllowed[token] = allowed;
        emit TokenPermission(token, allowed);
    }

    function setPaused(bool value) external onlyOwner onlyIdle {
        paused = value;
        emit PauseChanged(value);
    }

    function transferOwnership(address next) external onlyOwner onlyIdle {
        if (next == address(0)) revert Unauthorized();
        pendingOwner = next;
        emit OwnershipTransferStarted(owner, next);
    }

    /// Two-step handover: the incoming owner must prove control of `next` before it takes
    /// effect, so a typo or an address the key does not control cannot brick the executor.
    function acceptOwnership() external onlyIdle {
        if (msg.sender != pendingOwner) revert Unauthorized();
        address previous = owner;
        owner = msg.sender;
        pendingOwner = address(0);
        emit OwnershipTransferred(previous, msg.sender);
    }

    function start(Route calldata route) external onlyOwner {
        if (paused) revert Paused();
        if (activeRouteHash != bytes32(0) || activeBatchHash != bytes32(0)) revert Reentrant();
        if (block.number > route.targetBlock || block.timestamp > route.deadline) revert StaleRoute();
        if (_hash(route) != route.routeHash) revert InvalidRoute();
        if (commitmentUsed[route.routeHash]) revert CommitmentAlreadyUsed();
        _validateRoute(route);
        _requireCleanIntermediates(route);
        commitmentUsed[route.routeHash] = true;

        activeRouteHash = route.routeHash;
        activePreLoanBalance = IERC20(route.asset).balanceOf(address(this));

        POOL.flashLoanSimple(address(this), route.asset, route.amount, abi.encode(route), 0);

        uint256 finalBalance = IERC20(route.asset).balanceOf(address(this));
        uint256 preBalance = activePreLoanBalance;
        uint256 chargedPremium = activePremium;
        activeRouteHash = bytes32(0);
        activePreLoanBalance = 0;
        activePremium = 0;

        if (finalBalance < preBalance + route.minProfit) revert ProfitTooLow();
        emit RouteExecuted(route.routeHash, route.asset, route.amount, chargedPremium, finalBalance - preBalance);
    }

    /// @notice Execute a compact menu of pre-ranked routes under one Aave flash loan.
    /// @dev Packed layout, big-endian integers:
    /// version:u8 | candidateCount:u8 | asset:20 | amount:u256 | targetBlock:u64 | deadline:u64 |
    /// repeated candidate: candidateId:bytes32 | minProfit:u256 | legCount:u8 |
    /// repeated leg: adapter:20 | tokenIn:20 | tokenOut:20 | minOut:u256 | dataLen:u16 | data:dataLen.
    function startPacked(bytes calldata packed) external onlyOwner {
        if (paused) revert Paused();
        if (activeRouteHash != bytes32(0) || activeBatchHash != bytes32(0)) revert Reentrant();

        (PackedHeader memory header, PackedCandidate[] memory candidates) = _decodePacked(packed);
        if (block.number > header.targetBlock || block.timestamp > header.deadline) revert StaleRoute();
        _validatePacked(header, candidates);
        _requireCleanPackedIntermediates(header.asset, candidates);

        bytes32 batchHash = keccak256(packed);
        if (commitmentUsed[batchHash]) revert CommitmentAlreadyUsed();
        commitmentUsed[batchHash] = true;
        activeBatchHash = batchHash;
        activePreLoanBalance = IERC20(header.asset).balanceOf(address(this));
        activeSelectedCandidateId = bytes32(0);
        activeSelectedCandidateIndex = 0;
        activeSelectedMinProfit = 0;

        POOL.flashLoanSimple(address(this), header.asset, header.amount, packed, 0);

        uint256 finalBalance = IERC20(header.asset).balanceOf(address(this));
        uint256 preBalance = activePreLoanBalance;
        uint256 chargedPremium = activePremium;
        bytes32 selectedId = activeSelectedCandidateId;
        uint8 selectedIndex = activeSelectedCandidateIndex;
        uint256 selectedMinProfit = activeSelectedMinProfit;

        activeBatchHash = bytes32(0);
        activePreLoanBalance = 0;
        activePremium = 0;
        activeSelectedCandidateId = bytes32(0);
        activeSelectedCandidateIndex = 0;
        activeSelectedMinProfit = 0;

        if (selectedId == bytes32(0)) revert NoCandidateSucceeded();
        if (finalBalance < preBalance + selectedMinProfit) revert ProfitTooLow();
        emit PackedRouteExecuted(batchHash, selectedId, selectedIndex, header.asset, header.amount, chargedPremium, finalBalance - preBalance);
    }

    function executeOperation(
        address asset,
        uint256 amount,
        uint256 premium,
        address initiator,
        bytes calldata params
    ) external returns (bool) {
        if (msg.sender != address(POOL) || initiator != address(this)) revert Unauthorized();
        if (callbackEntered) revert Reentrant();
        callbackEntered = true;

        if (activeBatchHash != bytes32(0)) {
            _executePackedCallback(asset, amount, premium, params);
            callbackEntered = false;
            return true;
        }

        Route memory route = abi.decode(params, (Route));
        if (activeRouteHash == bytes32(0) || route.routeHash != activeRouteHash) revert InvalidRoute();
        if (asset != route.asset || amount != route.amount || _hashMemory(route) != route.routeHash) revert InvalidRoute();
        if (block.number > route.targetBlock || block.timestamp > route.deadline) revert StaleRoute();
        _validateRouteMemory(route);

        _executeLegs(route.legs, amount);

        activePremium = premium;
        uint256 afterBalance = IERC20(asset).balanceOf(address(this));
        uint256 owed = amount + premium;
        uint256 required = activePreLoanBalance + owed + route.minProfit;
        if (afterBalance < owed) revert InsufficientRepayment();
        if (afterBalance < required) revert ProfitTooLow();

        _forceApprove(asset, address(POOL), owed);
        callbackEntered = false;
        return true;
    }

    function attemptPackedCandidate(PackedCandidate calldata candidate, address asset, uint256 amount, uint256 premium)
        external
        onlySelf
        returns (uint256 endingBalance)
    {
        if (activeBatchHash == bytes32(0)) revert InvalidPackedBatch();
        if (candidate.legs.length < 2 || candidate.legs.length > MAX_PACKED_LEGS) revert InvalidRoute();
        if (candidate.legs[0].tokenIn != asset || candidate.legs[candidate.legs.length - 1].tokenOut != asset) revert InvalidRoute();

        uint256 beforeBalance = IERC20(asset).balanceOf(address(this));
        _executeLegsCalldata(candidate.legs, amount);
        endingBalance = IERC20(asset).balanceOf(address(this));

        // beforeBalance already includes the borrowed amount. Route profit must cover premium + minProfit.
        if (endingBalance < beforeBalance + premium + candidate.minProfit) revert ProfitTooLow();
    }

    function sweep(address token, address to, uint256 amount) external onlyOwner onlyIdle {
        _safeTransfer(token, to, amount);
    }

    function routeHash(Route calldata route) external pure returns (bytes32) {
        return _hash(route);
    }

    function packedHash(bytes calldata packed) external pure returns (bytes32) {
        return keccak256(packed);
    }

    function _executePackedCallback(address asset, uint256 amount, uint256 premium, bytes calldata params) internal {
        if (keccak256(params) != activeBatchHash) revert InvalidPackedBatch();
        (PackedHeader memory header, PackedCandidate[] memory candidates) = _decodePacked(params);
        if (asset != header.asset || amount != header.amount) revert InvalidPackedBatch();
        if (block.number > header.targetBlock || block.timestamp > header.deadline) revert StaleRoute();
        _validatePacked(header, candidates);

        bool success;
        for (uint8 i; i < candidates.length; ++i) {
            try this.attemptPackedCandidate(candidates[i], asset, amount, premium) returns (uint256) {
                activeSelectedCandidateId = candidates[i].candidateId;
                activeSelectedCandidateIndex = i;
                activeSelectedMinProfit = candidates[i].minProfit;
                success = true;
                break;
            } catch {
                // The external self-call gives each failed route its own revert boundary, including nested DEX state.
            }
        }
        if (!success) revert NoCandidateSucceeded();

        activePremium = premium;
        uint256 owed = amount + premium;
        if (IERC20(asset).balanceOf(address(this)) < activePreLoanBalance + owed + activeSelectedMinProfit) revert ProfitTooLow();
        _forceApprove(asset, address(POOL), owed);
    }

    function _executeLegs(Leg[] memory legs, uint256 firstAmount) internal {
        for (uint256 i; i < legs.length; ++i) {
            Leg memory leg = legs[i];
            uint256 amountIn = i == 0 ? firstAmount : IERC20(leg.tokenIn).balanceOf(address(this));
            if (amountIn == 0) revert InvalidRoute();
            uint256 beforeOut = IERC20(leg.tokenOut).balanceOf(address(this));
            _forceApprove(leg.tokenIn, leg.adapter, amountIn);
            uint256 reported = IExchangeAdapter(leg.adapter).swap(leg.tokenIn, leg.tokenOut, amountIn, leg.minOut, address(this), leg.data);
            _forceApprove(leg.tokenIn, leg.adapter, 0);
            uint256 delta = IERC20(leg.tokenOut).balanceOf(address(this)) - beforeOut;
            if (delta < leg.minOut || reported != delta) revert SwapOutputMismatch(i);
        }
    }

    function _executeLegsCalldata(Leg[] calldata legs, uint256 firstAmount) internal {
        for (uint256 i; i < legs.length; ++i) {
            Leg calldata leg = legs[i];
            uint256 amountIn = i == 0 ? firstAmount : IERC20(leg.tokenIn).balanceOf(address(this));
            if (amountIn == 0) revert InvalidRoute();
            uint256 beforeOut = IERC20(leg.tokenOut).balanceOf(address(this));
            _forceApprove(leg.tokenIn, leg.adapter, amountIn);
            uint256 reported = IExchangeAdapter(leg.adapter).swap(leg.tokenIn, leg.tokenOut, amountIn, leg.minOut, address(this), leg.data);
            _forceApprove(leg.tokenIn, leg.adapter, 0);
            uint256 delta = IERC20(leg.tokenOut).balanceOf(address(this)) - beforeOut;
            if (delta < leg.minOut || reported != delta) revert SwapOutputMismatch(i);
        }
    }

    function _validateRoute(Route calldata route) internal view {
        if (route.asset == address(0) || route.amount == 0 || route.legs.length < 2) revert InvalidRoute();
        if (!tokenAllowed[route.asset]) revert Unauthorized();
        if (route.legs[0].tokenIn != route.asset || route.legs[route.legs.length - 1].tokenOut != route.asset) revert InvalidRoute();
        for (uint256 i; i < route.legs.length; ++i) _validateLeg(route.legs[i].adapter, route.legs[i].tokenIn, route.legs[i].tokenOut);
        for (uint256 i; i + 1 < route.legs.length; ++i) if (route.legs[i].tokenOut != route.legs[i + 1].tokenIn) revert InvalidRoute();
    }

    function _validateRouteMemory(Route memory route) internal view {
        if (route.asset == address(0) || route.amount == 0 || route.legs.length < 2) revert InvalidRoute();
        if (!tokenAllowed[route.asset]) revert Unauthorized();
        if (route.legs[0].tokenIn != route.asset || route.legs[route.legs.length - 1].tokenOut != route.asset) revert InvalidRoute();
        for (uint256 i; i < route.legs.length; ++i) _validateLeg(route.legs[i].adapter, route.legs[i].tokenIn, route.legs[i].tokenOut);
        for (uint256 i; i + 1 < route.legs.length; ++i) if (route.legs[i].tokenOut != route.legs[i + 1].tokenIn) revert InvalidRoute();
    }

    function _validatePacked(PackedHeader memory header, PackedCandidate[] memory candidates) internal view {
        if (header.asset == address(0) || header.amount == 0 || candidates.length == 0 || candidates.length > MAX_PACKED_CANDIDATES) revert InvalidPackedBatch();
        if (!tokenAllowed[header.asset]) revert Unauthorized();
        for (uint256 c; c < candidates.length; ++c) {
            PackedCandidate memory candidate = candidates[c];
            if (candidate.candidateId == bytes32(0) || candidate.legs.length < 2 || candidate.legs.length > MAX_PACKED_LEGS) revert InvalidRoute();
            if (candidate.legs[0].tokenIn != header.asset || candidate.legs[candidate.legs.length - 1].tokenOut != header.asset) revert InvalidRoute();
            for (uint256 i; i < candidate.legs.length; ++i) {
                Leg memory leg = candidate.legs[i];
                _validateLeg(leg.adapter, leg.tokenIn, leg.tokenOut);
                if (i + 1 < candidate.legs.length && leg.tokenOut != candidate.legs[i + 1].tokenIn) revert InvalidRoute();
            }
        }
    }

    function _validateLeg(address adapter, address tokenIn, address tokenOut) internal view {
        if (!adapterAllowed[adapter] || !tokenAllowed[tokenIn] || !tokenAllowed[tokenOut]) revert Unauthorized();
        if (tokenIn == tokenOut) revert InvalidRoute();
    }

    function _requireCleanIntermediates(Route calldata route) internal view {
        for (uint256 i; i + 1 < route.legs.length; ++i) {
            address token = route.legs[i].tokenOut;
            if (token == route.asset) continue;
            uint256 balance = IERC20(token).balanceOf(address(this));
            if (balance != 0) revert DirtyIntermediateBalance(token, balance);
        }
    }

    function _requireCleanPackedIntermediates(address asset, PackedCandidate[] memory candidates) internal view {
        for (uint256 c; c < candidates.length; ++c) {
            for (uint256 i; i + 1 < candidates[c].legs.length; ++i) {
                address token = candidates[c].legs[i].tokenOut;
                if (token == asset) continue;
                uint256 balance = IERC20(token).balanceOf(address(this));
                if (balance != 0) revert DirtyIntermediateBalance(token, balance);
            }
        }
    }

    function _decodePacked(bytes calldata p) internal pure returns (PackedHeader memory header, PackedCandidate[] memory candidates) {
        uint256 o;
        uint8 version;
        (version, o) = _readU8(p, o);
        if (version != PACKED_VERSION) revert InvalidPackedBatch();
        (header.candidateCount, o) = _readU8(p, o);
        if (header.candidateCount == 0 || header.candidateCount > MAX_PACKED_CANDIDATES) revert InvalidPackedBatch();
        (header.asset, o) = _readAddress(p, o);
        (header.amount, o) = _readU256(p, o);
        (header.targetBlock, o) = _readU64(p, o);
        (header.deadline, o) = _readU64(p, o);

        candidates = new PackedCandidate[](header.candidateCount);
        for (uint256 c; c < header.candidateCount; ++c) {
            (candidates[c].candidateId, o) = _readBytes32(p, o);
            (candidates[c].minProfit, o) = _readU256(p, o);
            uint8 legCount;
            (legCount, o) = _readU8(p, o);
            if (legCount < 2 || legCount > MAX_PACKED_LEGS) revert InvalidPackedBatch();
            candidates[c].legs = new Leg[](legCount);
            for (uint256 i; i < legCount; ++i) {
                (candidates[c].legs[i].adapter, o) = _readAddress(p, o);
                (candidates[c].legs[i].tokenIn, o) = _readAddress(p, o);
                (candidates[c].legs[i].tokenOut, o) = _readAddress(p, o);
                (candidates[c].legs[i].minOut, o) = _readU256(p, o);
                uint16 dataLen;
                (dataLen, o) = _readU16(p, o);
                if (dataLen > MAX_PACKED_LEG_DATA || o + dataLen > p.length) revert InvalidPackedBatch();
                candidates[c].legs[i].data = p[o:o + dataLen];
                o += dataLen;
            }
        }
        if (o != p.length) revert InvalidPackedBatch();
    }

    function _readU8(bytes calldata p, uint256 o) private pure returns (uint8 v, uint256 n) {
        if (o + 1 > p.length) revert InvalidPackedBatch();
        v = uint8(p[o]);
        n = o + 1;
    }

    function _readU16(bytes calldata p, uint256 o) private pure returns (uint16 v, uint256 n) {
        if (o + 2 > p.length) revert InvalidPackedBatch();
        v = (uint16(uint8(p[o])) << 8) | uint16(uint8(p[o + 1]));
        n = o + 2;
    }

    function _readU64(bytes calldata p, uint256 o) private pure returns (uint64 v, uint256 n) {
        if (o + 8 > p.length) revert InvalidPackedBatch();
        for (uint256 i; i < 8; ++i) v = (v << 8) | uint64(uint8(p[o + i]));
        n = o + 8;
    }

    function _readU256(bytes calldata p, uint256 o) private pure returns (uint256 v, uint256 n) {
        if (o + 32 > p.length) revert InvalidPackedBatch();
        assembly { v := calldataload(add(p.offset, o)) }
        n = o + 32;
    }

    function _readBytes32(bytes calldata p, uint256 o) private pure returns (bytes32 v, uint256 n) {
        if (o + 32 > p.length) revert InvalidPackedBatch();
        assembly { v := calldataload(add(p.offset, o)) }
        n = o + 32;
    }

    function _readAddress(bytes calldata p, uint256 o) private pure returns (address v, uint256 n) {
        if (o + 20 > p.length) revert InvalidPackedBatch();
        assembly { v := shr(96, calldataload(add(p.offset, o))) }
        n = o + 20;
    }

    function _hash(Route calldata r) internal pure returns (bytes32) {
        bytes32 legsHash;
        for (uint256 i; i < r.legs.length; ++i) {
            Leg calldata leg = r.legs[i];
            legsHash = keccak256(abi.encode(legsHash, leg.adapter, leg.tokenIn, leg.tokenOut, leg.minOut, keccak256(leg.data)));
        }
        return keccak256(abi.encode(r.asset, r.amount, r.minProfit, r.targetBlock, r.deadline, legsHash));
    }

    function _hashMemory(Route memory r) internal pure returns (bytes32) {
        bytes32 legsHash;
        for (uint256 i; i < r.legs.length; ++i) {
            Leg memory leg = r.legs[i];
            legsHash = keccak256(abi.encode(legsHash, leg.adapter, leg.tokenIn, leg.tokenOut, leg.minOut, keccak256(leg.data)));
        }
        return keccak256(abi.encode(r.asset, r.amount, r.minProfit, r.targetBlock, r.deadline, legsHash));
    }

    function _forceApprove(address token, address spender, uint256 amount) internal {
        (bool ok, bytes memory ret) = token.call(abi.encodeCall(IERC20.approve, (spender, 0)));
        if (!ok || (ret.length != 0 && !abi.decode(ret, (bool)))) revert TokenCallFailed(token);
        if (amount != 0) {
            (ok, ret) = token.call(abi.encodeCall(IERC20.approve, (spender, amount)));
            if (!ok || (ret.length != 0 && !abi.decode(ret, (bool)))) revert TokenCallFailed(token);
        }
    }

    function _safeTransfer(address token, address to, uint256 amount) internal {
        (bool ok, bytes memory ret) = token.call(abi.encodeCall(IERC20.transfer, (to, amount)));
        if (!ok || (ret.length != 0 && !abi.decode(ret, (bool)))) revert TokenCallFailed(token);
    }
}
