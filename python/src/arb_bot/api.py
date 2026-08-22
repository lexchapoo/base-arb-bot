from __future__ import annotations
import asyncio
import json
import logging
from decimal import Decimal
from time import perf_counter
import httpx
from fastapi import FastAPI, HTTPException
from fastapi.responses import PlainTextResponse
from sqlalchemy import select, text
from . import metrics
from .config import settings
from .models import RouteCandidate, ScoreResult, ExecutionEnvelope, PoolRegistration, PendingLogTrigger
from .scoring import RiskGate
from .rl_gate import DeterministicPolicy
from .event_graph import LivePoolGraph, PoolMetadata
from .route_engine import RouteOptimizer
from .batch_execution import PackedBatchFinalizer
from .pool_discovery import BasePoolDiscovery
from .db.models import OpportunityRecord, SubmissionRecord, PendingPoolEventRecord, RouteEvaluationRecord, OpportunityTelemetryRecord
from .db.session import SessionLocal, init_db
from .db.migrations import migrate_v012
from .shadow_intelligence import route_family, empirical_survival_bps, empirical_capture_bps, choose_shadow_action, competitor_fingerprint
from .db.pool_registry import PostgresPoolRegistry

logger=logging.getLogger(__name__)
app=FastAPI(title="Base Arbitrage Strategy API", version="0.12.0")
gate=RiskGate(
    Decimal(str(settings.min_profit_usd)) if settings.min_profit_usd is not None else None,
    Decimal(str(settings.max_gas_usd)) if settings.max_gas_usd is not None else None,
    settings.max_route_hops,
    settings.max_state_age_blocks,
)
policy=DeterministicPolicy()
pool_graph=LivePoolGraph()
route_optimizer=RouteOptimizer(pool_graph)
pool_discovery = BasePoolDiscovery(
    settings.base_http_rpc, pool_graph,
    aerodrome_factory=settings.aerodrome_factory,
    uniswap_v3_factory=settings.uniswap_v3_factory,
    slipstream_factory=settings.aerodrome_slipstream_factory,
    uniswap_from_block=settings.pool_discovery_from_block,
    log_chunk_blocks=settings.pool_discovery_log_chunk_blocks,
    concurrency=settings.pool_discovery_concurrency,
    token_allowlist=[x for x in settings.pool_discovery_token_allowlist.split(",") if x.strip()],
    store=PostgresPoolRegistry(),
)
# Mutated in place (never rebound) so every reader observes the same dict object.
pool_discovery_status: dict[str, object] = {
    "enabled": settings.auto_pool_discovery_enabled,
    "last_error": None,
    "ready": False,
    "first_scan_complete": False,
}
_pool_discovery_task: asyncio.Task | None = None
# asyncio holds only a weak reference to a running task, so a bare `create_task(...)` whose
# handle is dropped can be garbage-collected mid-scan. Keep strong refs until they finish.
_background_tasks: set[asyncio.Task] = set()


def _spawn_background(coro) -> asyncio.Task:
    task = asyncio.create_task(coro)
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)
    return task

schema_status: dict[str, object] = {"ready": False, "last_error": None}

async def _discovery_loop():
    """Own the whole discovery lifecycle off the startup path.

    A cold registry means a full-history scan (every Aerodrome allPools index, plus the
    Uniswap PoolCreated log history from POOL_DISCOVERY_FROM_BLOCK to head). That is
    minutes-to-hours of RPC work and must never block uvicorn from binding and serving.
    """
    first = True
    while True:
        try:
            if first:
                restored = await pool_discovery.restore()
                pool_discovery_status.update(restored)
            result = await pool_discovery.refresh()
            pool_discovery_status.update(
                {"enabled": True, "last_error": None, "first_scan_complete": True, **result}
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            pool_discovery_status.update(
                {"enabled": True, "last_error": f"{type(exc).__name__}: {exc}"}
            )
        first = False
        await asyncio.sleep(max(5, settings.pool_discovery_refresh_seconds))

_schema_lock = asyncio.Lock()

async def _ensure_schema() -> bool:
    """Create/migrate the schema, retrying on every call until it succeeds.

    Startup is not the only chance: Postgres may still be coming up when the process boots,
    and a one-shot attempt would leave /ready (and therefore the container healthcheck)
    permanently unhealthy long after the database recovered.
    """
    if schema_status["ready"]:
        return True
    async with _schema_lock:
        if schema_status["ready"]:
            return True
        try:
            await init_db()
            await migrate_v012()
            schema_status.update({"ready": True, "last_error": None})
            return True
        except Exception as exc:
            # Do not crash the process (the DB may still be starting), but record the failure so
            # /ready reports unhealthy instead of silently dropping every subsequent write.
            schema_status.update({"ready": False, "last_error": f"{type(exc).__name__}: {exc}"})
            logger.exception("database schema initialisation failed; /ready will report unhealthy")
            return False

@app.on_event("startup")
async def startup():
    global _pool_discovery_task
    await _ensure_schema()
    if settings.auto_pool_discovery_enabled:
        _pool_discovery_task = asyncio.create_task(_discovery_loop())

@app.on_event("shutdown")
async def shutdown():
    pending = list(_background_tasks)
    if _pool_discovery_task is not None:
        pending.append(_pool_discovery_task)
    for task in pending:
        task.cancel()
    for task in pending:
        try:
            await task
        except asyncio.CancelledError:
            pass
        except Exception:
            logger.exception("background task failed during shutdown")

@app.get("/health")
def health():
    graph = pool_graph.snapshot()
    return {
        "status":"ok",
        "chain_id":settings.chain_id,
        "dry_run":settings.dry_run,
        "registered_pools":graph["pool_count"],
        "route_optimizer":"enabled",
    }

@app.get("/ready")
async def ready():
    if not await _ensure_schema():
        raise HTTPException(503, f"database schema unavailable: {schema_status['last_error']}")
    try:
        async with SessionLocal() as db:
            await db.execute(text("SELECT 1"))
    except Exception as exc:
        raise HTTPException(503, "database unavailable") from exc
    return {
        "status": "ready",
        "chain_id": settings.chain_id,
        "dry_run": settings.dry_run,
        "pool_discovery": {
            "enabled": bool(pool_discovery_status.get("enabled")),
            "first_scan_complete": bool(pool_discovery_status.get("first_scan_complete")),
            "last_error": pool_discovery_status.get("last_error"),
        },
    }

@app.get("/pools/addresses")
def pool_addresses():
    # Rust merges these automatically with any manual WATCHED_POOL_ADDRESSES entries.
    addresses = pool_graph.addresses()
    return {"addresses": addresses, "count": len(addresses), "discovery": pool_discovery_status}

@app.post("/pools/refresh")
async def refresh_pools(wait: bool = False):
    """Trigger a discovery pass. Returns immediately unless `wait=true`.

    A cold scan can take minutes, so the default is fire-and-forget; `refresh()` is
    serialised internally by its own lock, so a concurrent scheduled pass is safe.
    """
    if not settings.auto_pool_discovery_enabled:
        raise HTTPException(409, "automatic pool discovery is disabled")
    if not wait:
        _spawn_background(_refresh_once())
        return {"scheduled": True, **pool_discovery_status}
    try:
        result = await pool_discovery.refresh()
        pool_discovery_status.update(
            {"enabled": True, "last_error": None, "first_scan_complete": True, **result}
        )
        return pool_discovery_status
    except Exception as exc:
        pool_discovery_status.update({"enabled": True, "last_error": f"{type(exc).__name__}: {exc}"})
        raise HTTPException(502, dict(pool_discovery_status)) from exc

async def _refresh_once():
    try:
        result = await pool_discovery.refresh()
        pool_discovery_status.update(
            {"enabled": True, "last_error": None, "first_scan_complete": True, **result}
        )
    except Exception as exc:
        pool_discovery_status.update({"enabled": True, "last_error": f"{type(exc).__name__}: {exc}"})

@app.post("/pools/register")
def register_pool(pool: PoolRegistration):
    """Register pool metadata produced by live discovery; no synthetic metadata is generated here."""
    metadata=PoolMetadata.create(
        address=pool.address,
        token0=pool.token0,
        token1=pool.token1,
        venue=pool.venue,
        pool_type=pool.pool_type,
        fee=pool.fee,
        stable=pool.stable,
    )
    pool_graph.register(metadata)
    return {"registered":True,"pool":metadata.address,"graph":pool_graph.snapshot()}

@app.get("/pools")
def pools():
    return pool_graph.snapshot()

async def _shadow_decision_for(*, trigger_pool: str, route_ids: list[str], deterministic_profit_units: int):
    family=route_family(route_ids)
    async with SessionLocal() as db:
        events=(await db.execute(
            select(PendingPoolEventRecord)
            .where(PendingPoolEventRecord.pool_address==trigger_pool)
            .order_by(PendingPoolEventRecord.observed_at_unix_ms.desc())
            .limit(settings.shadow_history_limit)
        )).scalars().all()
        times=sorted({int(e.observed_at_unix_ms) for e in events})
        durations=[b-a for a,b in zip(times,times[1:]) if b>=a]
        survival_bps,survival_samples=empirical_survival_bps(
            durations, settings.expected_execution_latency_ms, settings.shadow_min_samples
        )
        prior=(await db.execute(
            select(OpportunityTelemetryRecord)
            .where(OpportunityTelemetryRecord.route_family==family)
            .where(OpportunityTelemetryRecord.receipt_status.is_not(None))
            .order_by(OpportunityTelemetryRecord.id.desc())
            .limit(settings.shadow_history_limit)
        )).scalars().all()
        outcomes=[int(r.receipt_status)==1 and (r.realized_profit_units_ex_gas is None or int(r.realized_profit_units_ex_gas)>0) for r in prior]
        capture_bps,capture_samples=empirical_capture_bps(outcomes, settings.shadow_min_samples)
    return family, choose_shadow_action(
        deterministic_profit_units=deterministic_profit_units,
        survival_bps=survival_bps, capture_bps=capture_bps,
        survival_samples=survival_samples, capture_samples=capture_samples,
        min_samples=settings.shadow_min_samples,
        now_capture_bps=settings.shadow_now_capture_bps,
        now_survival_bps=settings.shadow_now_survival_bps,
        wait_survival_bps=settings.shadow_wait_survival_bps,
    )

def _opportunity_id(trigger: PendingLogTrigger, plan) -> str:
    """Stable, collision-free identity for one observed opportunity.

    The Rust executor echoes this back as `route_id` on /telemetry/reconcile, so whatever is
    sent as the submission `route_id` must be exactly this value.
    """
    discriminator = trigger.state_version or str(trigger.state_sequence or trigger.observed_at_unix_ms)
    batch_hash = plan.batch_hash if plan is not None else None
    return f"{batch_hash}:{discriminator}" if batch_hash else f"state:{discriminator}"


@app.post("/route-trigger")
async def route_trigger(trigger: PendingLogTrigger):
    affected=pool_graph.affected_subgraph(trigger.pool_address)
    evaluations=[]
    evaluation_started=perf_counter()
    if affected["known_pool"]:
        evaluations=await route_optimizer.evaluate_affected(
            affected["affected_pools"], settings.max_route_hops, trigger.block_number
        )
    evaluation_latency_ms=round((perf_counter()-evaluation_started)*1000)
    # Committed in its own transaction: the observation feed (/intelligence/competitors,
    # survival calibration) must survive a failure in the much larger evaluation insert below.
    try:
        async with SessionLocal() as db:
            db.add(PendingPoolEventRecord(
                pool_address=trigger.pool_address,
                topic0=trigger.topic0,
                transaction_hash=trigger.transaction_hash,
                block_number=trigger.block_number,
                transaction_index=trigger.transaction_index,
                log_index=trigger.log_index,
                observed_at_unix_ms=trigger.observed_at_unix_ms,
                known_pool=bool(affected["known_pool"]),
                affected_pools_json=json.dumps(affected["affected_pools"]),
                raw_data=trigger.raw_data,
                source_sender=trigger.source_sender,
                tx_touched_pools_json=json.dumps(trigger.tx_touched_pools),
            ))
            await db.commit()
    except Exception:
        pass
    try:
        async with SessionLocal() as db:
            for result in evaluations:
                db.add(RouteEvaluationRecord(
                    route_id=result.route_id,
                    trigger_pool=trigger.pool_address,
                    pools_json=json.dumps(result.pools),
                    tokens_json=json.dumps(result.tokens),
                    amount_in=str(result.amount_in) if result.amount_in is not None else None,
                    amount_out=str(result.amount_out) if result.amount_out is not None else None,
                    gross_profit_units=str(result.gross_profit_units) if result.gross_profit_units is not None else None,
                    quote_gas_units=result.quote_gas_units,
                    ev_ready=result.ev_ready,
                    submission_eligible=result.submission_eligible,
                    blockers_json=json.dumps(result.blockers),
                    quote_metadata_json=json.dumps(result.quote_metadata),
                    execution_json=json.dumps(result.execution),
                    executor_calldata=result.execution.get("calldata") if result.execution else None,
                    simulation_gas_units=result.execution.get("simulation_gas_units") if result.execution else None,
                    estimate_gas_units=result.execution.get("estimate_gas_units") if result.execution else None,
                    total_native_fee_wei=str(result.execution.get("total_native_fee_wei")) if result.execution and result.execution.get("total_native_fee_wei") is not None else None,
                    gas_cost_asset_units=str(result.execution.get("gas_cost_asset_units")) if result.execution and result.execution.get("gas_cost_asset_units") is not None else None,
                    flashloan_premium_bps=result.execution.get("flashloan_premium_bps") if result.execution else None,
                    flashloan_premium_units=str(result.execution.get("flashloan_premium_units")) if result.execution and result.execution.get("flashloan_premium_units") is not None else None,
                    deterministic_net_profit_units=str(result.execution.get("deterministic_net_profit_units")) if result.execution and result.execution.get("deterministic_net_profit_units") is not None else None,
                    evaluation_latency_ms=evaluation_latency_ms,
                    provider_disagreement=None,
                    would_submit=result.submission_eligible,
                ))
            await db.commit()
    except Exception:
        pass
    auto_submissions=[]
    packed_batch_plan=None
    if settings.auto_submit_routes and settings.packed_multiroute_enabled and route_optimizer.execution_finalizer is not None:
        packed_batch_plan = await PackedBatchFinalizer(route_optimizer.execution_finalizer).finalize(evaluations, observed_at_unix_ms=trigger.observed_at_unix_ms)
        # opportunity_id is UNIQUE in the schema. The packed batch hash alone is not unique --
        # the same candidate menu re-derived on a later trigger produces the same hash -- so a
        # collision would silently drop this row and later let the reconcile step overwrite the
        # earlier, unrelated opportunity. Qualify it with the per-trigger state fingerprint.
        opportunity_id = _opportunity_id(trigger, packed_batch_plan)
        try:
            async with SessionLocal() as db:
                db.add(OpportunityTelemetryRecord(
                    opportunity_id=opportunity_id,
                    source_tx=trigger.transaction_hash,
                    trigger_pool=trigger.pool_address,
                    observed_block=trigger.block_number,
                    state_version=trigger.state_version,
                    state_sequence=trigger.state_sequence,
                    affected_pools_json=json.dumps(affected["affected_pools"]),
                    candidate_routes_json=json.dumps(list(packed_batch_plan.candidate_route_ids) if packed_batch_plan else [r.route_id for r in evaluations]),
                    packed_batch_hash=packed_batch_plan.batch_hash if packed_batch_plan else None,
                    deterministic_net_profit_units=str(packed_batch_plan.deterministic_net_profit_units) if packed_batch_plan else None,
                    decision="SUBMIT" if packed_batch_plan and packed_batch_plan.submission_eligible else "SKIP",
                    decision_reason="packed_batch_eligible_after_exact_simulation" if packed_batch_plan and packed_batch_plan.submission_eligible else "no_submission_eligible_packed_batch",
                ))
                await db.commit()
            if settings.shadow_policy_enabled:
                route_ids=list(packed_batch_plan.candidate_route_ids) if packed_batch_plan else [r.route_id for r in evaluations]
                profit=int(packed_batch_plan.deterministic_net_profit_units) if packed_batch_plan and packed_batch_plan.deterministic_net_profit_units is not None else 0
                family,shadow=await _shadow_decision_for(trigger_pool=trigger.pool_address,route_ids=route_ids,deterministic_profit_units=profit)
                async with SessionLocal() as db:
                    row=(await db.execute(select(OpportunityTelemetryRecord).where(OpportunityTelemetryRecord.opportunity_id==opportunity_id))).scalar_one_or_none()
                    if row is not None:
                        row.route_family=family
                        row.survival_probability_bps=shadow.survival_probability_bps
                        row.route_capture_probability_bps=shadow.route_capture_probability_bps
                        row.shadow_action=shadow.action
                        row.shadow_confidence_bps=shadow.confidence_bps
                        row.shadow_features_json=json.dumps(shadow.features)
                        await db.commit()
        except Exception:
            pass
        if packed_batch_plan.submission_eligible and packed_batch_plan.calldata:
            payload={
                "route_id":opportunity_id,
                "calldata":packed_batch_plan.calldata,
                "gas_limit":packed_batch_plan.estimate_gas_units or packed_batch_plan.simulation_gas_units,
                "deterministic_net_profit_units":str(packed_batch_plan.deterministic_net_profit_units),
                "asset":packed_batch_plan.asset,
                "target_block":packed_batch_plan.target_block,
                "opportunity_observed_at_unix_ms":trigger.observed_at_unix_ms,
                "expected_execution_latency_ms":settings.expected_execution_latency_ms,
                "survival_window_ms":settings.opportunity_survival_window_ms,
                "inclusion_probability_bps":settings.inclusion_probability_bps,
                "expected_failure_cost_units":str(settings.expected_failure_cost_asset_units),
                "safety_margin_units":str(settings.submission_safety_margin_asset_units),
                "trigger_pool":trigger.pool_address,
                "state_version":trigger.state_version,
                "state_sequence":trigger.state_sequence,
            }
            try:
                async with httpx.AsyncClient(timeout=15) as client:
                    response=await client.post(f"{settings.rust_executor_url}/submit-plan",json=payload)
                    body=response.json()
                    body["http_status"]=response.status_code
                    body["candidate_route_ids"]=list(packed_batch_plan.candidate_route_ids)
                    auto_submissions.append(body)
                    try:
                        async with SessionLocal() as db:
                            row=(await db.execute(select(OpportunityTelemetryRecord).where(OpportunityTelemetryRecord.opportunity_id==opportunity_id))).scalar_one_or_none()
                            if row is not None:
                                row.tx_hash=body.get("tx_hash")
                                row.submission_provider=body.get("provider")
                                row.submission_latency_ms=body.get("latency_ms")
                                row.decision="DRY_RUN" if body.get("dry_run", True) else ("SUBMITTED" if body.get("accepted") else "REJECTED")
                                row.decision_reason=body.get("message", row.decision_reason)
                                await db.commit()
                    except Exception:
                        pass
                    if body.get("submission_id"):
                        try:
                            async with SessionLocal() as db:
                                db.add(SubmissionRecord(
                                    submission_id=body["submission_id"],
                                    opportunity_id=opportunity_id,
                                    provider=body.get("provider","unknown"),
                                    dry_run=bool(body.get("dry_run",True)),
                                    accepted=bool(body.get("accepted",False)),
                                    tx_hash=body.get("tx_hash"),
                                    latency_ms=body.get("latency_ms"),
                                    message=body.get("message",""),
                                ))
                                await db.commit()
                        except Exception:
                            pass
            except Exception as exc:
                auto_submissions.append({"route_id":opportunity_id,"accepted":False,"error":type(exc).__name__})
    elif settings.auto_submit_routes:
        # Compatibility fallback when packed execution is explicitly disabled.
        async with httpx.AsyncClient(timeout=15) as client:
            for result in evaluations:
                if not result.submission_eligible or not result.execution.get("calldata"):
                    continue
                payload={
                    "route_id":result.route_id,
                    "calldata":result.execution["calldata"],
                    "gas_limit":result.execution.get("estimate_gas_units") or result.execution.get("simulation_gas_units"),
                    "deterministic_net_profit_units":str(result.execution.get("deterministic_net_profit_units")),
                    "asset":result.tokens[0],
                    "target_block":result.execution.get("target_block"),
                    # The Rust boundary rejects any submission whose trigger-pool state
                    # fingerprint is absent (HTTP 410), so this path must send the same
                    # state-versioned fields and adaptive inputs as the packed path.
                    "opportunity_observed_at_unix_ms":trigger.observed_at_unix_ms,
                    "expected_execution_latency_ms":settings.expected_execution_latency_ms,
                    "survival_window_ms":settings.opportunity_survival_window_ms,
                    "inclusion_probability_bps":settings.inclusion_probability_bps,
                    "expected_failure_cost_units":str(settings.expected_failure_cost_asset_units),
                    "safety_margin_units":str(settings.submission_safety_margin_asset_units),
                    "trigger_pool":trigger.pool_address,
                    "state_version":trigger.state_version,
                    "state_sequence":trigger.state_sequence,
                }
                try:
                    response=await client.post(f"{settings.rust_executor_url}/submit-plan",json=payload)
                    body=response.json(); body["http_status"]=response.status_code; auto_submissions.append(body)
                except Exception as exc:
                    auto_submissions.append({"route_id":result.route_id,"accepted":False,"error":type(exc).__name__})

    submission_candidates=sum(1 for result in evaluations if result.submission_eligible)
    metrics.inc("arb_route_triggers_total")
    metrics.inc("arb_route_triggers_known_pool_total" if affected["known_pool"] else "arb_route_triggers_unknown_pool_total")
    metrics.inc("arb_route_evaluations_total", len(evaluations))
    metrics.inc("arb_route_submission_eligible_total", submission_candidates)
    metrics.observe_latency_ms(evaluation_latency_ms)

    return {
        "triggered":bool(affected["known_pool"]),
        "reason":"route_optimizer_evaluated" if affected["known_pool"] else "pool_not_registered",
        **affected,
        "route_evaluations":[result.to_dict() for result in evaluations],
        "submission_candidates":submission_candidates,
        "packed_batch_plan":packed_batch_plan.to_dict() if packed_batch_plan else None,
        "auto_submissions":auto_submissions,
        "state_version":trigger.state_version,
        "state_sequence":trigger.state_sequence,
    }

@app.get("/metrics")
def prometheus_metrics() -> PlainTextResponse:
    graph = pool_graph.snapshot()
    gauges = {
        "arb_dry_run": 1 if settings.dry_run else 0,
        "arb_registered_pools": graph["pool_count"],
    }
    return PlainTextResponse(metrics.render(gauges))

@app.post("/score", response_model=ScoreResult)
async def score(candidate: RouteCandidate):
    result=gate.score(candidate)
    try:
        async with SessionLocal() as db:
            db.add(OpportunityRecord(opportunity_id=candidate.opportunity_id, observed_block=candidate.observed_block, target_block=candidate.target_block, route_hash=candidate.route_hash, approved=result.approved, expected_value_usd=result.expected_value_usd, net_profit_usd=result.net_profit_usd, reasons_json=json.dumps(result.reasons)))
            await db.commit()
    except Exception:
        pass
    return result

@app.post("/execute")
async def execute(envelope: ExecutionEnvelope):
    fresh=gate.score(envelope.candidate)
    if fresh != envelope.score: raise HTTPException(409,"score mismatch; rescore candidate")
    action=policy.choose(envelope.candidate,fresh)
    if action.value == 0: raise HTTPException(422,{"approved":False,"reasons":fresh.reasons})
    payload=envelope.model_dump(mode="json") | {"size_percent":action.value}
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            response=await client.post(f"{settings.rust_executor_url}/submit",json=payload)
            response.raise_for_status()
            body=response.json()
        try:
            async with SessionLocal() as db:
                db.add(SubmissionRecord(submission_id=body["submission_id"], opportunity_id=envelope.candidate.opportunity_id, provider=body.get("provider","unknown"), dry_run=body["dry_run"], accepted=body["accepted"], tx_hash=body.get("tx_hash"), latency_ms=body.get("latency_ms"), message=body["message"]))
                await db.commit()
        except Exception:
            pass
        return body
    except httpx.HTTPError as exc:
        raise HTTPException(502,f"executor unavailable: {exc}") from exc

@app.post("/telemetry/reconcile")
async def reconcile_telemetry(payload: dict):
    opportunity_id=str(payload.get("opportunity_id") or "")
    if not opportunity_id:
        raise HTTPException(400,"opportunity_id is required")
    async with SessionLocal() as db:
        row=(await db.execute(select(OpportunityTelemetryRecord).where(OpportunityTelemetryRecord.opportunity_id==opportunity_id))).scalar_one_or_none()
        if row is None:
            raise HTTPException(404,"opportunity telemetry not found")
        for name in ("tx_hash","submission_provider","failure_reason"):
            if name in payload:
                setattr(row,name,payload.get(name))
        for name in ("receipt_status","submission_latency_ms"):
            if payload.get(name) is not None:
                setattr(row,name,int(payload[name]))
        for name in ("gas_used","effective_gas_price_wei","l1_fee_wei","actual_native_fee_wei","asset_balance_before","asset_balance_after","realized_profit_units_ex_gas","realized_net_profit_units"):
            if payload.get(name) is not None:
                setattr(row,name,str(payload[name]))
        if "net_profit_exact" in payload:
            row.net_profit_exact=bool(payload["net_profit_exact"])
        if payload.get("receipt_status") is not None:
            row.decision="INCLUDED" if int(payload["receipt_status"]) == 1 else "REVERTED"
        await db.commit()
    return {"updated":True,"opportunity_id":opportunity_id}

@app.get("/telemetry/opportunities")
async def telemetry_opportunities(limit:int=100):
    async with SessionLocal() as db:
        rows=(await db.execute(select(OpportunityTelemetryRecord).order_by(OpportunityTelemetryRecord.id.desc()).limit(min(max(limit,1),500)))).scalars().all()
        return [{
            "opportunity_id":r.opportunity_id,"source_tx":r.source_tx,"trigger_pool":r.trigger_pool,
            "observed_block":r.observed_block,"state_version":r.state_version,"state_sequence":r.state_sequence,
            "decision":r.decision,"decision_reason":r.decision_reason,"tx_hash":r.tx_hash,
            "deterministic_net_profit_units":r.deterministic_net_profit_units,
            "actual_native_fee_wei":r.actual_native_fee_wei,"realized_profit_units_ex_gas":r.realized_profit_units_ex_gas,
            "realized_net_profit_units":r.realized_net_profit_units,"net_profit_exact":r.net_profit_exact,
            "failure_reason":r.failure_reason,"route_family":r.route_family,
            "survival_probability_bps":r.survival_probability_bps,
            "route_capture_probability_bps":r.route_capture_probability_bps,
            "shadow_action":r.shadow_action,"shadow_confidence_bps":r.shadow_confidence_bps,
            "created_at":r.created_at,
        } for r in rows]

@app.get("/telemetry/replay/{opportunity_id}")
async def replay_input(opportunity_id:str):
    async with SessionLocal() as db:
        row=(await db.execute(select(OpportunityTelemetryRecord).where(OpportunityTelemetryRecord.opportunity_id==opportunity_id))).scalar_one_or_none()
        if row is None:
            raise HTTPException(404,"opportunity telemetry not found")
        return {
            "opportunity_id":row.opportunity_id,"source_tx":row.source_tx,"trigger_pool":row.trigger_pool,
            "observed_block":row.observed_block,"state_version":row.state_version,"state_sequence":row.state_sequence,
            "affected_pools":json.loads(row.affected_pools_json or "[]"),
            "candidate_routes":json.loads(row.candidate_routes_json or "[]"),
            "packed_batch_hash":row.packed_batch_hash,"deterministic_net_profit_units":row.deterministic_net_profit_units,
            "decision":row.decision,"decision_reason":row.decision_reason,"tx_hash":row.tx_hash,
            "receipt":{"status":row.receipt_status,"gas_used":row.gas_used,"effective_gas_price_wei":row.effective_gas_price_wei,"l1_fee_wei":row.l1_fee_wei},
        }

@app.get("/intelligence/competitors")
async def intelligence_competitors(limit:int=500):
    async with SessionLocal() as db:
        rows=(await db.execute(
            select(PendingPoolEventRecord)
            .where(PendingPoolEventRecord.source_sender.is_not(None))
            .order_by(PendingPoolEventRecord.id.desc())
            .limit(min(max(limit,1),5000))
        )).scalars().all()
    events=[{
        "source_sender":r.source_sender,
        "tx_touched_pools":json.loads(r.tx_touched_pools_json or "[]"),
        "observed_at_unix_ms":r.observed_at_unix_ms,
    } for r in rows]
    return {"fingerprints":competitor_fingerprint(events),"observations":len(events)}

@app.get("/intelligence/shadow")
async def intelligence_shadow(limit:int=100):
    async with SessionLocal() as db:
        rows=(await db.execute(
            select(OpportunityTelemetryRecord)
            .where(OpportunityTelemetryRecord.shadow_action.is_not(None))
            .order_by(OpportunityTelemetryRecord.id.desc())
            .limit(min(max(limit,1),500))
        )).scalars().all()
    return [{
        "opportunity_id":r.opportunity_id,"route_family":r.route_family,
        "shadow_action":r.shadow_action,"confidence_bps":r.shadow_confidence_bps,
        "survival_probability_bps":r.survival_probability_bps,
        "route_capture_probability_bps":r.route_capture_probability_bps,
        "features":json.loads(r.shadow_features_json or "{}"),
        "real_decision":r.decision,"receipt_status":r.receipt_status,
    } for r in rows]

@app.get("/opportunities")
async def opportunities(limit:int=50):
    async with SessionLocal() as db:
        rows=(await db.execute(select(OpportunityRecord).order_by(OpportunityRecord.id.desc()).limit(min(limit,200)))).scalars().all()
        return [{"opportunity_id":r.opportunity_id,"approved":r.approved,"expected_value_usd":str(r.expected_value_usd),"route_hash":r.route_hash,"created_at":r.created_at} for r in rows]

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("arb_bot.api:app",host="0.0.0.0",port=8080)
