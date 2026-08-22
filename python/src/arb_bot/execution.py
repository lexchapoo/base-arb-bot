from __future__ import annotations

from dataclasses import dataclass, asdict
from typing import Any

import httpx

from .adapters.base import Quote
from .config import settings
from .event_graph import Cycle, LivePoolGraph, PoolMetadata


EXECUTOR_ABI = [
    {
        "inputs": [
            {
                "components": [
                    {"name": "routeHash", "type": "bytes32"},
                    {"name": "asset", "type": "address"},
                    {"name": "amount", "type": "uint256"},
                    {"name": "minProfit", "type": "uint256"},
                    {"name": "targetBlock", "type": "uint64"},
                    {"name": "deadline", "type": "uint64"},
                    {
                        "components": [
                            {"name": "adapter", "type": "address"},
                            {"name": "tokenIn", "type": "address"},
                            {"name": "tokenOut", "type": "address"},
                            {"name": "minOut", "type": "uint256"},
                            {"name": "data", "type": "bytes"},
                        ],
                        "name": "legs",
                        "type": "tuple[]",
                    },
                ],
                "name": "route",
                "type": "tuple",
            }
        ],
        "name": "start",
        "outputs": [],
        "stateMutability": "nonpayable",
        "type": "function",
    },
    {
        "inputs": [{"name": "packed", "type": "bytes"}],
        "name": "startPacked",
        "outputs": [],
        "stateMutability": "nonpayable",
        "type": "function",
    },
]

AAVE_POOL_ABI = [
    {
        "inputs": [],
        "name": "FLASHLOAN_PREMIUM_TOTAL",
        "outputs": [{"name": "", "type": "uint128"}],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [{"name": "asset", "type": "address"}],
        "name": "getReserveData",
        "outputs": [
            {
                "components": [
                    {"components": [{"name": "data", "type": "uint256"}], "name": "configuration", "type": "tuple"},
                    {"name": "liquidityIndex", "type": "uint128"},
                    {"name": "currentLiquidityRate", "type": "uint128"},
                    {"name": "variableBorrowIndex", "type": "uint128"},
                    {"name": "currentVariableBorrowRate", "type": "uint128"},
                    {"name": "__deprecatedStableBorrowRate", "type": "uint128"},
                    {"name": "lastUpdateTimestamp", "type": "uint40"},
                    {"name": "id", "type": "uint16"},
                    {"name": "liquidationGracePeriodUntil", "type": "uint40"},
                    {"name": "aTokenAddress", "type": "address"},
                    {"name": "__deprecatedStableDebtTokenAddress", "type": "address"},
                    {"name": "variableDebtTokenAddress", "type": "address"},
                    {"name": "interestRateStrategyAddress", "type": "address"},
                    {"name": "accruedToTreasury", "type": "uint128"},
                    {"name": "unbacked", "type": "uint128"},
                    {"name": "isolationModeTotalDebt", "type": "uint128"},
                    {"name": "virtualUnderlyingBalance", "type": "uint128"},
                ],
                "name": "",
                "type": "tuple",
            }
        ],
        "stateMutability": "view",
        "type": "function",
    },
]

ERC20_ABI = [
    {
        "inputs": [{"name": "account", "type": "address"}],
        "name": "balanceOf",
        "outputs": [{"name": "", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function",
    }
]

GAS_ORACLE_ABI = [
    {
        "inputs": [{"name": "txSize", "type": "uint256"}],
        "name": "getL1FeeUpperBound",
        "outputs": [{"name": "", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function",
    }
]


@dataclass(frozen=True, slots=True)
class AaveSnapshot:
    premium_bps: int
    available_liquidity: int
    flashloan_enabled: bool
    a_token: str


@dataclass(frozen=True, slots=True)
class SimulationResult:
    success: bool
    gas_used: int | None
    block_number: int | None
    base_fee_per_gas: int | None
    error: str | None
    raw: dict[str, Any]


@dataclass(frozen=True, slots=True)
class ExecutionFinalization:
    calldata: str | None
    route_hash: str | None
    target_block: int | None
    deadline: int | None
    simulation_gas_units: int | None
    estimate_gas_units: int | None
    gas_price_wei: int | None
    l2_fee_wei: int | None
    l1_fee_upper_bound_wei: int | None
    total_native_fee_wei: int | None
    gas_cost_asset_units: int | None
    flashloan_premium_bps: int | None
    flashloan_premium_units: int | None
    available_flash_liquidity: int | None
    deterministic_net_profit_units: int | None
    simulation_success: bool
    submission_eligible: bool
    blockers: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["blockers"] = list(self.blockers)
        return data


class BaseRpcClient:
    def __init__(self, url: str) -> None:
        self.url = url
        self.client = httpx.AsyncClient(timeout=settings.execution_rpc_timeout_seconds)
        self._id = 0

    async def close(self) -> None:
        await self.client.aclose()

    async def call(self, method: str, params: list[Any]) -> Any:
        self._id += 1
        response = await self.client.post(
            self.url,
            json={"jsonrpc": "2.0", "id": self._id, "method": method, "params": params},
        )
        response.raise_for_status()
        body = response.json()
        if body.get("error"):
            raise RuntimeError(f"{method}: {body['error']}")
        if "result" not in body:
            raise RuntimeError(f"{method}: missing result")
        return body["result"]

    async def chain_id(self) -> int:
        return int(await self.call("eth_chainId", []), 16)

    async def block_number(self) -> int:
        return int(await self.call("eth_blockNumber", []), 16)

    async def nonce(self, address: str) -> int:
        return int(await self.call("eth_getTransactionCount", [address, "pending"]), 16)

    async def gas_price(self) -> int:
        return int(await self.call("eth_gasPrice", []), 16)

    async def priority_fee(self) -> int:
        return int(await self.call("eth_maxPriorityFeePerGas", []), 16)

    async def pending_base_fee(self) -> int:
        block = await self.call("eth_getBlockByNumber", ["pending", False])
        value = block.get("baseFeePerGas")
        if value is None:
            raise RuntimeError("pending block did not include baseFeePerGas")
        return int(value, 16)

    async def estimate_gas(self, tx: dict[str, str]) -> int:
        return int(await self.call("eth_estimateGas", [tx, "pending"]), 16)

    async def simulate(self, tx: dict[str, str]) -> SimulationResult:
        result = await self.call(
            "eth_simulateV1",
            [
                {
                    "blockStateCalls": [{"calls": [tx], "stateOverrides": {}}],
                    "traceTransfers": False,
                    "validation": False,
                },
                "pending",
            ],
        )
        if not result:
            return SimulationResult(False, None, None, None, "empty eth_simulateV1 result", {})
        block = result[0]
        calls = block.get("calls") or []
        if not calls:
            return SimulationResult(False, None, _hex_int(block.get("number")), _hex_int(block.get("baseFeePerGas")), "simulation returned no calls", block)
        call = calls[0]
        success = call.get("status") == "0x1"
        return SimulationResult(
            success=success,
            gas_used=_hex_int(call.get("gasUsed")) or _hex_int(block.get("gasUsed")),
            block_number=_hex_int(block.get("number")),
            base_fee_per_gas=_hex_int(block.get("baseFeePerGas")),
            error=call.get("error") if not success else None,
            raw=block,
        )


def _hex_int(value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        return int(value, 16)
    return None


def _int_bytes(value: int) -> bytes:
    if value == 0:
        return b""
    return value.to_bytes((value.bit_length() + 7) // 8, "big")


def _rlp(item: Any) -> bytes:
    if isinstance(item, int):
        return _rlp(_int_bytes(item))
    if isinstance(item, str):
        if item.startswith("0x"):
            return _rlp(bytes.fromhex(item[2:]))
        return _rlp(item.encode())
    if isinstance(item, bytes):
        if len(item) == 1 and item[0] < 0x80:
            return item
        if len(item) <= 55:
            return bytes([0x80 + len(item)]) + item
        size = _int_bytes(len(item))
        return bytes([0xB7 + len(size)]) + size + item
    if isinstance(item, (list, tuple)):
        payload = b"".join(_rlp(x) for x in item)
        if len(payload) <= 55:
            return bytes([0xC0 + len(payload)]) + payload
        size = _int_bytes(len(payload))
        return bytes([0xF7 + len(size)]) + size + payload
    raise TypeError(f"unsupported RLP type: {type(item)!r}")


def conservative_eip1559_signed_size(
    chain_id: int,
    nonce: int,
    max_priority_fee: int,
    max_fee: int,
    gas_limit: int,
    to: str,
    data: str,
) -> int:
    """Serialized EIP-1559 size using maximum-width signature scalars.

    The signature values are structural upper bounds, not fabricated market inputs.
    """
    fields = [
        chain_id,
        nonce,
        max_priority_fee,
        max_fee,
        gas_limit,
        bytes.fromhex(to[2:]),
        0,
        bytes.fromhex(data[2:]),
        [],
        1,
        (1 << 256) - 1,
        (1 << 256) - 1,
    ]
    return 1 + len(_rlp(fields))


class ContractRuntime:
    """Lazy Web3-backed ABI encoding and protocol inspection."""

    def __init__(self, rpc_url: str) -> None:
        try:
            from web3 import AsyncWeb3, Web3
            from web3.providers import AsyncHTTPProvider
            from eth_abi import encode
        except ImportError as exc:  # pragma: no cover - exercised only when runtime dependency is missing
            raise RuntimeError("web3/eth-abi runtime dependencies are required") from exc
        self.AsyncWeb3 = AsyncWeb3
        self.Web3 = Web3
        self.encode = encode
        self.w3 = AsyncWeb3(AsyncHTTPProvider(rpc_url))

    def checksum(self, address: str) -> str:
        return self.Web3.to_checksum_address(address)

    async def verify_contract(self, address: str) -> None:
        code = await self.w3.eth.get_code(self.checksum(address), block_identifier="pending")
        if not code:
            raise RuntimeError(f"no contract bytecode at {address}")

    async def aave_snapshot(self, pool: str, asset: str) -> AaveSnapshot:
        p = self.w3.eth.contract(address=self.checksum(pool), abi=AAVE_POOL_ABI)
        premium = int(await p.functions.FLASHLOAN_PREMIUM_TOTAL().call(block_identifier="pending"))
        reserve = await p.functions.getReserveData(self.checksum(asset)).call(block_identifier="pending")
        configuration = reserve[0][0] if isinstance(reserve[0], (tuple, list)) else reserve[0]
        flashloan_enabled = bool((int(configuration) >> 63) & 1)
        a_token = self.checksum(reserve[9])
        token = self.w3.eth.contract(address=self.checksum(asset), abi=ERC20_ABI)
        available = int(await token.functions.balanceOf(a_token).call(block_identifier="pending"))
        return AaveSnapshot(premium, available, flashloan_enabled, a_token)

    async def l1_fee_upper_bound(self, oracle: str, tx_size: int) -> int:
        contract = self.w3.eth.contract(address=self.checksum(oracle), abi=GAS_ORACLE_ABI)
        return int(await contract.functions.getL1FeeUpperBound(tx_size).call(block_identifier="pending"))

    def adapter_data(self, pool: PoolMetadata) -> bytes:
        venue = pool.venue.lower()
        if "uniswap" in venue:
            if pool.fee is None:
                raise RuntimeError(f"missing fee for {pool.address}")
            return self.encode(["uint24", "uint160"], [pool.fee, 0])
        if "slipstream" in venue:
            # Slipstream's pool key is tickSpacing (int24), not fee.
            if pool.tick_spacing is None:
                raise RuntimeError(f"missing tick spacing for {pool.address}")
            return self.encode(["int24", "uint160"], [int(pool.tick_spacing), 0])
        if "aerodrome" in venue and "slipstream" not in venue:
            if pool.stable is None:
                raise RuntimeError(f"missing stable flag for {pool.address}")
            return self.encode(["bool"], [pool.stable])
        raise RuntimeError(f"no execution encoding for venue={pool.venue}")

    def route_hash(
        self,
        *,
        asset: str,
        amount: int,
        min_profit: int,
        target_block: int,
        deadline: int,
        legs: list[dict[str, Any]],
    ) -> bytes:
        zero = bytes(32)
        legs_hash = zero
        for leg in legs:
            data_hash = self.Web3.keccak(leg["data"])
            legs_hash = self.Web3.keccak(
                self.encode(
                    ["bytes32", "address", "address", "address", "uint256", "bytes32"],
                    [
                        legs_hash,
                        self.checksum(leg["adapter"]),
                        self.checksum(leg["tokenIn"]),
                        self.checksum(leg["tokenOut"]),
                        int(leg["minOut"]),
                        data_hash,
                    ],
                )
            )
        return self.Web3.keccak(
            self.encode(
                ["address", "uint256", "uint256", "uint64", "uint64", "bytes32"],
                [self.checksum(asset), amount, min_profit, target_block, deadline, legs_hash],
            )
        )

    def encode_start(
        self,
        *,
        executor: str,
        asset: str,
        amount: int,
        min_profit: int,
        target_block: int,
        deadline: int,
        legs: list[dict[str, Any]],
    ) -> tuple[str, str]:
        route_hash = self.route_hash(
            asset=asset,
            amount=amount,
            min_profit=min_profit,
            target_block=target_block,
            deadline=deadline,
            legs=legs,
        )
        route = {
            "routeHash": route_hash,
            "asset": self.checksum(asset),
            "amount": amount,
            "minProfit": min_profit,
            "targetBlock": target_block,
            "deadline": deadline,
            "legs": [
                {
                    "adapter": self.checksum(x["adapter"]),
                    "tokenIn": self.checksum(x["tokenIn"]),
                    "tokenOut": self.checksum(x["tokenOut"]),
                    "minOut": int(x["minOut"]),
                    "data": x["data"],
                }
                for x in legs
            ],
        }
        contract = self.w3.eth.contract(address=self.checksum(executor), abi=EXECUTOR_ABI)
        calldata = contract.encode_abi("start", args=[route])
        route_hash_hex = route_hash.hex()
        if not route_hash_hex.startswith("0x"):
            route_hash_hex = "0x" + route_hash_hex
        return calldata, route_hash_hex


    def encode_start_packed(self, *, executor: str, packed: bytes) -> tuple[str, str]:
        if not packed:
            raise ValueError("packed batch cannot be empty")
        contract = self.w3.eth.contract(address=self.checksum(executor), abi=EXECUTOR_ABI)
        calldata = contract.encode_abi("startPacked", args=[packed])
        # Ethereum uses Keccak-256; Web3.keccak matches Solidity keccak256.
        batch_hash = self.Web3.keccak(packed).hex()
        if not batch_hash.startswith("0x"):
            batch_hash = "0x" + batch_hash
        return calldata, batch_hash


class NativeFeeConverter:
    def __init__(self, graph: LivePoolGraph, registry: Any) -> None:
        self.graph = graph
        self.registry = registry

    async def convert(self, asset: str, native_wei: int) -> int:
        if native_wei == 0:
            return 0
        weth = settings.base_weth.strip().lower()
        if not weth:
            raise RuntimeError("BASE_WETH is required for gas-cost conversion")
        asset = asset.lower()
        if asset == weth:
            return native_wei
        candidates: list[int] = []
        for pool in self.graph.pools_for_token(weth):
            other = pool.token1 if pool.token0 == weth else pool.token0
            if other != asset:
                continue
            adapter = self.registry.get(pool.venue)
            if adapter is None:
                continue
            kwargs: dict[str, Any] = {"block_identifier": "pending"}
            canonical = self.registry.canonical_venue(pool.venue)
            if canonical == "uniswap-v3":
                if pool.fee is None:
                    continue
                kwargs["fee"] = pool.fee
            elif canonical == "aerodrome":
                if pool.stable is None:
                    continue
                kwargs["stable"] = pool.stable
            else:
                continue
            quote = await adapter.quote_exact_input(weth, asset, native_wei, **kwargs)
            candidates.append(int(quote.amount_out))
        if not candidates:
            raise RuntimeError(f"no configured live WETH/{asset} gas-conversion pool")
        return max(candidates)


class ExecutionFinalizer:
    def __init__(self, graph: LivePoolGraph, registry: Any) -> None:
        self.graph = graph
        self.registry = registry
        self.rpc = BaseRpcClient(settings.base_http_rpc)
        self.contracts: ContractRuntime | None = None
        self.converter = NativeFeeConverter(graph, registry)

    def _runtime(self) -> ContractRuntime:
        if self.contracts is None:
            self.contracts = ContractRuntime(settings.base_http_rpc)
        return self.contracts

    def configured(self) -> tuple[bool, list[str]]:
        required = {
            "EXECUTOR_ADDRESS": settings.executor_address,
            "EXECUTOR_OWNER_ADDRESS": settings.executor_owner_address,
            "AAVE_POOL": settings.aave_pool,
            "BASE_WETH": settings.base_weth,
        }
        missing = [f"missing_config:{k}" for k, v in required.items() if not str(v).strip()]
        return not missing, missing

    def packed_candidate_dict(
        self,
        cycle: Cycle,
        amount_in: int,
        amount_out: int,
        quotes: list[Quote],
        final: ExecutionFinalization,
    ) -> dict[str, Any] | None:
        """Return the exact candidate fields used to construct startPacked calldata."""
        if (
            final.target_block is None
            or final.deadline is None
            or final.flashloan_premium_units is None
            or final.deterministic_net_profit_units is None
        ):
            return None
        runtime = self._runtime()
        legs: list[dict[str, Any]] = []
        for index, pool in enumerate(cycle.pools):
            adapter = self._adapter_for(pool)
            if not adapter:
                return None
            legs.append({
                "adapter": adapter,
                "token_in": cycle.tokens[index],
                "token_out": cycle.tokens[index + 1],
                "min_out": int(quotes[index].amount_out),
                "data": "0x" + runtime.adapter_data(pool).hex(),
            })
        min_profit = amount_out - amount_in - final.flashloan_premium_units
        if min_profit <= 0:
            return None
        return {
            "asset": cycle.tokens[0],
            "amount": amount_in,
            "target_block": final.target_block,
            "deadline": final.deadline,
            "min_profit": min_profit,
            "legs": legs,
        }

    def _adapter_for(self, pool: PoolMetadata) -> str:
        canonical = self.registry.canonical_venue(pool.venue)
        if canonical == "uniswap-v3":
            return settings.uniswap_v3_adapter_address
        if canonical == "aerodrome-slipstream":
            return settings.aerodrome_slipstream_adapter_address
        if canonical == "aerodrome":
            return settings.aerodrome_adapter_address
        raise RuntimeError(f"no execution adapter configured for venue={pool.venue}")

    async def finalize(
        self,
        cycle: Cycle,
        amount_in: int,
        amount_out: int,
        quotes: list[Quote],
        observed_block: int | None,
    ) -> ExecutionFinalization:
        blockers: list[str] = []
        ok, missing = self.configured()
        if not ok:
            return _empty_finalization(tuple(missing))
        runtime = self._runtime()
        try:
            chain_id = await self.rpc.chain_id()
        except Exception as exc:
            return _empty_finalization((f"chain_id_query_failed:{type(exc).__name__}",))
        if chain_id != settings.chain_id:
            return _empty_finalization((f"wrong_chain:{chain_id}",))
        adapter_addresses: list[str] = []
        try:
            for pool in cycle.pools:
                adapter = self._adapter_for(pool)
                if not adapter:
                    blockers.append(f"missing_execution_adapter:{self.registry.canonical_venue(pool.venue)}")
                adapter_addresses.append(adapter)
            if blockers:
                return _empty_finalization(tuple(sorted(set(blockers))))

            await runtime.verify_contract(settings.executor_address)
            await runtime.verify_contract(settings.aave_pool)
            await runtime.verify_contract(settings.gas_price_oracle)
            for address in set(adapter_addresses):
                await runtime.verify_contract(address)
        except Exception as exc:
            return _empty_finalization((f"contract_verification_failed:{type(exc).__name__}",))

        try:
            aave = await runtime.aave_snapshot(settings.aave_pool, cycle.tokens[0])
        except Exception as exc:
            return _empty_finalization((f"aave_snapshot_failed:{type(exc).__name__}",))
        if not aave.flashloan_enabled:
            blockers.append("aave_flashloan_disabled_for_asset")
        if amount_in > aave.available_liquidity:
            blockers.append("insufficient_aave_flash_liquidity")
        if blockers:
            return ExecutionFinalization(
                calldata=None, route_hash=None, target_block=None, deadline=None,
                simulation_gas_units=None, estimate_gas_units=None, gas_price_wei=None,
                l2_fee_wei=None, l1_fee_upper_bound_wei=None, total_native_fee_wei=None,
                gas_cost_asset_units=None, flashloan_premium_bps=aave.premium_bps,
                flashloan_premium_units=None, available_flash_liquidity=aave.available_liquidity,
                deterministic_net_profit_units=None, simulation_success=False,
                submission_eligible=False, blockers=tuple(blockers),
            )

        premium_units = (amount_in * aave.premium_bps + 5_000) // 10_000
        pre_gas_profit = amount_out - amount_in - premium_units
        if pre_gas_profit <= 0:
            blockers.append("non_positive_profit_after_flashloan_premium")
            return ExecutionFinalization(
                calldata=None, route_hash=None, target_block=None, deadline=None,
                simulation_gas_units=None, estimate_gas_units=None, gas_price_wei=None,
                l2_fee_wei=None, l1_fee_upper_bound_wei=None, total_native_fee_wei=None,
                gas_cost_asset_units=None, flashloan_premium_bps=aave.premium_bps,
                flashloan_premium_units=premium_units, available_flash_liquidity=aave.available_liquidity,
                deterministic_net_profit_units=pre_gas_profit, simulation_success=False,
                submission_eligible=False, blockers=tuple(blockers),
            )

        try:
            latest = await self.rpc.block_number()
            target_block = max(observed_block or (latest + 1), latest + 1)
            deadline = (1 << 64) - 1  # freshness is enforced by the exact target block
            legs: list[dict[str, Any]] = []
            for index, pool in enumerate(cycle.pools):
                legs.append(
                    {
                        "adapter": adapter_addresses[index],
                        "tokenIn": cycle.tokens[index],
                        "tokenOut": cycle.tokens[index + 1],
                        # Exact pending quote: no fabricated slippage tolerance.
                        "minOut": int(quotes[index].amount_out),
                        "data": runtime.adapter_data(pool),
                    }
                )

            calldata, route_hash = runtime.encode_start(
                executor=settings.executor_address,
                asset=cycle.tokens[0],
                amount=amount_in,
                min_profit=pre_gas_profit,
                target_block=target_block,
                deadline=deadline,
                legs=legs,
            )
            tx = {
                "from": runtime.checksum(settings.executor_owner_address),
                "to": runtime.checksum(settings.executor_address),
                "data": calldata,
                "value": "0x0",
            }
            simulation = await self.rpc.simulate(tx)
            if not simulation.success:
                blockers.append(f"executor_simulation_failed:{simulation.error or 'unknown'}")
                return ExecutionFinalization(
                    calldata=calldata, route_hash=route_hash, target_block=target_block, deadline=deadline,
                    simulation_gas_units=simulation.gas_used, estimate_gas_units=None, gas_price_wei=None,
                    l2_fee_wei=None, l1_fee_upper_bound_wei=None, total_native_fee_wei=None,
                    gas_cost_asset_units=None, flashloan_premium_bps=aave.premium_bps,
                    flashloan_premium_units=premium_units, available_flash_liquidity=aave.available_liquidity,
                    deterministic_net_profit_units=None, simulation_success=False,
                    submission_eligible=False, blockers=tuple(blockers),
                )

            try:
                estimate_gas = await self.rpc.estimate_gas(tx)
            except Exception:
                estimate_gas = None
            gas_used = max(x for x in (simulation.gas_used, estimate_gas) if x is not None)
            gas_price = await self.rpc.gas_price()
            l2_fee = gas_used * gas_price

            nonce = await self.rpc.nonce(settings.executor_owner_address)
            priority_fee = await self.rpc.priority_fee()
            pending_base_fee = await self.rpc.pending_base_fee()
            max_fee = pending_base_fee + priority_fee
            tx_size = conservative_eip1559_signed_size(
                settings.chain_id,
                nonce,
                priority_fee,
                max_fee,
                gas_used,
                settings.executor_address,
                calldata,
            )
            l1_fee = await runtime.l1_fee_upper_bound(settings.gas_price_oracle, tx_size)
            total_native_fee = l2_fee + l1_fee
            gas_asset_units = await self.converter.convert(cycle.tokens[0], total_native_fee)
            deterministic_net = pre_gas_profit - gas_asset_units
            if deterministic_net <= 0:
                blockers.append("non_positive_profit_after_full_transaction_cost")

            return ExecutionFinalization(
                calldata=calldata,
                route_hash=route_hash,
                target_block=target_block,
                deadline=deadline,
                simulation_gas_units=simulation.gas_used,
                estimate_gas_units=estimate_gas,
                gas_price_wei=gas_price,
                l2_fee_wei=l2_fee,
                l1_fee_upper_bound_wei=l1_fee,
                total_native_fee_wei=total_native_fee,
                gas_cost_asset_units=gas_asset_units,
                flashloan_premium_bps=aave.premium_bps,
                flashloan_premium_units=premium_units,
                available_flash_liquidity=aave.available_liquidity,
                deterministic_net_profit_units=deterministic_net,
                simulation_success=True,
                submission_eligible=deterministic_net > 0,
                blockers=tuple(sorted(set(blockers))),
            )
        except Exception as exc:
            blockers.append(f"execution_finalization_failed:{type(exc).__name__}")
            return ExecutionFinalization(
                calldata=None, route_hash=None, target_block=None, deadline=None,
                simulation_gas_units=None, estimate_gas_units=None, gas_price_wei=None,
                l2_fee_wei=None, l1_fee_upper_bound_wei=None, total_native_fee_wei=None,
                gas_cost_asset_units=None, flashloan_premium_bps=aave.premium_bps,
                flashloan_premium_units=premium_units, available_flash_liquidity=aave.available_liquidity,
                deterministic_net_profit_units=None, simulation_success=False,
                submission_eligible=False, blockers=tuple(sorted(set(blockers))),
            )


def _empty_finalization(blockers: tuple[str, ...]) -> ExecutionFinalization:
    return ExecutionFinalization(
        calldata=None,
        route_hash=None,
        target_block=None,
        deadline=None,
        simulation_gas_units=None,
        estimate_gas_units=None,
        gas_price_wei=None,
        l2_fee_wei=None,
        l1_fee_upper_bound_wei=None,
        total_native_fee_wei=None,
        gas_cost_asset_units=None,
        flashloan_premium_bps=None,
        flashloan_premium_units=None,
        available_flash_liquidity=None,
        deterministic_net_profit_units=None,
        simulation_success=False,
        submission_eligible=False,
        blockers=blockers,
    )
