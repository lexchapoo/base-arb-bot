from __future__ import annotations
import json
from time import perf_counter
from decimal import Decimal
import httpx
from fastapi import FastAPI, HTTPException
from sqlalchemy import select, text
from .config import settings
from .models import RouteCandidate, ScoreResult, ExecutionEnvelope, PoolRegistration, PendingLogTrigger
from .scoring import RiskGate
from .rl_gate import DeterministicPolicy
from .event_graph import LivePoolGraph, PoolMetadata, load_configured_pools
from .route_engine import RouteOptimizer
from .batch_execution import PackedBatchFinalizer
from .db.models import OpportunityRecord, SubmissionRecord, PendingPoolEventRecord, RouteEvaluationRecord
from .db.session import SessionLocal, init_db

app=FastAPI(title="Base Arbitrage Strategy API", version="0.7.0")
gate=RiskGate(
    Decimal(str(settings.min_profit_usd)) if settings.min_profit_usd is not None else None,
    Decimal(str(settings.max_gas_usd)) if settings.max_gas_usd is not None else None,
    settings.max_route_hops,
    settings.max_state_age_blocks,
)
policy=DeterministicPolicy()
pool_graph=LivePoolGraph()
route_optimizer=RouteOptimizer(pool_graph)

@app.on_event("startup")
async def startup():
    await init_db()
    for pool in load_configured_pools(settings.watched_pools_json, settings.watched_pool_addresses):
        pool_graph.register(pool)

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
    try:
        async with SessionLocal() as db:
            await db.execute(text("SELECT 1"))
    except Exception as exc:
        raise HTTPException(503, "database unavailable") from exc
    return {"status":"ready","chain_id":settings.chain_id,"dry_run":settings.dry_run}

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

@app.post("/route-trigger")
async def route_trigger(trigger: PendingLogTrigger):
    affected=pool_graph.affected_subgraph(trigger.pool_address)
    evaluations=[]
    evaluation_started=perf_counter()
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
            ))
            await db.commit()
    except Exception:
        pass
    if affected["known_pool"]:
        finalizer=route_optimizer.execution_finalizer
        configured=getattr(finalizer,"configured",None)
        ready, configuration_blockers=(configured() if configured is not None else (True,[]))
        if ready:
            evaluations=await route_optimizer.evaluate_affected(
                affected["affected_pools"], settings.max_route_hops, trigger.block_number
            )
        else:
            evaluations=route_optimizer.reject_affected(
                affected["affected_pools"], configuration_blockers, settings.max_route_hops
            )
    evaluation_latency_ms=round((perf_counter()-evaluation_started)*1000)
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
        packed_batch_plan = await PackedBatchFinalizer(route_optimizer.execution_finalizer).finalize(evaluations)
        if packed_batch_plan.submission_eligible and packed_batch_plan.calldata:
            payload={
                "route_id":packed_batch_plan.batch_hash or "packed-batch",
                "calldata":packed_batch_plan.calldata,
                "gas_limit":packed_batch_plan.estimate_gas_units or packed_batch_plan.simulation_gas_units,
                "deterministic_net_profit_units":str(packed_batch_plan.deterministic_net_profit_units),
                "asset":packed_batch_plan.asset,
                "target_block":packed_batch_plan.target_block,
            }
            try:
                async with httpx.AsyncClient(timeout=15) as client:
                    response=await client.post(f"{settings.rust_executor_url}/submit-plan",json=payload)
                    body=response.json()
                    body["http_status"]=response.status_code
                    body["candidate_route_ids"]=list(packed_batch_plan.candidate_route_ids)
                    auto_submissions.append(body)
                    if body.get("submission_id"):
                        try:
                            async with SessionLocal() as db:
                                db.add(SubmissionRecord(
                                    submission_id=body["submission_id"],
                                    opportunity_id=packed_batch_plan.batch_hash or "packed-batch",
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
                auto_submissions.append({"route_id":packed_batch_plan.batch_hash,"accepted":False,"error":type(exc).__name__})
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
                }
                try:
                    response=await client.post(f"{settings.rust_executor_url}/submit-plan",json=payload)
                    body=response.json(); body["http_status"]=response.status_code; auto_submissions.append(body)
                except Exception as exc:
                    auto_submissions.append({"route_id":result.route_id,"accepted":False,"error":type(exc).__name__})

    return {
        "triggered":bool(affected["known_pool"]),
        "reason":"route_optimizer_evaluated" if affected["known_pool"] else "pool_not_registered",
        **affected,
        "route_evaluations":[result.to_dict() for result in evaluations],
        "submission_candidates":sum(1 for result in evaluations if result.submission_eligible),
        "packed_batch_plan":packed_batch_plan.to_dict() if packed_batch_plan else None,
        "auto_submissions":auto_submissions,
    }

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

@app.get("/opportunities")
async def opportunities(limit:int=50):
    async with SessionLocal() as db:
        rows=(await db.execute(select(OpportunityRecord).order_by(OpportunityRecord.id.desc()).limit(min(limit,200)))).scalars().all()
        return [{"opportunity_id":r.opportunity_id,"approved":r.approved,"expected_value_usd":str(r.expected_value_usd),"route_hash":r.route_hash,"created_at":r.created_at} for r in rows]

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("arb_bot.api:app",host="0.0.0.0",port=8080)
