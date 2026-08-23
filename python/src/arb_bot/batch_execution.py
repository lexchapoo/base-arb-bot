from __future__ import annotations

from dataclasses import dataclass, asdict
from typing import Any, Iterable

from .config import settings
from .adaptive_submission import evaluate_adaptive_submission
from .execution import ExecutionFinalizer, conservative_eip1559_signed_size
from .packed_batch import PackedBatch, PackedCandidate, PackedLeg, candidate_id, encode_packed_batch


@dataclass(frozen=True, slots=True)
class PackedBatchPlan:
    batch_hash: str | None
    calldata: str | None
    asset: str | None
    amount: int | None
    target_block: int | None
    candidate_route_ids: tuple[str, ...]
    simulation_success: bool
    simulation_gas_units: int | None
    estimate_gas_units: int | None
    gas_cost_asset_units: int | None
    deterministic_net_profit_units: int | None
    submission_eligible: bool
    adaptive_submission: dict[str, Any] | None
    blockers: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        out = asdict(self)
        out["candidate_route_ids"] = list(self.candidate_route_ids)
        out["blockers"] = list(self.blockers)
        return out


def _to_candidate(route_id: str, data: dict[str, Any]) -> PackedCandidate:
    return PackedCandidate(
        candidate_id(route_id),
        int(data["min_profit"]),
        tuple(
            PackedLeg(
                adapter=leg["adapter"],
                token_in=leg["token_in"],
                token_out=leg["token_out"],
                min_out=int(leg["min_out"]),
                data=bytes.fromhex(str(leg.get("data", "0x"))[2:]),
            )
            for leg in data["legs"]
        ),
    )


def compatible_groups(evaluations: Iterable[Any]) -> list[list[Any]]:
    groups: dict[tuple[str, int, int, int], list[Any]] = {}
    for row in evaluations:
        if not row.submission_eligible:
            continue
        pc = row.execution.get("packed_candidate") if row.execution else None
        ev = row.execution.get("deterministic_net_profit_units") if row.execution else None
        if not pc or ev is None or int(ev) <= 0:
            continue
        key = (str(pc["asset"]).lower(), int(pc["amount"]), int(pc["target_block"]), int(pc["deadline"]))
        groups.setdefault(key, []).append(row)
    for rows in groups.values():
        rows.sort(key=lambda r: (-int(r.execution["deterministic_net_profit_units"]), r.route_id))
    return sorted(
        groups.values(),
        key=lambda rows: (-int(rows[0].execution["deterministic_net_profit_units"]), rows[0].route_id),
    )


class PackedBatchFinalizer:
    def __init__(self, execution_finalizer: ExecutionFinalizer) -> None:
        self.execution_finalizer = execution_finalizer

    async def finalize(self, evaluations: Iterable[Any], observed_at_unix_ms: int | None = None, now_unix_ms: int | None = None) -> PackedBatchPlan:
        groups = compatible_groups(evaluations)
        if not groups:
            return PackedBatchPlan(None, None, None, None, None, (), False, None, None, None, None, False, None, ("no_compatible_packed_candidates",))

        rows = groups[0][: max(1, min(settings.packed_max_candidates, 8))]
        runtime = self.execution_finalizer._runtime()
        rpc = self.execution_finalizer.rpc
        converter = self.execution_finalizer.converter
        blockers: list[str] = []

        # Drop low-EV fallbacks until the exact packed transaction has positive
        # conservative net profit for every candidate in the menu.
        while rows:
            first_pc = rows[0].execution["packed_candidate"]
            batch = PackedBatch(
                asset=first_pc["asset"],
                amount=int(first_pc["amount"]),
                target_block=int(first_pc["target_block"]),
                deadline=int(first_pc["deadline"]),
                candidates=tuple(_to_candidate(r.route_id, r.execution["packed_candidate"]) for r in rows),
            )
            packed = encode_packed_batch(batch)
            calldata, batch_hash = runtime.encode_start_packed(executor=settings.executor_address, packed=packed)
            tx = {
                "from": runtime.checksum(settings.executor_owner_address),
                "to": runtime.checksum(settings.executor_address),
                "data": calldata,
                "value": "0x0",
            }
            try:
                sim = await rpc.simulate(tx)
            except Exception as exc:
                return PackedBatchPlan(batch_hash, calldata, batch.asset, batch.amount, batch.target_block, tuple(r.route_id for r in rows), False, None, None, None, None, False, None, (f"packed_simulation_rpc_failed:{type(exc).__name__}",))
            if not sim.success:
                return PackedBatchPlan(batch_hash, calldata, batch.asset, batch.amount, batch.target_block, tuple(r.route_id for r in rows), False, sim.gas_used, None, None, None, False, None, (f"packed_executor_simulation_failed:{sim.error or 'unknown'}",))
            try:
                estimate = await rpc.estimate_gas(tx)
            except Exception:
                estimate = None
            gas_values = [v for v in (sim.gas_used, estimate) if v is not None]
            if not gas_values:
                return PackedBatchPlan(batch_hash, calldata, batch.asset, batch.amount, batch.target_block, tuple(r.route_id for r in rows), True, None, None, None, None, False, None, ("packed_gas_measurement_missing",))
            gas_used = max(gas_values)
            gas_price = await rpc.gas_price()
            nonce = await rpc.nonce(settings.executor_owner_address)
            priority = await rpc.priority_fee()
            base_fee = await rpc.pending_base_fee()
            max_fee = base_fee + priority
            tx_size = conservative_eip1559_signed_size(
                settings.chain_id, nonce, priority, max_fee, gas_used, settings.executor_address, calldata
            )
            l1_fee = await runtime.l1_fee_upper_bound(settings.gas_price_oracle, tx_size)
            total_native = gas_used * gas_price + l1_fee
            gas_asset = await converter.convert(batch.asset, total_native)
            min_candidate_profit = min(int(r.execution["packed_candidate"]["min_profit"]) for r in rows)
            conservative_net = min_candidate_profit - gas_asset
            if conservative_net > 0:
                adaptive = None
                eligible = True
                if settings.adaptive_submission_enabled:
                    import time
                    observed_ms = observed_at_unix_ms if observed_at_unix_ms is not None else int(time.time() * 1000)
                    now_ms = now_unix_ms if now_unix_ms is not None else int(time.time() * 1000)
                    decision = evaluate_adaptive_submission(
                        deterministic_net_profit_units=conservative_net,
                        observed_at_unix_ms=observed_ms,
                        now_unix_ms=now_ms,
                        expected_execution_latency_ms=settings.expected_execution_latency_ms,
                        survival_window_ms=settings.opportunity_survival_window_ms,
                        inclusion_probability_bps=settings.inclusion_probability_bps,
                        expected_failure_cost_units=settings.expected_failure_cost_asset_units,
                        safety_margin_units=settings.submission_safety_margin_asset_units,
                    )
                    adaptive = decision.to_dict()
                    eligible = decision.eligible
                    if decision.blocker:
                        blockers.append(decision.blocker)
                return PackedBatchPlan(
                    batch_hash, calldata, batch.asset, batch.amount, batch.target_block,
                    tuple(r.route_id for r in rows), True, sim.gas_used, estimate,
                    gas_asset, conservative_net, eligible, adaptive, tuple(blockers),
                )
            # `conservative_net` is bound by the *smallest* `min_profit` in the menu, because the
            # contract enforces only the selected candidate's floor. `rows` is ordered by each
            # route's own post-gas EV, which is a different ordering (per-route gas differs while
            # the packed transaction has a single shared cost), so dropping the tail can leave the
            # binding candidate in place and discard a menu that was profitable without it.
            trimmed = min(rows, key=lambda r: (int(r.execution["packed_candidate"]["min_profit"]), r.route_id))
            blockers.append(f"trimmed_low_ev_candidate:{trimmed.route_id}")
            rows = [r for r in rows if r.route_id != trimmed.route_id]

        return PackedBatchPlan(None, None, None, None, None, (), False, None, None, None, None, False, None, tuple(blockers + ["no_profitable_packed_batch_after_exact_costs"]))
