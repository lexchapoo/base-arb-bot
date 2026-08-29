from __future__ import annotations
import asyncio
import hashlib
import hmac
import json
import logging
from decimal import Decimal
from time import perf_counter
import httpx
from fastapi import Body, Depends, FastAPI, Header, HTTPException
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
from .db.pool_registry import PostgresCuratedPoolStore, PostgresPoolRegistry

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
# Serialises every write that spans durable storage and the in-memory graph.
#
# Both halves of such a write must land as one step. Each handler awaits the database and
# only then mutates the graph, so two interleaved handlers could commit in one order and
# mutate the graph in the other, leaving Postgres and the graph describing different pool
# sets until the next restart resolved it in favour of Postgres.
#
# Discovery takes it too, around "persist this batch, register it, advance the cursor"
# (BasePoolDiscovery._commit_guard). That step is the one place a pool can be written to
# `verified_pools`, filtered out of the graph by a restriction that is simultaneously
# being lifted, and then skipped forever because the cursor moved past it. Discovery's
# other graph writes -- restore() -- stay outside: they advance no cursor, so a rebuild
# from the same table always recovers them.
_curation_lock = asyncio.Lock()

# One registry instance, shared: discovery writes verified pools through it, and lifting a
# curated restriction reads them back out of it to rebuild the graph.
pool_registry = PostgresPoolRegistry()
def _build_pool_discovery() -> BasePoolDiscovery:
    """Construct discovery from settings.

    A function rather than an inline call so the wiring is testable: every argument here
    is a knob an operator can turn, and two of them were silently unreachable. The lag
    bound was not passed at all, so a deployment tuning it kept the 64-block default; and
    the split verification endpoint it guards had no setting to set, so the bound had
    nothing to measure either way and the whole check was dead code in the service.
    """
    return BasePoolDiscovery(
        settings.base_http_rpc, pool_graph,
        aerodrome_factory=settings.aerodrome_factory,
        uniswap_v3_factory=settings.uniswap_v3_factory,
        slipstream_factory=settings.aerodrome_slipstream_factory,
        uniswap_from_block=settings.pool_discovery_from_block,
        log_chunk_blocks=settings.pool_discovery_log_chunk_blocks,
        concurrency=settings.pool_discovery_concurrency,
        token_allowlist=[x for x in settings.pool_discovery_token_allowlist.split(",") if x.strip()],
        # Verification eth_calls, optionally on their own endpoint. Empty keeps both on the
        # log endpoint, in which case verify_endpoint_consistency() short-circuits and the
        # lag bound below is inert -- there is no second endpoint to fall behind.
        call_rpc_url=settings.pool_discovery_call_rpc_url,
        # How far the verification endpoint may trail the log endpoint before its answers are
        # treated as untrustworthy. Only meaningful once a separate call endpoint is configured,
        # but wired unconditionally so the setting cannot silently mean nothing.
        max_call_endpoint_lag_blocks=settings.pool_discovery_max_call_lag_blocks,
        store=pool_registry,
        # Discovery's persist-and-register step is a graph write, so it takes the same lock the
        # curation handlers do. Without it a pool persisted while a restriction is being lifted
        # is registered against the outgoing restriction, missed by the rebuild's earlier
        # registry snapshot, and then skipped forever because the cursor advanced past it.
        commit_lock=_curation_lock,
    )


pool_discovery = _build_pool_discovery()
# Mutated in place (never rebound) so every reader observes the same dict object.
pool_discovery_status: dict[str, object] = {
    "enabled": settings.auto_pool_discovery_enabled,
    "last_error": None,
    "ready": False,
    "first_scan_complete": False,
    # source -> why it is held. Empty when every source is scanning normally.
    "blocked_sources": {},
}
curated_pools = PostgresCuratedPoolStore()
curated_pool_status: dict[str, object] = {
    "restored": 0, "restricted": False, "loaded": False, "last_error": None,
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

def _record_discovery_success(result: dict) -> None:
    pool_discovery_status.update({
        "enabled": True, "last_error": None, "first_scan_complete": True,
        "blocked_sources": {}, **result,
    })


def _record_discovery_failure(exc: BaseException) -> None:
    """Record the failure, but publish whatever the pass did accomplish first.

    A code-absence blocker holds one source's cursor and says nothing about the other two,
    which may have reached head on the same pass. refresh() assembles its result before
    checking for blockers and hands it over on the exception, so the cursors, stats and
    pool count are real measurements -- discarding them left /ready reporting a
    permanently pre-first-scan discovery, with a stale error, while two of three sources
    were making steady progress.

    `first_scan_complete` is deliberately left alone: a held source has not reached head,
    so this pass cannot set it, and a latch set by an earlier clean pass is still true.
    """
    update: dict[str, object] = {
        "enabled": True, "last_error": f"{type(exc).__name__}: {exc}",
    }
    partial = getattr(exc, "partial", None)
    if isinstance(partial, dict):
        update.update(partial)
        update["blocked_sources"] = dict(getattr(exc, "blockers", {}) or {})
    pool_discovery_status.update(update)


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
            _record_discovery_success(await pool_discovery.refresh())
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            _record_discovery_failure(exc)
        first = False
        await asyncio.sleep(max(5, settings.pool_discovery_refresh_seconds))

_schema_lock = asyncio.Lock()

async def _ensure_schema() -> bool:
    """Bring the database schema up, then the curated selection, retrying both.

    Two separate phases, because they fail independently. Folding the restore into the
    schema branch made it unreachable the moment the schema succeeded and the restore did
    not: every later call short-circuited on `schema_status["ready"]` and the graph stayed
    held empty forever while /ready reported healthy.

    Returns True only when both phases have succeeded, so /ready stays 503 for a process
    that is up but has no idea which pools it is supposed to route.
    """
    if not schema_status["ready"]:
        async with _schema_lock:
            if not schema_status["ready"]:
                try:
                    await init_db()
                    await migrate_v012()
                    schema_status.update({"ready": True, "last_error": None})
                except Exception as exc:
                    # Do not crash the process (the DB may still be starting), but record the
                    # failure so /ready reports unhealthy instead of silently dropping every
                    # subsequent write.
                    schema_status.update({"ready": False, "last_error": f"{type(exc).__name__}: {exc}"})
                    logger.exception("database schema initialisation failed; /ready will report unhealthy")
                    # Hold the graph empty: without a readable selection, running the full
                    # discovery output is the one outcome that must not happen by default.
                    _hold_graph_empty("database schema unavailable")
                    return False
    if not curated_pool_status.get("loaded"):
        await _restore_curated_pools()
    return bool(schema_status["ready"] and curated_pool_status.get("loaded"))

async def _load_discovered_pools() -> list[PoolMetadata]:
    """Everything discovery has verified and persisted, under the same token allowlist.

    Needed whenever a curated restriction is lifted. Lifting is not enough on its own:
    pools discovery registered while a selection was in force were turned away at the door
    and are not held anywhere in the graph, and discovery's cursors have advanced past
    them -- restore() is idempotent and runs once per process, so nothing re-registers
    them until a restart. Reloading `verified_pools` is what actually puts them back.

    Raises if the registry cannot be read, so callers can refuse the whole operation
    rather than unfiltering onto a graph that is missing most of its pools.
    """
    return [p for p in await pool_registry.load_pools() if pool_discovery.admits(p)]

def _hold_graph_empty(reason: str) -> None:
    """Fail closed: no readable selection means no routable pools.

    A database blip must not be the reason the router starts trading the full 28.6k-pool
    discovery output, so the restriction is installed rather than left absent.
    """
    pool_graph.restrict_to([])
    curated_pool_status.update({
        "restored": 0, "restricted": True, "loaded": False, "last_error": reason,
    })

def _selection_in_force() -> bool:
    """True only when a non-empty curated selection has actually been read from the store.

    Deliberately not `restriction() is not None`. _hold_graph_empty() fails closed by
    installing an *empty* restriction, which passes that test while meaning the opposite:
    we do not know what the selection is. Reading a hold as "a selection is in force" let a
    single /pools/register persist one pool into an otherwise-empty `curated_pools`, and the
    next successful restore read that one row back as the entire selection and collapsed the
    routable graph onto it -- durably, and exactly as a database blip cleared.
    """
    return bool(curated_pool_status.get("loaded")) and bool(pool_graph.restriction())

async def _restore_curated_pools() -> None:
    """Rehydrate the operator-selected pool set and make it a standing filter.

    Two things were broken without this. The graph was empty on boot whenever automatic
    discovery was disabled, so every curated selection had to be re-registered by hand.
    And when discovery *was* enabled -- the default -- it registered every verified pool
    on top of the curated set, so a selection was authoritative for exactly as long as it
    took the next refresh to run. Restoring the selection as a restriction (rather than as
    a batch of registrations) fixes both: discovery keeps discovering and persisting, but
    only curated pools enter the routable graph.

    An empty curated table means "no selection", which leaves discovery unrestricted.
    """
    if not schema_status["ready"]:
        _hold_graph_empty("database schema unavailable")
        return
    # Under the curation lock: a restore retry racing a live /pools/select would otherwise
    # overwrite the newer selection with whatever the database held when the read started.
    async with _curation_lock:
        if curated_pool_status.get("loaded"):
            return
        try:
            restored = await curated_pools.load()
        except Exception as exc:
            _hold_graph_empty(f"{type(exc).__name__}: {exc}")
            logger.exception("curated pool restore failed; graph is held empty until it succeeds")
            return
        if restored:
            pool_graph.replace_all(restored)
        else:
            # No selection: discovery owns the graph. Rebuild it rather than merely
            # unfiltering -- on a retry after a failed restore, discovery's own restore()
            # has already run and had everything filtered out by the fail-closed empty
            # restriction, and it will not run again.
            try:
                pool_graph.replace_all(await _load_discovered_pools(), restrict=False)
            except Exception as exc:
                _hold_graph_empty(f"{type(exc).__name__}: {exc}")
                logger.exception("discovered pool reload failed; graph is held empty")
                return
        curated_pool_status.update({
            "restored": len(restored),
            "restricted": bool(restored),
            "loaded": True,
            "last_error": None,
        })


async def _bootstrap():
    """Schema, curated restore, then discovery -- all off the startup path.

    Schema *and* curated restore, each retried on every later call until it lands. If
    either is unavailable the graph is held empty rather than left unrestricted, and
    /ready reports unhealthy until both recover -- so discovery, started only after
    _ensure_schema() returns, can never populate a graph whose selection is unknown.

    Awaiting this from startup() blocked uvicorn from binding: on the default path
    (an empty `curated_pools`) the restore reads every row of `verified_pools` -- ~28.6k
    -- and rebuilds the graph from it before the socket is up, which is exactly what
    _discovery_loop and scripts/backfill_uniswap_pools.py both say must not happen on the
    bind path. /ready drives the retry, so nothing is lost by deferring it.
    """
    global _pool_discovery_task
    await _ensure_schema()
    if settings.auto_pool_discovery_enabled:
        _pool_discovery_task = asyncio.create_task(_discovery_loop())


@app.on_event("startup")
async def startup():
    # Loud at boot rather than on the first curation request: a token that cannot work is
    # a deployment error, and the operator who set it is not the one who will hit the 503.
    defect = settings.operator_api_token and _operator_token_defect(settings.operator_api_token)
    if defect:
        logger.error(
            "OPERATOR_API_TOKEN %s; every pool-curation request will be refused with 503 "
            "until it is corrected", defect,
        )
    # Fail closed *before* anything can serve: until the restore lands we do not know what
    # the selection is, and an unrestricted graph is the one state that must never be
    # reachable. _restore_curated_pools() lifts this once it knows.
    _hold_graph_empty("curated selection not yet loaded")
    _spawn_background(_bootstrap())

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
        # None = discovery owns the graph; a number = a curated selection is filtering it.
        "curated_selection":graph["restricted_to"],
        "route_optimizer":"enabled",
    }

@app.get("/ready")
async def ready():
    if not await _ensure_schema():
        # Distinguish the two phases: a process whose schema is fine but whose curated
        # selection could not be read is up, healthy-looking, and routing nothing.
        if not schema_status["ready"]:
            raise HTTPException(503, f"database schema unavailable: {schema_status['last_error']}")
        raise HTTPException(
            503,
            "curated pool selection unavailable, graph is held empty: "
            f"{curated_pool_status['last_error']}",
        )
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
            # Which sources are held, so a blocker on one is distinguishable from a scan
            # that is failing outright. Cursors for the rest keep advancing underneath.
            "blocked_sources": dict(pool_discovery_status.get("blocked_sources") or {}),
        },
        "curated_pools": dict(curated_pool_status),
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
        _record_discovery_success(await pool_discovery.refresh())
        return pool_discovery_status
    except Exception as exc:
        _record_discovery_failure(exc)
        raise HTTPException(502, dict(pool_discovery_status)) from exc

async def _refresh_once():
    try:
        _record_discovery_success(await pool_discovery.refresh())
    except Exception as exc:
        _record_discovery_failure(exc)

def _operator_token_defect(token: str) -> str | None:
    """Why a configured OPERATOR_API_TOKEN can never authenticate, or None if it can.

    Both defects here fail the same way without this check: a token the operator set is
    rejected forever with a 401 that is indistinguishable from a wrong one, so the
    misconfiguration looks like a caller problem and the deployment stays locked out of
    its own curation endpoints.

    - Non-ASCII: HTTP header values are ASCII, and clients refuse to encode anything else
      (httpx raises before it sends), so no conforming caller can ever present it.
    - Surrounding whitespace: require_operator strips the presented half but compares
      against the configured value verbatim. `.env` parsing strips trailing spaces, but a
      real environment variable keeps them, so `OPERATOR_API_TOKEN='s3cret '` matches
      nothing a client can send.
    """
    if token != token.strip():
        return (
            "has leading or trailing whitespace, which no client can present "
            "(the header value is stripped before comparison)"
        )
    try:
        token.encode("ascii")
    except UnicodeEncodeError:
        return (
            "contains non-ASCII characters, which cannot be sent in an HTTP "
            "Authorization header"
        )
    return None


def require_operator(authorization: str | None = Header(default=None)) -> None:
    """Gate the endpoints that can rewrite what the router trades.

    /pools/select replaces both the durable curated set and the live graph, /pools/register
    admits a pool into routing, and DELETE removes one. Unauthenticated, any client that
    can reach the port can point route evaluation at pools of its choosing or empty the
    graph and stop trading entirely -- and because the selection is durable, it survives
    the restart an operator would reach for first.

    Fails closed when OPERATOR_API_TOKEN is unset. An open-by-default toggle would leave
    every existing deployment exactly as exposed as it is now, which is the thing being
    fixed; refusing loudly makes the gap impossible to run past unnoticed. Read-only
    endpoints are untouched.
    """
    expected = settings.operator_api_token
    if not expected:
        raise HTTPException(
            503,
            "pool curation is disabled: set OPERATOR_API_TOKEN to a strong shared secret "
            "and send it as `Authorization: Bearer <token>`",
        )
    # 503, not 401: the token is unusable as configured, so this is a server-side
    # misconfiguration rather than a caller who got it wrong. Same reasoning as the
    # unset-token branch above -- say which, so it is fixable without guessing.
    defect = _operator_token_defect(expected)
    if defect:
        raise HTTPException(503, f"pool curation is misconfigured: OPERATOR_API_TOKEN {defect}")
    scheme, _, presented = (authorization or "").partition(" ")
    # compare_digest, not ==: a short-circuiting comparison leaks the token a byte at a
    # time to a caller that can measure response latency.
    #
    # Compared as bytes, not str: the str overload is ASCII-only and raises TypeError on
    # anything else, so a token with a non-ASCII character (or a caller who sends one)
    # turned every curation request into a 500 with no hint about the cause, instead of
    # the 401 it is. Encoding first also makes the comparison length-safe either way.
    if scheme.lower() != "bearer" or not hmac.compare_digest(
        presented.strip().encode("utf-8"), expected.encode("utf-8")
    ):
        raise HTTPException(
            401,
            "operator authentication required",
            headers={"WWW-Authenticate": "Bearer"},
        )


def _metadata_from(pool: PoolRegistration) -> PoolMetadata:
    return PoolMetadata.create(
        address=pool.address,
        token0=pool.token0,
        token1=pool.token1,
        venue=pool.venue,
        pool_type=pool.pool_type,
        fee=pool.fee,
        stable=pool.stable,
        # Slipstream's routing key. Dropping it here produced pools the quoter refused
        # (missing_tick_spacing), i.e. registered-but-unroutable entries.
        tick_spacing=pool.tick_spacing,
    )

@app.post("/pools/register", dependencies=[Depends(require_operator)])
async def register_pool(pool: PoolRegistration):
    """Register pool metadata produced by live discovery; no synthetic metadata is generated here.

    Amends a curated selection when one is in force: the pool is persisted to
    `curated_pools` and the standing restriction widens to admit it, so neither a restart
    nor a discovery refresh can drop it.

    With no selection in force the graph belongs to discovery, and a single registration
    is deliberately *not* persisted. Persisting it would make the curated table non-empty,
    and the next restart would read that as "the selection is this one pool" and collapse
    the graph to it. Establishing a selection is what /pools/select is for.

    A fail-closed hold counts as "no selection in force": the store is unreadable, so the
    registration stays graph-only until the restore lands. See _selection_in_force().
    """
    metadata = _metadata_from(pool)
    # One lock across the store write and the graph mutation: see _curation_lock.
    async with _curation_lock:
        curated = _selection_in_force()
        if curated:
            try:
                await curated_pools.add(metadata)
            except Exception as exc:
                raise HTTPException(503, f"curated pool store unavailable: {type(exc).__name__}: {exc}") from exc
        # admit(), not register(): an operator adding a pool is expressing the selection, so
        # it must widen any standing restriction rather than be filtered out by it.
        pool_graph.admit(metadata)
        if curated:
            curated_pool_status["restored"] = len(pool_graph.restriction() or ())
    return {
        "registered": True,
        "pool": metadata.address,
        # False means graph-only: discovery owns the set and this entry does not survive a
        # restart. POST /pools/select to make a selection durable.
        "curated": curated,
        "graph": pool_graph.snapshot(),
    }

@app.post("/pools/select", dependencies=[Depends(require_operator)])
async def select_pools(pools_in: list[PoolRegistration] = Body(...)):
    """Replace the entire pool set with this selection.

    /pools/register is additive and therefore cannot express "and drop everything else":
    re-curating with a tighter floor still left every earlier pool in the graph. This
    makes a curated selection authoritative -- the durable store and the in-memory graph
    are both swapped, and the store is written first so a crash between the two loses
    nothing that a restart will not restore.
    """
    metadata = [_metadata_from(p) for p in pools_in]
    # One lock across the store write and the graph mutation: see _curation_lock. Without
    # it a /pools/register that commits and admits while this handler is awaiting its own
    # commit is then erased from the graph by the replace below -- but not from Postgres,
    # so the pool reappears at the next restart.
    async with _curation_lock:
        # Read the discovery output *before* committing anything, so a registry that cannot
        # be read fails the request outright instead of clearing the selection and then
        # discovering it has nothing to hand the graph back to.
        discovered = None
        if not metadata:
            try:
                discovered = await _load_discovered_pools()
            except Exception as exc:
                raise HTTPException(
                    503,
                    "cannot lift curation: the discovered-pool registry is unreadable, so "
                    f"the graph could not be rebuilt: {type(exc).__name__}: {exc}",
                ) from exc
        try:
            await curated_pools.replace(metadata)
        except Exception as exc:
            raise HTTPException(503, f"curated pool store unavailable: {type(exc).__name__}: {exc}") from exc
        if metadata:
            # Installs the selection as a standing filter, so the discovery loop -- which is
            # enabled by default and runs every `pool_discovery_refresh_seconds` -- cannot
            # put the de-selected pools back.
            count = pool_graph.replace_all(metadata)
            restricted = True
        else:
            # An empty selection means "stop curating", not "route nothing". Lifting the
            # filter alone would leave the graph holding only whatever the selection held:
            # every pool discovery registered while the filter was up was dropped at the
            # door, and its cursors have advanced past them. Rebuild from verified_pools.
            count = pool_graph.replace_all(discovered or [], restrict=False)
            restricted = False
        curated_pool_status.update({
            "restored": count, "restricted": restricted, "loaded": True, "last_error": None,
        })
    return {
        "selected": count,
        "restricted": restricted,
        "discovery_enabled": bool(pool_discovery_status.get("enabled")),
        "graph": pool_graph.snapshot(),
    }

@app.delete("/pools/{address}", dependencies=[Depends(require_operator)])
async def unregister_pool(address: str):
    """Remove one pool from the graph, and from the curated set when one is in force.

    With a selection in force this is permanent: the pool leaves `curated_pools` and the
    standing restriction narrows, so neither a restart nor a discovery refresh brings it
    back.

    With no selection the graph belongs to discovery and the removal is graph-only, which
    `permanent: false` reports -- but that is *not* the same as temporary. Nothing
    re-registers the pool while the process lives: unregister() narrows the restriction so
    a refresh cannot reinstate it, discovery's cursor is already past it, and restore() is
    idempotent and has run. A restart is what brings it back.
    """
    try:
        key = PoolMetadata.normalize_address(address)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    # One lock across the store write and the graph mutation: see _curation_lock.
    async with _curation_lock:
        curated = _selection_in_force()
        removed_from_store = False
        if curated:
            try:
                removed_from_store = await curated_pools.remove(key)
            except Exception as exc:
                raise HTTPException(503, f"curated pool store unavailable: {type(exc).__name__}: {exc}") from exc
        # With no selection in force the removal is graph-only, so it must not touch the
        # store at all -- reading it would make an ephemeral delete fail with 503 during a
        # database outage, and writing it would seed a one-pool selection for the next boot.
        # Narrows the standing restriction, so a discovery refresh cannot reinstate it.
        removed_from_graph = pool_graph.unregister(key)
        if not removed_from_graph and not removed_from_store:
            raise HTTPException(404, f"pool not registered: {key}")
        if curated:
            curated_pool_status["restored"] = len(pool_graph.restriction() or ())
    return {
        "removed": True,
        "pool": key,
        "removed_from_graph": removed_from_graph,
        "removed_from_store": removed_from_store,
        # True: gone from `curated_pools`, so it stays gone across restarts. False: the
        # removal is graph-only and a restart brings the pool back -- but nothing short of
        # one does, so this is not an "undo on next refresh". See the docstring.
        "permanent": curated,
        "graph": pool_graph.snapshot(),
    }

@app.get("/pools")
def pools():
    return {**pool_graph.snapshot(), "curated": dict(curated_pool_status)}

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
    sent as the submission `route_id` must be exactly this value. It is also forwarded verbatim
    to the external signer, whose policy accepts only a 32-byte hex `route_id`, and it is stored
    in a VARCHAR(96) column -- so this must be a fixed 32-byte commitment, not a concatenation of
    the batch hash and the state fingerprint (which is 133 characters and satisfies neither).
    """
    discriminator = trigger.state_version or str(trigger.state_sequence or trigger.observed_at_unix_ms)
    batch_hash = plan.batch_hash if plan is not None else None
    preimage = f"{batch_hash}:{discriminator}" if batch_hash else f"state:{discriminator}"
    return "0x" + hashlib.sha256(preimage.encode()).hexdigest()


@app.post("/route-trigger")
async def route_trigger(trigger: PendingLogTrigger):
    affected=pool_graph.affected_subgraph(trigger.pool_address)
    evaluations=[]
    budget_exceeded=False
    evaluation_started=perf_counter()
    if affected["known_pool"]:
        # `route_evaluation_budget_seconds` is a hard wall-clock ceiling on the whole pass. The
        # per-cycle `asyncio.wait_for` inside the optimizer bounds each cycle individually, but
        # cycles run concurrently and auto-discovery makes their count unbounded by config, so
        # without this a single trigger can outlive the opportunity it was priced for. Abandoning
        # the entire pass (rather than selecting which in-flight cycles to keep) is deliberate:
        # a partial result would be a biased sample of whichever cycles happened to finish first.
        try:
            evaluations=await asyncio.wait_for(
                route_optimizer.evaluate_affected(
                    affected["affected_pools"], settings.max_route_hops, trigger.block_number
                ),
                timeout=settings.route_evaluation_budget_seconds,
            )
        except asyncio.TimeoutError:
            # Fail closed: no evaluations means no packed batch and no submission. The trigger is
            # still recorded below so the overrun is visible in telemetry and calibration.
            budget_exceeded=True
            evaluations=[]
            metrics.inc("arb_route_evaluation_budget_exceeded_total")
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
                "gas_limit":max((v for v in (packed_batch_plan.estimate_gas_units, packed_batch_plan.simulation_gas_units) if v is not None), default=None),
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
        "reason":("route_evaluation_budget_exceeded" if budget_exceeded
                  else "route_optimizer_evaluated" if affected["known_pool"]
                  else "pool_not_registered"),
        "budget_exceeded":budget_exceeded,
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
