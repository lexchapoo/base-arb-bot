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
    def __init__(self, rpc_url: str | list[str]) -> None:
        from web3 import AsyncWeb3
        from .multicall import RotatingProviders
        self._async_web3 = AsyncWeb3
        urls = [rpc_url] if isinstance(rpc_url, str) else list(rpc_url)
        self.providers = RotatingProviders(urls)
        # Preserved for callers/tests that reach for a single client.
        self.w3 = self.providers._clients[0]

    async def token_balance(self, token: str, account: str, block_identifier: str = "pending") -> int:
        # This is the fallback path taken when the multicall prefetch did not supply the balance,
        # so it runs precisely when the provider is already under pressure. Failing over to the
        # next endpoint keeps a rate limit from turning into `live_balance_query_failed`, which
        # discards the route entirely.
        last_error: Exception | None = None
        for w3 in self.providers.ordered():
            try:
                contract = w3.eth.contract(
                    address=self._async_web3.to_checksum_address(token), abi=ERC20_BALANCE_ABI
                )
                return int(
                    await contract.functions.balanceOf(
                        self._async_web3.to_checksum_address(account)
                    ).call(block_identifier=block_identifier)
                )
            except Exception as exc:
                last_error = exc
        raise last_error if last_error else RuntimeError("no RPC endpoint available")


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
        # Bounds how many quote/cycle coroutines hit the RPC at once when
        # evaluation fans out with asyncio.gather. Created without a running
        # loop (safe on 3.10+, which no longer binds the semaphore to a loop
        # at construction) and shared across requests as a global throttle.
        self._quote_semaphore = asyncio.Semaphore(max(1, settings.quote_concurrency))
        self._multicall_client = None
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

    @staticmethod
    def _refine_amounts(grid: list[int], best_amount: int, points: int) -> list[int]:
        """Sizes strictly between the best candidate's neighbours in the grid.

        Used for one extra batched quote round to sharpen the optimal size.
        Deterministic and integer-only.
        """
        ordered = sorted(set(grid))
        if points <= 0 or best_amount not in ordered:
            return []
        idx = ordered.index(best_amount)
        lo = ordered[idx - 1] if idx > 0 else max(1, best_amount // 2)
        hi = ordered[idx + 1] if idx + 1 < len(ordered) else best_amount
        if hi <= lo:
            return []
        span = hi - lo
        out: set[int] = set()
        for k in range(1, points + 1):
            candidate = lo + span * k // (points + 1)
            if candidate > 0 and candidate != best_amount:
                out.add(candidate)
        return sorted(out)

    @staticmethod
    def _dedup_cycles(cycles: list[Cycle]) -> list[Cycle]:
        """Collapse rotations of the same directed ring.

        A ring A->B->C->A is enumerated once per entry point (3x for 3-hop,
        2x for 2-hop). Those rotations are the same trade, so we keep one
        representative per rotation-invariant signature. Direction is
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

    async def _prefetch_balances(self, cycles: list[Cycle]) -> dict[tuple[str, str], int]:
        """Batch every distinct (first-hop token, first pool) balanceOf via Multicall3."""
        if not cycles or not self._cycle_supports_batching(cycles[0]):
            return {}
        pairs = list({(cycle.tokens[0], cycle.pools[0].address) for cycle in cycles})
        calls: list[tuple[str, bytes]] = []
        for token, pool in pairs:
            account = pool.lower().removeprefix("0x").rjust(64, "0")
            calls.append((token, bytes.fromhex("70a08231" + account)))  # balanceOf(address)
        try:
            results = await self._multicall().aggregate3(calls, "pending")
        except Exception:
            return {}
        balances: dict[tuple[str, str], int] = {}
        for (token, pool), (ok, return_data) in zip(pairs, results):
            if ok and len(return_data) >= 32:
                balances[(token, pool)] = int.from_bytes(return_data[:32], "big")
        return balances

    def _cycle_supports_batching(self, cycle: Cycle) -> bool:
        """True only when every pool's adapter can encode/decode raw calldata."""
        for pool in cycle.pools:
            adapter = self.registry.get(pool.venue)
            if adapter is None or not hasattr(adapter, "encode_quote") or not hasattr(adapter, "decode_quote"):
                return False
        return True

    def _multicall(self) -> "MulticallClient":
        if self._multicall_client is None:
            from .multicall import MulticallClient
            from .multicall import endpoint_list
            self._multicall_client = MulticallClient(
                endpoint_list(settings.base_http_rpc, settings.base_http_rpcs)
            )
        return self._multicall_client

    async def _batched_quotes(
        self, cycle: Cycle, amounts: list[int], observed_block: int | None
    ) -> tuple[dict[int, tuple[int, list[Quote]]], list[str]]:
        """Quote every candidate size using one Multicall3 request per hop.

        Quotes are dependent along a cycle (hop N+1 input is hop N output) but
        independent across candidate sizes, so we batch the size dimension at
        each hop: C candidates x H hops collapses from C*H round-trips to H.
        """
        blockers: list[str] = []
        mc = self._multicall()
        quotes_by_index: dict[int, list[Quote]] = {i: [] for i in range(len(amounts))}
        current: dict[int, int] = {i: amounts[i] for i in range(len(amounts))}
        surviving = list(range(len(amounts)))
        for hop, pool in enumerate(cycle.pools):
            adapter = self.registry.get(pool.venue)
            kwargs = self._pool_kwargs(pool)
            token_in = cycle.tokens[hop]
            token_out = cycle.tokens[hop + 1]
            calls = [adapter.encode_quote(token_in, token_out, current[i], **kwargs) for i in surviving]
            try:
                results = await mc.aggregate3(calls, block_identifier=kwargs.get("block_identifier", "pending"))
            except Exception as exc:
                return {}, [f"multicall_failed:{type(exc).__name__}"]
            next_surviving: list[int] = []
            for i, (ok, return_data) in zip(surviving, results):
                if not ok or len(return_data) == 0:
                    blockers.append("quote_failed:ContractLogicError")
                    continue
                try:
                    quote = adapter.decode_quote(
                        return_data, token_in, token_out, current[i], block_number=observed_block, **kwargs
                    )
                except Exception as exc:
                    blockers.append(f"quote_decode_failed:{type(exc).__name__}")
                    continue
                quotes_by_index[i].append(quote)
                current[i] = quote.amount_out
                next_surviving.append(i)
            surviving = next_surviving
            if not surviving:
                break

        quoted: dict[int, tuple[int, list[Quote]]] = {}
        for i in surviving:
            if len(quotes_by_index[i]) == len(cycle.pools):
                quoted[amounts[i]] = (current[i], quotes_by_index[i])
        return quoted, blockers

    async def _parallel_quotes(
        self, cycle: Cycle, amounts: list[int]
    ) -> tuple[dict[int, tuple[int, list[Quote]]], list[str]]:
        """Fallback path for adapters without multicall support (e.g. test fakes).

        Quotes each candidate concurrently, bounded by the shared semaphore.
        """
        blockers: list[str] = []

        async def _quote_size(amount: int) -> tuple[int, int, list[Quote]]:
            async with self._quote_semaphore:
                out, quotes = await self._quote_cycle(cycle, amount)
            return amount, out, quotes

        settled = await asyncio.gather(
            *(_quote_size(amount) for amount in amounts), return_exceptions=True
        )
        quoted: dict[int, tuple[int, list[Quote]]] = {}
        for item in settled:
            if isinstance(item, BaseException):
                blockers.append(f"quote_failed:{type(item).__name__}")
                continue
            amount, out, quotes = item
            quoted[amount] = (out, quotes)
        return quoted, blockers

    async def _optimize_size(
        self,
        cycle: Cycle,
        observed_block: int | None = None,
        balance_map: dict[tuple[str, str], int] | None = None,
    ) -> tuple[int | None, int | None, list[Quote], list[str]]:
        blockers: list[str] = []
        token_in = cycle.tokens[0]
        first_pool = cycle.pools[0]
        upper = balance_map.get((token_in, first_pool.address)) if balance_map else None
        if upper is None:
            # Not prefetched (or batching unavailable): fall back to a live query.
            try:
                if self.balance_provider is None:
                    from .multicall import endpoint_list
                    self.balance_provider = LiveBalanceProvider(
                        endpoint_list(settings.base_http_rpc, settings.base_http_rpcs)
                    )
                upper = await self.balance_provider.token_balance(token_in, first_pool.address, "pending")
            except Exception as exc:
                return None, None, [], [f"live_balance_query_failed:{type(exc).__name__}"]
        if upper <= 0:
            return None, None, [], ["first_pool_has_zero_pending_token_balance"]

        amounts = self._candidate_amounts(upper)
        batched = self._cycle_supports_batching(cycle)
        if batched:
            quoted, quote_blockers = await self._batched_quotes(cycle, amounts, observed_block)
        else:
            quoted, quote_blockers = await self._parallel_quotes(cycle, amounts)
        blockers.extend(quote_blockers)

        def _pick_best(candidates: dict[int, tuple[int, list[Quote]]]) -> tuple[int | None, int | None]:
            # Iterate in deterministic ascending order; strict `>` keeps the
            # smallest size on a profit tie, matching pre-batch behaviour.
            b_amount: int | None = None
            b_out: int | None = None
            b_profit: int | None = None
            for amount in sorted(candidates):
                out, _quotes = candidates[amount]
                profit = out - amount
                if b_profit is None or profit > b_profit:
                    b_amount, b_out, b_profit = amount, out, profit
            return b_amount, b_out

        best_amount, best_out = _pick_best(quoted)

        # Second batched round: sharpen the optimum by quoting sizes between the
        # best candidate's neighbours. Stays fully batched and deterministic.
        if batched and best_amount is not None and settings.size_refinement_points > 0:
            refined = self._refine_amounts(amounts, best_amount, settings.size_refinement_points)
            if refined:
                extra, refine_blockers = await self._batched_quotes(cycle, refined, observed_block)
                blockers.extend(refine_blockers)
                quoted.update(extra)
                best_amount, best_out = _pick_best(quoted)

        if best_amount is None or best_out is None:
            if not blockers:
                blockers.append("no_live_quotes_returned")
            return None, None, [], sorted(set(blockers))

        # Re-quote the selected size immediately against Base pending state.
        if batched:
            requoted, requote_blockers = await self._batched_quotes(cycle, [best_amount], observed_block)
            item = requoted.get(best_amount)
            if item is None:
                return None, None, [], sorted(set(blockers + requote_blockers + ["pending_resimulation_failed"]))
            final_out, final_quotes = item
        else:
            try:
                final_out, final_quotes = await self._quote_cycle(cycle, best_amount)
            except Exception as exc:
                return None, None, [], [f"pending_resimulation_failed:{type(exc).__name__}"]
        return best_amount, final_out, final_quotes, sorted(set(blockers))

    async def evaluate_cycle(
        self,
        cycle: Cycle,
        observed_block: int | None = None,
        balance_map: dict[tuple[str, str], int] | None = None,
    ) -> RouteEvaluation:
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

        amount_in, amount_out, quotes, size_blockers = await self._optimize_size(cycle, observed_block, balance_map)
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

    def reject_affected(
        self,
        affected_addresses: list[str],
        blockers: list[str],
        max_hops: int | None = None,
    ) -> list[RouteEvaluation]:
        """Emit explicit candidates without RPC quoting when a global execution gate is absent."""
        return [
            RouteEvaluation(
                route_id=self._route_id(cycle),
                pools=tuple(pool.address for pool in cycle.pools),
                tokens=cycle.tokens,
                amount_in=None,
                amount_out=None,
                gross_profit_units=None,
                quote_block_numbers=tuple(),
                quote_gas_units=None,
                ev_ready=False,
                submission_eligible=False,
                blockers=tuple(sorted(set(blockers))),
                quote_metadata=tuple(),
                execution={},
            )
            for cycle in self.graph.cycles_for_affected(
                affected_addresses, max_hops or settings.max_route_hops
            )
        ]

    async def evaluate_affected(
        self,
        affected_addresses: list[str],
        max_hops: int | None = None,
        observed_block: int | None = None,
    ) -> list[RouteEvaluation]:
        cycles = self.graph.cycles_for_affected(affected_addresses, max_hops or settings.max_route_hops)
        cycles = self._dedup_cycles(cycles)
        cycle_timeout = settings.quote_timeout_seconds + settings.execution_rpc_timeout_seconds * 6
        # Fetch every distinct first-hop balance in one Multicall3 request instead
        # of one live query per cycle (many cycles share a starting pool/token).
        balance_map = await self._prefetch_balances(cycles)

        async def _eval(cycle: Cycle) -> RouteEvaluation:
            try:
                return await asyncio.wait_for(
                    self.evaluate_cycle(cycle, observed_block, balance_map), timeout=cycle_timeout
                )
            except asyncio.TimeoutError:
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
                    blockers=("route_evaluation_timeout",),
                    quote_metadata=tuple(),
                    execution={},
                )

        # Evaluate cycles concurrently; per-cycle quote fan-out is throttled by
        # the shared semaphore. gather preserves order so results line up with
        # `cycles` deterministically.
        results = await asyncio.gather(*(_eval(cycle) for cycle in cycles))
        return list(results)
