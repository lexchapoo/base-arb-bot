from __future__ import annotations

import asyncio
import hashlib
from dataclasses import asdict, dataclass

from .adapters.base import Quote, QuoteAdapter
from .config import settings
from .event_graph import Cycle, LivePoolGraph, PoolMetadata
from .execution import ExecutionFinalizer

ERC20_BALANCE_ABI = [{
    "inputs": [{"name": "account", "type": "address"}],
    "name": "balanceOf",
    "outputs": [{"name": "", "type": "uint256"}],
    "stateMutability": "view",
    "type": "function",
}]


@dataclass(frozen=True, slots=True)
class RouteEvaluation:
    route_id: str
    pools: tuple[str, ...]
    tokens: tuple[str, ...]
    amount_in: int | None
    amount_out: int | None
    gross_profit_units: int | None
    quote_block_numbers: tuple[int | None, ...]
    quote_gas_units: int | None
    ev_ready: bool
    submission_eligible: bool
    blockers: tuple[str, ...]
    quote_metadata: tuple[dict, ...]
    execution: dict

    def to_dict(self) -> dict:
        data = asdict(self)
        data["pools"] = list(self.pools)
        data["tokens"] = list(self.tokens)
        data["quote_block_numbers"] = list(self.quote_block_numbers)
        data["blockers"] = list(self.blockers)
        data["quote_metadata"] = list(self.quote_metadata)
        return data


class AdapterRegistry:
    """Create only adapters that have explicit production configuration."""

    def __init__(self) -> None:
        self._adapters: dict[str, QuoteAdapter] = {}
        if settings.uniswap_v3_quoter_v2:
            from .adapters.uniswap_v3 import UniswapV3Quoter
            self._adapters["uniswap-v3"] = UniswapV3Quoter(
                settings.base_http_rpc, settings.uniswap_v3_quoter_v2, "uniswap-v3"
            )
        if settings.aerodrome_slipstream_quoter:
            from .adapters.slipstream import SlipstreamQuoter
            self._adapters["aerodrome-slipstream"] = SlipstreamQuoter(
                settings.base_http_rpc, settings.aerodrome_slipstream_quoter, "aerodrome-slipstream"
            )
        if settings.aerodrome_router and settings.aerodrome_factory:
            from .adapters.aerodrome import AerodromeRouter
            self._adapters["aerodrome"] = AerodromeRouter(
                settings.base_http_rpc,
                settings.aerodrome_router,
                settings.aerodrome_factory,
                "aerodrome",
            )

    @staticmethod
    def canonical_venue(venue: str) -> str:
        v = venue.strip().lower()
        if "slipstream" in v:
            return "aerodrome-slipstream"
        if "uniswap" in v:
            return "uniswap-v3"
        if "aerodrome" in v:
            return "aerodrome"
        return v

    def get(self, venue: str) -> QuoteAdapter | None:
        return self._adapters.get(self.canonical_venue(venue))


class LiveBalanceProvider:
    def __init__(self, rpc_url: str) -> None:
        from web3 import AsyncWeb3
        from web3.providers import AsyncHTTPProvider
        self._async_web3 = AsyncWeb3
        self.w3 = AsyncWeb3(AsyncHTTPProvider(rpc_url))

    async def token_balance(self, token: str, account: str, block_identifier: str = "pending") -> int:
        contract = self.w3.eth.contract(
            address=self._async_web3.to_checksum_address(token), abi=ERC20_BALANCE_ABI
        )
        return int(
            await contract.functions.balanceOf(
                self._async_web3.to_checksum_address(account)
            ).call(block_identifier=block_identifier)
        )


class RouteOptimizer:
    def __init__(
        self,
        graph: LivePoolGraph,
        registry: AdapterRegistry | None = None,
        balance_provider: LiveBalanceProvider | None = None,
        max_quote_evaluations: int | None = None,
        execution_finalizer: ExecutionFinalizer | None = None,
        enable_execution_finalizer: bool = True,
    ) -> None:
        self.graph = graph
        self.registry = registry or AdapterRegistry()
        self.balance_provider = balance_provider
        self.max_quote_evaluations = max_quote_evaluations or settings.max_quote_evaluations
        self.execution_finalizer = execution_finalizer
        if self.execution_finalizer is None and enable_execution_finalizer:
            self.execution_finalizer = ExecutionFinalizer(graph, self.registry)

    @staticmethod
    def _route_id(cycle: Cycle) -> str:
        return "0x" + hashlib.sha256(cycle.key.encode()).hexdigest()

    @staticmethod
    def _pool_kwargs(pool: PoolMetadata) -> dict:
        venue = AdapterRegistry.canonical_venue(pool.venue)
        if venue == "uniswap-v3":
            return {"fee": pool.fee, "block_identifier": "pending"}
        if venue == "aerodrome-slipstream":
            return {"tick_spacing": pool.tick_spacing, "block_identifier": "pending"}
        if venue == "aerodrome":
            return {"stable": pool.stable, "block_identifier": "pending"}
        return {"block_identifier": "pending"}

    async def _quote_cycle(self, cycle: Cycle, amount_in: int) -> tuple[int, list[Quote]]:
        amount = amount_in
        quotes: list[Quote] = []
        for index, pool in enumerate(cycle.pools):
            adapter = self.registry.get(pool.venue)
            if adapter is None:
                raise RuntimeError(f"no configured live quote adapter for venue={pool.venue}")
            quote = await adapter.quote_exact_input(
                cycle.tokens[index],
                cycle.tokens[index + 1],
                amount,
                **self._pool_kwargs(pool),
            )
            if quote.amount_in != amount:
                raise RuntimeError("quote adapter returned mismatched amount_in")
            amount = quote.amount_out
            quotes.append(quote)
        return amount, quotes

    def _candidate_amounts(self, upper: int) -> list[int]:
        if upper <= 0:
            return []
        values = {upper, 1}
        x = upper
        while x > 1 and len(values) < self.max_quote_evaluations:
            x //= 2
            values.add(max(1, x))
        return sorted(values)

    async def _optimize_size(self, cycle: Cycle) -> tuple[int | None, int | None, list[Quote], list[str]]:
        blockers: list[str] = []
        token_in = cycle.tokens[0]
        first_pool = cycle.pools[0]
        try:
            if self.balance_provider is None:
                self.balance_provider = LiveBalanceProvider(settings.base_http_rpc)
            upper = await self.balance_provider.token_balance(token_in, first_pool.address, "pending")
        except Exception as exc:
            return None, None, [], [f"live_balance_query_failed:{type(exc).__name__}"]
        if upper <= 0:
            return None, None, [], ["first_pool_has_zero_pending_token_balance"]

        best_amount: int | None = None
        best_out: int | None = None
        best_profit: int | None = None
        for amount in self._candidate_amounts(upper):
            try:
                out, _quotes = await self._quote_cycle(cycle, amount)
            except Exception as exc:
                blockers.append(f"quote_failed:{type(exc).__name__}")
                continue
            profit = out - amount
            if best_profit is None or profit > best_profit:
                best_amount, best_out, best_profit = amount, out, profit

        if best_amount is None or best_out is None:
            if not blockers:
                blockers.append("no_live_quotes_returned")
            return None, None, [], sorted(set(blockers))

        # Re-quote selected size immediately against Base pending state.
        try:
            final_out, final_quotes = await self._quote_cycle(cycle, best_amount)
        except Exception as exc:
            return None, None, [], [f"pending_resimulation_failed:{type(exc).__name__}"]
        return best_amount, final_out, final_quotes, sorted(set(blockers))

    async def evaluate_cycle(self, cycle: Cycle, observed_block: int | None = None) -> RouteEvaluation:
        route_id = self._route_id(cycle)
        blockers: list[str] = []
        for pool in cycle.pools:
            canonical = self.registry.canonical_venue(pool.venue)
            if self.registry.get(pool.venue) is None:
                blockers.append(f"missing_adapter:{canonical}")
            if canonical == "uniswap-v3" and pool.fee is None:
                blockers.append(f"missing_pool_fee:{pool.address}")
            if canonical == "aerodrome" and pool.stable is None:
                blockers.append(f"missing_stable_flag:{pool.address}")
            if canonical == "aerodrome-slipstream" and pool.tick_spacing is None:
                blockers.append(f"missing_tick_spacing:{pool.address}")
        if blockers:
            return RouteEvaluation(route_id, tuple(p.address for p in cycle.pools), cycle.tokens, None, None, None, tuple(), None, False, False, tuple(sorted(set(blockers))), tuple(), {})

        amount_in, amount_out, quotes, size_blockers = await self._optimize_size(cycle)
        blockers.extend(size_blockers)
        if amount_in is None or amount_out is None:
            return RouteEvaluation(route_id, tuple(p.address for p in cycle.pools), cycle.tokens, None, None, None, tuple(), None, False, False, tuple(sorted(set(blockers))), tuple(), {})

        gross = amount_out - amount_in
        gas_values = [q.gas_estimate for q in quotes]
        quote_gas_units = sum(v for v in gas_values if v is not None) if all(v is not None for v in gas_values) else None
        if gross <= 0:
            blockers.append("non_positive_gross_profit")
            return RouteEvaluation(
                route_id, tuple(p.address for p in cycle.pools), cycle.tokens, amount_in, amount_out, gross,
                tuple(q.block_number for q in quotes), quote_gas_units, False, False,
                tuple(sorted(set(blockers))), tuple(q.metadata for q in quotes), {}
            )

        execution: dict = {}
        ev_ready = False
        submission_eligible = False
        if self.execution_finalizer is None:
            blockers.append("execution_finalizer_unavailable")
        else:
            final = await self.execution_finalizer.finalize(cycle, amount_in, amount_out, quotes, observed_block)
            execution = final.to_dict()
            packed_builder = getattr(self.execution_finalizer, "packed_candidate_dict", None)
            if packed_builder is not None:
                packed_candidate = packed_builder(cycle, amount_in, amount_out, quotes, final)
                if packed_candidate is not None:
                    execution["packed_candidate"] = packed_candidate
            blockers.extend(final.blockers)
            ev_ready = final.deterministic_net_profit_units is not None and final.simulation_success
            submission_eligible = final.submission_eligible

        return RouteEvaluation(
            route_id=route_id,
            pools=tuple(p.address for p in cycle.pools),
            tokens=cycle.tokens,
            amount_in=amount_in,
            amount_out=amount_out,
            gross_profit_units=gross,
            quote_block_numbers=tuple(q.block_number for q in quotes),
            quote_gas_units=quote_gas_units,
            ev_ready=ev_ready,
            submission_eligible=submission_eligible,
            blockers=tuple(sorted(set(blockers))),
            quote_metadata=tuple(q.metadata for q in quotes),
            execution=execution,
        )

    def _skipped(self, cycle: Cycle, blocker: str) -> RouteEvaluation:
        return RouteEvaluation(
            route_id=self._route_id(cycle),
            pools=tuple(p.address for p in cycle.pools),
            tokens=cycle.tokens,
            amount_in=None,
            amount_out=None,
            gross_profit_units=None,
            quote_block_numbers=tuple(),
            quote_gas_units=None,
            ev_ready=False,
            submission_eligible=False,
            blockers=(blocker,),
            quote_metadata=tuple(),
            execution={},
        )

    @staticmethod
    def _dedup_cycles(cycles: list[Cycle]) -> list[Cycle]:
        """Collapse rotations of the same directed ring.

        A ring A->B->C->A is enumerated once per entry point (3x for 3-hop, 2x for 2-hop).
        Those rotations are the same trade, so quoting and packing them all burns the
        evaluation budget on duplicates and puts the same trade into a packed batch several
        times. Keep one representative per rotation-invariant signature. Direction is
        preserved: the reverse ring is a different trade and is kept.
        """
        seen: set[tuple] = set()
        out: list[Cycle] = []
        for cycle in cycles:
            edges = tuple(
                (cycle.pools[i].address, cycle.tokens[i], cycle.tokens[i + 1])
                for i in range(len(cycle.pools))
            )
            canonical = min(edges[i:] + edges[:i] for i in range(len(edges))) if edges else edges
            if canonical in seen:
                continue
            seen.add(canonical)
            out.append(cycle)
        return out

    async def evaluate_affected(
        self,
        affected_addresses: list[str],
        max_hops: int | None = None,
        observed_block: int | None = None,
    ) -> list[RouteEvaluation]:
        """Evaluate every affected cycle under a hard wall-clock budget.

        Automatic pool discovery registers the whole verified Base pool set, so
        `cycles_for_affected` can return hundreds of cycles. Evaluating them one at a time,
        each with its own per-cycle timeout, makes the total unbounded -- the caller (and the
        Rust executor's in-flight trigger slot) would block far past the point where the
        opportunity is still live. Fan out with bounded concurrency and stop at the deadline.
        """
        cycles = self._dedup_cycles(
            self.graph.cycles_for_affected(affected_addresses, max_hops or settings.max_route_hops)
        )
        if not cycles:
            return []
        cycle_timeout = settings.quote_timeout_seconds + settings.execution_rpc_timeout_seconds * 6
        deadline = asyncio.get_running_loop().time() + settings.route_evaluation_budget_seconds
        semaphore = asyncio.Semaphore(max(1, settings.quote_concurrency))

        async def _eval(cycle: Cycle) -> RouteEvaluation:
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                return self._skipped(cycle, "route_evaluation_budget_exhausted")
            try:
                async with semaphore:
                    remaining = deadline - asyncio.get_running_loop().time()
                    if remaining <= 0:
                        return self._skipped(cycle, "route_evaluation_budget_exhausted")
                    return await asyncio.wait_for(
                        self.evaluate_cycle(cycle, observed_block),
                        timeout=min(cycle_timeout, remaining),
                    )
            except asyncio.TimeoutError:
                return self._skipped(cycle, "route_evaluation_timeout")

        # gather preserves input order, so results line up with `cycles` deterministically.
        return list(await asyncio.gather(*(_eval(cycle) for cycle in cycles)))
