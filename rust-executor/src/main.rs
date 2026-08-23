mod packed;
mod v3_math;
mod v3_pool;
mod sizing;

use axum::{
    extract::State,
    http::StatusCode,
    routing::{get, post},
    Json, Router,
};
use futures_util::{SinkExt, StreamExt};
use serde::{Deserialize, Serialize};
use serde_json::Value;
use sha2::{Digest, Sha256};
use std::{
    collections::{HashMap, HashSet, VecDeque},
    env,
    fs,
    net::SocketAddr,
    os::unix::fs::PermissionsExt,
    path::Path,
    sync::{Arc, OnceLock},
    time::{Duration, Instant, SystemTime, UNIX_EPOCH},
};
use tokio::sync::{Mutex, RwLock, Semaphore};
use tracing::{info, warn};

#[derive(Debug, Serialize, Clone)]
struct PoolStateEntry {
    state_version: String,
    state_sequence: u64,
    block_number: Option<u64>,
    transaction_hash: Option<String>,
    log_index: Option<u64>,
    observed_at_unix_ms: u128,
}

#[derive(Default, Serialize, Clone)]
struct ChainState {
    latest_block: u64,
    ws_connected: bool,
    pending_logs_enabled: bool,
    pending_logs_received: u64,
    flashblock_transactions_received: u64,
    flashblock_transaction_logs_matched: u64,
    duplicate_logs_dropped: u64,
    route_triggers_sent: u64,
    route_trigger_failures: u64,
    route_triggers_dropped_backpressure: u64,
    last_pending_log_unix_ms: Option<u128>,
    last_pool_address: Option<String>,
    last_error: Option<String>,
    global_state_sequence: u64,
    cached_pool_states: usize,
    #[serde(skip)]
    pool_state_cache: HashMap<String, PoolStateEntry>,
}

#[derive(Deserialize)]
struct SubmitRequest {
    score: Score,
    size_percent: u8,
    calldata: String,
    candidate: Value,
}

#[derive(Deserialize)]
struct Score {
    approved: bool,
    expected_value_usd: String,
}


#[derive(Deserialize)]
struct SubmitPlanRequest {
    route_id: String,
    calldata: String,
    gas_limit: Option<u64>,
    deterministic_net_profit_units: String,
    asset: String,
    target_block: Option<u64>,
    opportunity_observed_at_unix_ms: Option<u128>,
    expected_execution_latency_ms: Option<u128>,
    survival_window_ms: Option<u128>,
    inclusion_probability_bps: Option<u128>,
    expected_failure_cost_units: Option<String>,
    safety_margin_units: Option<String>,
    trigger_pool: Option<String>,
    state_version: Option<String>,
    state_sequence: Option<u64>,
}

#[derive(Deserialize)]
struct SignerResponse {
    raw_transaction: String,
}

#[derive(Serialize)]
struct SubmitResponse {
    accepted: bool,
    dry_run: bool,
    submission_id: String,
    message: String,
    provider: String,
    tx_hash: Option<String>,
    latency_ms: u128,
}

#[derive(Debug, Serialize, Clone)]
struct PendingLogTrigger {
    pool_address: String,
    topic0: Option<String>,
    transaction_hash: Option<String>,
    block_number: Option<u64>,
    transaction_index: Option<u64>,
    log_index: Option<u64>,
    observed_at_unix_ms: u128,
    state_version: Option<String>,
    state_sequence: Option<u64>,
    raw_data: String,
    source_sender: Option<String>,
    tx_touched_pools: Vec<String>,
}

struct SeenLogs {
    capacity: usize,
    set: HashSet<String>,
    order: VecDeque<String>,
}

impl SeenLogs {
    fn new(capacity: usize) -> Self {
        Self {
            capacity,
            set: HashSet::with_capacity(capacity),
            order: VecDeque::with_capacity(capacity),
        }
    }

    fn insert_if_new(&mut self, key: String) -> bool {
        if self.set.contains(&key) {
            return false;
        }
        self.set.insert(key.clone());
        self.order.push_back(key);
        while self.order.len() > self.capacity {
            if let Some(old) = self.order.pop_front() {
                self.set.remove(&old);
            }
        }
        true
    }
}

fn now_unix_ms() -> u128 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default()
        .as_millis()
}

fn parse_hex_u64(value: Option<&str>) -> Option<u64> {
    value.and_then(|v| u64::from_str_radix(v.trim_start_matches("0x"), 16).ok())
}

fn csv_env(name: &str) -> Vec<String> {
    env::var(name)
        .unwrap_or_default()
        .split(',')
        .map(|v| v.trim().to_lowercase())
        .filter(|v| !v.is_empty())
        .collect()
}


fn adaptive_expected_capture(
    deterministic_net_profit_units: u128,
    observed_at_unix_ms: u128,
    now_ms: u128,
    expected_execution_latency_ms: u128,
    survival_window_ms: u128,
    inclusion_probability_bps: u128,
    expected_failure_cost_units: u128,
) -> Result<(u128, u128, u128), String> {
    if survival_window_ms == 0 {
        return Err("survival_window_ms must be positive".into());
    }
    if inclusion_probability_bps > 10_000 {
        return Err("inclusion_probability_bps must be <= 10000".into());
    }
    let age_ms = now_ms.saturating_sub(observed_at_unix_ms);
    let total_latency_ms = age_ms.saturating_add(expected_execution_latency_ms);
    let survival_bps = if total_latency_ms >= survival_window_ms {
        0
    } else {
        survival_window_ms
            .saturating_sub(total_latency_ms)
            .saturating_mul(10_000)
            / survival_window_ms
    };
    let after_survival = deterministic_net_profit_units.saturating_mul(survival_bps) / 10_000;
    let after_inclusion = after_survival.saturating_mul(inclusion_probability_bps) / 10_000;
    let expected_capture = after_inclusion.saturating_sub(expected_failure_cost_units);
    Ok((expected_capture, survival_bps, total_latency_ms))
}

fn read_bearer_token_file(path: &Path) -> Result<String, String> {
    let metadata = fs::metadata(path).map_err(|e| format!("signer token metadata: {e}"))?;
    if metadata.permissions().mode() & 0o077 != 0 {
        return Err("signer token file must not be group/world accessible".into());
    }
    let token = fs::read_to_string(path)
        .map_err(|e| format!("signer token read: {e}"))?
        .trim()
        .to_string();
    if token.len() < 32 {
        return Err("signer token must contain at least 32 characters".into());
    }
    Ok(token)
}

fn signer_bearer_token() -> Result<Option<String>, String> {
    let Some(path) = env::var("EXTERNAL_SIGNER_BEARER_TOKEN_FILE")
        .ok()
        .filter(|value| !value.trim().is_empty())
    else {
        return Ok(None);
    };
    read_bearer_token_file(Path::new(&path)).map(Some)
}

fn watched_pool_set() -> HashSet<String> {
    csv_env("WATCHED_POOL_ADDRESSES").into_iter().collect()
}

fn watched_topic_set() -> HashSet<String> {
    csv_env("WATCHED_TOPIC0S").into_iter().collect()
}

fn decode_flashblock_transaction_triggers(
    result: &Value,
    watched: &HashSet<String>,
    watched_topics: &HashSet<String>,
) -> Vec<PendingLogTrigger> {
    if watched.is_empty() {
        return Vec::new();
    }
    let tx_hash = result.get("hash").and_then(Value::as_str).map(str::to_lowercase);
    let source_sender = result.get("from").and_then(Value::as_str).map(str::to_lowercase);
    let block_number = parse_hex_u64(result.get("blockNumber").and_then(Value::as_str));
    let tx_index = parse_hex_u64(result.get("transactionIndex").and_then(Value::as_str));
    let observed = now_unix_ms();
    let mut touched: Vec<String> = result.get("logs").and_then(Value::as_array).into_iter().flatten()
        .filter_map(|log| log.get("address").and_then(Value::as_str).map(str::to_lowercase))
        .filter(|address| watched.contains(address))
        .collect();
    touched.sort();
    touched.dedup();
    result
        .get("logs")
        .and_then(Value::as_array)
        .into_iter()
        .flatten()
        .filter_map(|log| {
            let address = log.get("address")?.as_str()?.to_lowercase();
            if !watched.contains(&address) {
                return None;
            }
            let topic0 = log
                .get("topics")
                .and_then(Value::as_array)
                .and_then(|items| items.first())
                .and_then(Value::as_str)
                .map(str::to_lowercase);
            if !watched_topics.is_empty()
                && !topic0.as_ref().map(|t| watched_topics.contains(t)).unwrap_or(false)
            {
                return None;
            }
            Some(PendingLogTrigger {
                pool_address: address,
                topic0,
                transaction_hash: log
                    .get("transactionHash")
                    .and_then(Value::as_str)
                    .map(str::to_lowercase)
                    .or_else(|| tx_hash.clone()),
                block_number: parse_hex_u64(log.get("blockNumber").and_then(Value::as_str)).or(block_number),
                transaction_index: parse_hex_u64(log.get("transactionIndex").and_then(Value::as_str)).or(tx_index),
                log_index: parse_hex_u64(log.get("logIndex").and_then(Value::as_str)),
                observed_at_unix_ms: observed,
                state_version: None,
                state_sequence: None,
                raw_data: log.get("data").and_then(Value::as_str).unwrap_or("0x").to_string(),
                source_sender: source_sender.clone(),
                tx_touched_pools: touched.clone(),
            })
        })
        .collect()
}

fn valid_address(value: &str) -> bool {
    value.len() == 42
        && value.starts_with("0x")
        && value[2..].chars().all(|c| c.is_ascii_hexdigit())
}

fn build_pending_logs_params_for(addresses: &[String], topic0s: &[String]) -> Result<Option<Value>, String> {
    let allow_unfiltered = env::var("ALLOW_UNFILTERED_PENDING_LOGS")
        .unwrap_or_default()
        .eq_ignore_ascii_case("true");

    for address in addresses {
        if !valid_address(address) {
            return Err(format!("invalid watched pool address: {address}"));
        }
    }
    for topic in topic0s {
        if topic.len() != 66 || !topic.starts_with("0x") || !topic[2..].chars().all(|c| c.is_ascii_hexdigit()) {
            return Err(format!("invalid WATCHED_TOPIC0S entry: {topic}"));
        }
    }
    if addresses.is_empty() && !allow_unfiltered {
        return Ok(None);
    }
    let mut filter = serde_json::Map::new();
    if !addresses.is_empty() {
        filter.insert("address".into(), serde_json::json!(addresses));
    }
    if !topic0s.is_empty() {
        filter.insert("topics".into(), serde_json::json!([topic0s]));
    }
    Ok(Some(serde_json::json!(["pendingLogs", Value::Object(filter)])))
}

/// Short-timeout client for the pool registry. This call is made from the websocket message
/// loop, so an unbounded request would stall every pending-log trigger behind a slow or hung
/// Python API (the ws idle timeout only guards `socket.next()`, not our own awaits).
fn registry_client() -> &'static reqwest::Client {
    static REGISTRY_CLIENT: OnceLock<reqwest::Client> = OnceLock::new();
    REGISTRY_CLIENT.get_or_init(|| {
        reqwest::Client::builder()
            .timeout(registry_fetch_budget())
            .build()
            .expect("failed to build pool registry client")
    })
}

fn registry_fetch_budget() -> Duration {
    let ms: u64 = env::var("POOL_REGISTRY_TIMEOUT_MS")
        .ok()
        .and_then(|v| v.parse().ok())
        .filter(|v: &u64| *v > 0 && *v <= 30_000)
        .unwrap_or(2_000);
    Duration::from_millis(ms)
}

async fn load_watched_pools() -> HashSet<String> {
    let mut watched = watched_pool_set();
    let enabled = env::var("AUTO_POOL_DISCOVERY_ENABLED")
        .unwrap_or_else(|_| "true".into())
        .eq_ignore_ascii_case("true");
    if !enabled {
        return watched;
    }
    let url = env::var("PYTHON_POOL_REGISTRY_URL")
        .unwrap_or_else(|_| "http://python-api:8080/pools/addresses".into());
    match registry_client().get(url).send().await {
        Ok(resp) if resp.status().is_success() => {
            match resp.json::<Value>().await {
                Ok(body) => {
                    if let Some(items) = body.get("addresses").and_then(Value::as_array) {
                        for item in items.iter().filter_map(Value::as_str) {
                            let addr = item.to_lowercase();
                            if valid_address(&addr) {
                                watched.insert(addr);
                            }
                        }
                    }
                }
                Err(e) => warn!(%e, "failed to decode dynamic pool registry"),
            }
        }
        Ok(resp) => warn!(status=%resp.status(), "dynamic pool registry returned non-success"),
        Err(e) => warn!(%e, "dynamic pool registry unavailable; using manual watched pools only"),
    }
    watched
}

fn decode_pending_log(result: &Value) -> Option<PendingLogTrigger> {
    let address = result.get("address")?.as_str()?.to_lowercase();
    if !valid_address(&address) {
        return None;
    }
    let topics = result.get("topics").and_then(Value::as_array);
    let topic0 = topics
        .and_then(|items| items.first())
        .and_then(Value::as_str)
        .map(str::to_lowercase);
    Some(PendingLogTrigger {
        pool_address: address.clone(),
        topic0,
        transaction_hash: result
            .get("transactionHash")
            .and_then(Value::as_str)
            .map(str::to_lowercase),
        block_number: parse_hex_u64(result.get("blockNumber").and_then(Value::as_str)),
        transaction_index: parse_hex_u64(result.get("transactionIndex").and_then(Value::as_str)),
        log_index: parse_hex_u64(result.get("logIndex").and_then(Value::as_str)),
        observed_at_unix_ms: now_unix_ms(),
        state_version: None,
        state_sequence: None,
        raw_data: result
            .get("data")
            .and_then(Value::as_str)
            .unwrap_or("0x")
            .to_string(),
        source_sender: None,
        tx_touched_pools: vec![address],
    })
}

fn dedup_key(trigger: &PendingLogTrigger) -> String {
    format!(
        "{}:{}:{}:{}",
        trigger.transaction_hash.as_deref().unwrap_or("none"),
        trigger.log_index.unwrap_or(u64::MAX),
        trigger.block_number.unwrap_or(u64::MAX),
        trigger.pool_address
    )
}

/// One process-wide RPC client. Building a `reqwest::Client` per call throws away the
/// connection pool, so every JSON-RPC round trip would pay a fresh TCP + TLS handshake --
/// unacceptable on a submission path that budgets in hundreds of milliseconds.
fn rpc_client() -> &'static reqwest::Client {
    static RPC_CLIENT: OnceLock<reqwest::Client> = OnceLock::new();
    RPC_CLIENT.get_or_init(|| {
        let timeout_ms: u64 = env::var("RPC_HTTP_TIMEOUT_MS").ok().and_then(|v| v.parse().ok()).unwrap_or(8000);
        reqwest::Client::builder()
            .timeout(Duration::from_millis(timeout_ms))
            .pool_idle_timeout(Duration::from_secs(20))
            .build()
            .expect("failed to build shared RPC client")
    })
}

async fn rpc_call(url: &str, method: &str, params: Value) -> Result<Value, String> {
    let body = serde_json::json!({"jsonrpc":"2.0","id":1,"method":method,"params":params});
    rpc_client()
        .post(url)
        .json(&body)
        .send()
        .await
        .map_err(|e| e.to_string())?
        .json::<Value>()
        .await
        .map_err(|e| e.to_string())
}

fn rpc_result_value(body: &Value) -> Result<&Value, String> {
    if let Some(err) = body.get("error") {
        return Err(err.to_string());
    }
    body.get("result").ok_or_else(|| "missing JSON-RPC result".to_string())
}

fn rpc_hex_u128(body: &Value) -> Result<u128, String> {
    let value = rpc_result_value(body)?
        .as_str()
        .ok_or_else(|| "JSON-RPC hex result is not a string".to_string())?;
    u128::from_str_radix(value.trim_start_matches("0x"), 16).map_err(|e| e.to_string())
}


fn configured_rpc_urls() -> Vec<String> {
    let mut urls: Vec<String> = env::var("BASE_HTTP_RPCS")
        .unwrap_or_default()
        .split(',')
        .map(|v| v.trim().to_string())
        .filter(|v| !v.is_empty())
        .collect();
    if urls.is_empty() {
        urls.push(env::var("BASE_HTTP_RPC").unwrap_or_else(|_| "https://mainnet.base.org".into()));
    }
    // `Vec::dedup` only collapses *adjacent* duplicates, so `a,b,a` would keep two copies of
    // `a` and let the consensus check "agree with itself" against a single endpoint.
    let mut seen = HashSet::new();
    urls.retain(|u| seen.insert(u.clone()));
    urls
}

fn dry_run_enabled() -> bool {
    env::var("DRY_RUN").unwrap_or_else(|_| "true".into()).eq_ignore_ascii_case("true")
}

fn consensus_requested() -> bool {
    env::var("RPC_CONSENSUS_REQUIRED").unwrap_or_else(|_| "true".into()).eq_ignore_ascii_case("true")
}

fn rpc_probe_timeout() -> Duration {
    let secs: f64 = env::var("RPC_PROBE_TIMEOUT_SECONDS")
        .ok()
        .and_then(|v| v.parse().ok())
        .filter(|v: &f64| *v > 0.0 && *v <= 60.0)
        .unwrap_or(2.5);
    Duration::from_millis((secs * 1000.0) as u64)
}

#[derive(Debug, Clone)]
struct RpcProbe {
    url: String,
    latency_ms: u128,
    chain_id: u128,
    block_number: u128,
}

async fn probe_rpc(url: String) -> Result<RpcProbe, String> {
    let started = Instant::now();
    // Issued concurrently: this probe runs on the submission hot path, and the adaptive gate
    // below charges its own elapsed time against `survival_window_ms`, so a second serialised
    // round trip is survival budget spent for nothing.
    let probe = async {
        let (chain, block) = tokio::join!(
            rpc_call(&url, "eth_chainId", serde_json::json!([])),
            rpc_call(&url, "eth_blockNumber", serde_json::json!([])),
        );
        let chain_id = rpc_hex_u128(&chain?)?;
        let block_number = rpc_hex_u128(&block?)?;
        Ok::<(u128, u128), String>((chain_id, block_number))
    };
    let (chain_id, block_number) = tokio::time::timeout(rpc_probe_timeout(), probe)
        .await
        .map_err(|_| "RPC probe timed out".to_string())??;
    Ok(RpcProbe { url, latency_ms: started.elapsed().as_millis(), chain_id, block_number })
}

async fn select_rpc_endpoints(expected_chain: u128) -> Result<Vec<RpcProbe>, String> {
    let urls = configured_rpc_urls();
    let settled = futures_util::future::join_all(
        urls.iter().cloned().map(|url| async move {
            let result = probe_rpc(url.clone()).await;
            (url, result)
        }),
    )
    .await;
    let mut probes = Vec::new();
    for (url, result) in settled {
        match result {
            Ok(p) if p.chain_id == expected_chain => probes.push(p),
            Ok(p) => warn!(url=%p.url, chain_id=p.chain_id, "discarding RPC on wrong chain"),
            Err(e) => warn!(url=%url, %e, "RPC probe failed"),
        }
    }
    if probes.is_empty() {
        return Err("no healthy Base HTTP RPC endpoints".into());
    }
    probes.sort_by_key(|p| p.latency_ms);
    if consensus_requested() {
        if probes.len() < 2 {
            // The shipped default ships one endpoint (BASE_HTTP_RPCS empty -> BASE_HTTP_RPC).
            // Hard-failing here would 502 every submission out of the box, so only live
            // signing -- where the failure mode costs real money -- refuses to proceed.
            if !dry_run_enabled() {
                return Err(format!(
                    "RPC_CONSENSUS_REQUIRED=true and DRY_RUN=false, but only {} healthy Base RPC endpoint(s) are configured; \
                     set BASE_HTTP_RPCS to two or more independent providers, or set RPC_CONSENSUS_REQUIRED=false to accept single-provider state",
                    probes.len()
                ));
            }
            warn!(
                healthy = probes.len(),
                "RPC_CONSENSUS_REQUIRED=true but fewer than two healthy Base RPCs; \
                 continuing unverified because DRY_RUN=true (live submission will refuse)"
            );
            return Ok(probes);
        }
        let min_block = probes.iter().map(|p| p.block_number).min().unwrap_or(0);
        let max_block = probes.iter().map(|p| p.block_number).max().unwrap_or(0);
        if max_block.saturating_sub(min_block) > 1 {
            return Err(format!("RPC block disagreement too large: min={min_block} max={max_block}"));
        }
    }
    Ok(probes)
}

/// Simulate against the primary endpoint at `tag` (normally `pending`), and — when consensus is
/// required — cross-check a *second* endpoint at a **fixed block tag**.
///
/// Pending state is inherently provider-local: each node has its own mempool view and its own
/// Flashblock tip, so byte-comparing two providers' `pending` results rejects nearly every real
/// opportunity. Agreement is only meaningful on a block both nodes have actually sealed, so the
/// cross-check runs against `consensus_block` (the highest block all probes have in common).
async fn consensus_eth_call(probes: &[RpcProbe], tx: Value, tag: &str) -> Result<Value, String> {
    let primary = probes.first().ok_or_else(|| "no healthy RPC endpoint".to_string())?;
    let body = rpc_call(&primary.url, "eth_call", serde_json::json!([tx.clone(), tag])).await?;
    let primary_value = rpc_result_value(&body)?.clone();

    if !consensus_requested() || probes.len() < 2 {
        return Ok(primary_value);
    }

    // Highest block every probe reported, so both sides address identical, already-sealed state.
    let consensus_block = probes.iter().map(|p| p.block_number).min().unwrap_or(0);
    let block_tag = format!("0x{consensus_block:x}");
    let mut agreed: Option<Value> = None;
    for p in probes.iter().take(2) {
        let body = rpc_call(&p.url, "eth_call", serde_json::json!([tx.clone(), block_tag])).await?;
        let value = rpc_result_value(&body)?.clone();
        match agreed {
            Some(ref expected) if expected != &value => {
                return Err(format!(
                    "critical eth_call disagreed across RPC providers at block {consensus_block}"
                ));
            }
            Some(_) => {}
            None => agreed = Some(value),
        }
    }
    // The submitted transaction is still governed by the `tag` simulation; the fixed-block
    // agreement only proves the providers describe the same chain.
    Ok(primary_value)
}

fn stamp_pool_state(state: &mut ChainState, trigger: &mut PendingLogTrigger) {
    state.global_state_sequence = state.global_state_sequence.saturating_add(1);
    let seq = state.global_state_sequence;
    let mut h = Sha256::new();
    h.update(trigger.pool_address.as_bytes());
    h.update(trigger.transaction_hash.as_deref().unwrap_or("none").as_bytes());
    h.update(trigger.block_number.unwrap_or_default().to_be_bytes());
    h.update(trigger.log_index.unwrap_or_default().to_be_bytes());
    h.update(trigger.raw_data.as_bytes());
    h.update(seq.to_be_bytes());
    let version = format!("0x{}", hex::encode(h.finalize()));
    trigger.state_version = Some(version.clone());
    trigger.state_sequence = Some(seq);
    state.pool_state_cache.insert(trigger.pool_address.clone(), PoolStateEntry {
        state_version: version,
        state_sequence: seq,
        block_number: trigger.block_number,
        transaction_hash: trigger.transaction_hash.clone(),
        log_index: trigger.log_index,
        observed_at_unix_ms: trigger.observed_at_unix_ms,
    });
    state.cached_pool_states = state.pool_state_cache.len();
}

fn validate_state_version(state: &ChainState, req: &SubmitPlanRequest) -> Result<(), String> {
    let (Some(trigger_pool), Some(version), Some(sequence)) = (&req.trigger_pool, &req.state_version, req.state_sequence) else {
        return Err("state-versioned submission fields are required".into());
    };
    // Deliberately NOT compared against `global_state_sequence`: that counter advances on every
    // watched log across every pool, so an unrelated pool's event between trigger and submission
    // would reject a route whose own state is untouched. The trigger pool's fingerprint is the
    // guard that actually detects the state this route was priced against having moved.
    let key = trigger_pool.to_lowercase();
    let entry = state.pool_state_cache.get(&key).ok_or_else(|| "trigger pool not present in local state cache".to_string())?;
    if entry.state_version != *version || entry.state_sequence != sequence {
        return Err(format!(
            "trigger pool state fingerprint changed before submission: observed_seq={sequence} current_seq={}",
            entry.state_sequence
        ));
    }
    Ok(())
}

fn erc20_balance_of_calldata(account: &str) -> Result<String, String> {
    if !valid_address(account) { return Err("invalid balanceOf account".into()); }
    Ok(format!("0x70a08231{:0>64}", account.trim_start_matches("0x")))
}

async fn erc20_balance(url: &str, token: &str, account: &str, tag: &str) -> Result<u128, String> {
    let data = erc20_balance_of_calldata(account)?;
    let body = rpc_call(url, "eth_call", serde_json::json!([{"to":token,"data":data},tag])).await?;
    rpc_hex_u128(&body)
}

async fn wait_for_receipt(url: &str, tx_hash: &str) -> Result<Value, String> {
    let timeout_secs: u64 = env::var("RECEIPT_TIMEOUT_SECONDS").ok().and_then(|v| v.parse().ok()).unwrap_or(45);
    let poll_ms: u64 = env::var("RECEIPT_POLL_MS").ok().and_then(|v| v.parse().ok()).unwrap_or(1000);
    let started = Instant::now();
    loop {
        let body = rpc_call(url, "eth_getTransactionReceipt", serde_json::json!([tx_hash])).await?;
        let result = rpc_result_value(&body)?;
        if !result.is_null() { return Ok(result.clone()); }
        if started.elapsed() >= Duration::from_secs(timeout_secs) { return Err("receipt timeout".into()); }
        tokio::time::sleep(Duration::from_millis(poll_ms)).await;
    }
}

async fn post_reconciliation(opportunity_id: &str, payload: Value) {
    let base = env::var("PYTHON_TELEMETRY_RECONCILE_URL").unwrap_or_else(|_| "http://python-api:8080/telemetry/reconcile".into());
    let mut body = payload.as_object().cloned().unwrap_or_default();
    body.insert("opportunity_id".into(), Value::String(opportunity_id.to_string()));
    if let Err(e) = rpc_client().post(base).json(&Value::Object(body)).send().await {
        warn!(%e, "failed to post receipt reconciliation");
    }
}

async fn submit_plan(State(state): State<Arc<RwLock<ChainState>>>, Json(req): Json<SubmitPlanRequest>) -> Result<Json<SubmitResponse>, (StatusCode, String)> {
    let start = Instant::now();
    if !req.calldata.starts_with("0x") || req.calldata.len() < 10 {
        return Err((StatusCode::BAD_REQUEST, "calldata must be non-empty hex".into()));
    }
    if !valid_address(&req.asset) {
        return Err((StatusCode::BAD_REQUEST, "asset must be an EVM address".into()));
    }
    let dry = dry_run_enabled();
    let expected_chain: u128 = env::var("CHAIN_ID").ok().and_then(|v| v.parse().ok()).unwrap_or(8453);
    let probes = select_rpc_endpoints(expected_chain).await.map_err(|e| (StatusCode::BAD_GATEWAY, e))?;
    let rpc = probes[0].url.clone();
    let provider = format!("rpc:{}ms", probes[0].latency_ms);
    validate_state_version(&*state.read().await, &req).map_err(|e| (StatusCode::GONE, e))?;
    let executor = env::var("EXECUTOR_ADDRESS").unwrap_or_default().to_lowercase();
    let owner = env::var("EXECUTOR_OWNER_ADDRESS").unwrap_or_default().to_lowercase();
    if !valid_address(&executor) || !valid_address(&owner) {
        return Err((StatusCode::PRECONDITION_FAILED, "EXECUTOR_ADDRESS and EXECUTOR_OWNER_ADDRESS are required".into()));
    }

    let mut h = Sha256::new();
    h.update(req.route_id.as_bytes());
    h.update(req.calldata.as_bytes());
    h.update(req.deterministic_net_profit_units.as_bytes());
    let id = hex::encode(&h.finalize()[..12]);

    if let Some(target) = req.target_block {
        if let Ok(body) = rpc_call(&rpc, "eth_blockNumber", serde_json::json!([])).await {
            if let Ok(latest) = rpc_hex_u128(&body) {
                // A transaction sent while `eth_blockNumber == latest` can be included no
                // earlier than `latest + 1`, so it can satisfy the executor's
                // `block.number <= targetBlock` only when `latest < target`. Rejecting only a
                // strictly-past target let an unsatisfiable `latest == target` through to the
                // exact pending-state simulation instead of failing here.
                if latest >= target as u128 {
                    return Err((StatusCode::GONE, "route target block already passed".into()));
                }
            }
        }
    }

    if let (Some(observed), Some(expected_latency), Some(window), Some(inclusion_bps), Some(failure_cost), Some(safety_margin)) = (
        req.opportunity_observed_at_unix_ms,
        req.expected_execution_latency_ms,
        req.survival_window_ms,
        req.inclusion_probability_bps,
        req.expected_failure_cost_units.as_deref(),
        req.safety_margin_units.as_deref(),
    ) {
        let deterministic = req.deterministic_net_profit_units.parse::<u128>()
            .map_err(|_| (StatusCode::BAD_REQUEST, "deterministic_net_profit_units must fit uint128 for adaptive gate".into()))?;
        let failure = failure_cost.parse::<u128>()
            .map_err(|_| (StatusCode::BAD_REQUEST, "expected_failure_cost_units must fit uint128".into()))?;
        let margin = safety_margin.parse::<u128>()
            .map_err(|_| (StatusCode::BAD_REQUEST, "safety_margin_units must fit uint128".into()))?;
        let (capture, survival_bps, total_latency_ms) = adaptive_expected_capture(
            deterministic, observed, now_unix_ms(), expected_latency, window, inclusion_bps, failure,
        ).map_err(|e| (StatusCode::BAD_REQUEST, e))?;
        if capture <= margin {
            return Err((StatusCode::UNPROCESSABLE_ENTITY, format!(
                "adaptive submission gate rejected: expected_capture={capture} threshold={margin} survival_bps={survival_bps} total_latency_ms={total_latency_ms}"
            )));
        }
    }

    // Re-simulate the exact final calldata immediately before either dry-run acceptance or live signing.
    // This is deliberately inside the Rust submission boundary so Python cannot bypass it.
    let sim_result = consensus_eth_call(
        &probes,
        serde_json::json!({"from":owner,"to":executor,"data":req.calldata,"value":"0x0"}),
        "pending",
    ).await.map_err(|e| (StatusCode::UNPROCESSABLE_ENTITY, e))?;
    validate_state_version(&*state.read().await, &req).map_err(|e| (StatusCode::GONE, e))?;

    if dry {
        return Ok(Json(SubmitResponse {
            accepted: true,
            dry_run: true,
            submission_id: id,
            message: format!("exact pending-state executor eth_call passed: {}", sim_result.as_str().unwrap_or("0x")),
            provider,
            tx_hash: None,
            latency_ms: start.elapsed().as_millis(),
        }));
    }

    if !env::var("LIVE_TRADING").unwrap_or_default().eq_ignore_ascii_case("true") {
        return Err((StatusCode::PRECONDITION_FAILED, "LIVE_TRADING must be explicitly true".into()));
    }
    if env::var("LIVE_TRADING_ACK").unwrap_or_default() != "I_UNDERSTAND_MAINNET_RISK" {
        return Err((StatusCode::PRECONDITION_FAILED, "set LIVE_TRADING_ACK=I_UNDERSTAND_MAINNET_RISK".into()));
    }
    let signer_url = env::var("EXTERNAL_SIGNER_URL").unwrap_or_default();
    let signer_address = env::var("EXTERNAL_SIGNER_ADDRESS").unwrap_or_default().to_lowercase();
    if signer_url.is_empty() || !valid_address(&signer_address) || signer_address != owner {
        return Err((StatusCode::PRECONDITION_FAILED, "external signer URL/address must be configured and match EXECUTOR_OWNER_ADDRESS".into()));
    }
    let gas_limit = req.gas_limit.ok_or_else(|| (StatusCode::PRECONDITION_FAILED, "gas_limit required for live submission".into()))?;
    validate_state_version(&*state.read().await, &req).map_err(|e| (StatusCode::GONE, e))?;
    let asset_balance_before = erc20_balance(&rpc, &req.asset, &executor, "pending").await
        .map_err(|e| (StatusCode::BAD_GATEWAY, format!("pre-submit asset balance failed: {e}")))?;

    let nonce_body = rpc_call(&rpc, "eth_getTransactionCount", serde_json::json!([signer_address,"pending"]))
        .await.map_err(|e| (StatusCode::BAD_GATEWAY, e))?;
    let nonce = rpc_hex_u128(&nonce_body).map_err(|e| (StatusCode::BAD_GATEWAY, e))?;
    let priority_body = rpc_call(&rpc, "eth_maxPriorityFeePerGas", serde_json::json!([]))
        .await.map_err(|e| (StatusCode::BAD_GATEWAY, e))?;
    let priority = rpc_hex_u128(&priority_body).map_err(|e| (StatusCode::BAD_GATEWAY, e))?;
    let block_body = rpc_call(&rpc, "eth_getBlockByNumber", serde_json::json!(["pending",false]))
        .await.map_err(|e| (StatusCode::BAD_GATEWAY, e))?;
    let block = rpc_result_value(&block_body).map_err(|e| (StatusCode::BAD_GATEWAY, e))?;
    let base_fee_hex = block.get("baseFeePerGas").and_then(Value::as_str)
        .ok_or_else(|| (StatusCode::BAD_GATEWAY, "pending block missing baseFeePerGas".into()))?;
    let base_fee = u128::from_str_radix(base_fee_hex.trim_start_matches("0x"),16)
        .map_err(|e| (StatusCode::BAD_GATEWAY, e.to_string()))?;
    let max_fee = base_fee.saturating_add(priority);

    let chain_id: u64 = env::var("CHAIN_ID").ok().and_then(|v| v.parse().ok()).unwrap_or(8453);
    let signer_payload = serde_json::json!({
        "chain_id": chain_id,
        "from": signer_address,
        "to": executor,
        "nonce": format!("0x{:x}", nonce),
        "gas": format!("0x{:x}", gas_limit),
        "max_fee_per_gas": format!("0x{:x}", max_fee),
        "max_priority_fee_per_gas": format!("0x{:x}", priority),
        "value": "0x0",
        "data": req.calldata,
        "route_id": req.route_id,
        "asset": req.asset,
    });
    let mut signer_request = reqwest::Client::new().post(&signer_url).json(&signer_payload);
    // signer-gateway authenticates every request; omitting this header makes live signing 401.
    if let Some(token) = signer_bearer_token().map_err(|e| (StatusCode::PRECONDITION_FAILED, e))? {
        signer_request = signer_request.bearer_auth(token);
    }
    let signer_response = signer_request.send().await
        .map_err(|e| (StatusCode::BAD_GATEWAY, e.to_string()))?;
    if !signer_response.status().is_success() {
        return Err((StatusCode::BAD_GATEWAY, format!("external signer returned {}", signer_response.status())));
    }
    let signed: SignerResponse = signer_response.json().await
        .map_err(|e| (StatusCode::BAD_GATEWAY, e.to_string()))?;
    if !signed.raw_transaction.starts_with("0x") {
        return Err((StatusCode::BAD_GATEWAY, "external signer returned invalid raw_transaction".into()));
    }
    let send_body = rpc_call(&rpc, "eth_sendRawTransaction", serde_json::json!([signed.raw_transaction]))
        .await.map_err(|e| (StatusCode::BAD_GATEWAY, e))?;
    let tx_hash = rpc_result_value(&send_body).map_err(|e| (StatusCode::BAD_GATEWAY, e))?
        .as_str().ok_or_else(|| (StatusCode::BAD_GATEWAY, "eth_sendRawTransaction did not return tx hash".into()))?.to_string();

    let opportunity_id = req.route_id.clone();
    let receipt_rpc = rpc.clone();
    let receipt_tx = tx_hash.clone();
    let receipt_asset = req.asset.clone();
    let receipt_executor = executor.clone();
    tokio::spawn(async move {
        match wait_for_receipt(&receipt_rpc, &receipt_tx).await {
            Ok(receipt) => {
                let status = parse_hex_u64(receipt.get("status").and_then(Value::as_str)).unwrap_or(0);
                let gas_used = u128::from_str_radix(receipt.get("gasUsed").and_then(Value::as_str).unwrap_or("0x0").trim_start_matches("0x"),16).unwrap_or(0);
                let effective = u128::from_str_radix(receipt.get("effectiveGasPrice").and_then(Value::as_str).unwrap_or("0x0").trim_start_matches("0x"),16).unwrap_or(0);
                let l1_fee = u128::from_str_radix(receipt.get("l1Fee").and_then(Value::as_str).unwrap_or("0x0").trim_start_matches("0x"),16).unwrap_or(0);
                let native_fee = gas_used.saturating_mul(effective).saturating_add(l1_fee);
                // Pin both sides of the delta to the transaction's own block. Reading `after` at
                // "latest" and `before` at pre-submit "pending" attributes every unrelated
                // balance movement in between to this trade, which poisons the realized-PnL
                // series the shadow policy calibrates on. Fall back to the pre-submit reading
                // only when the historical state is unavailable (non-archive provider).
                let receipt_block = parse_hex_u64(receipt.get("blockNumber").and_then(Value::as_str));
                let (before_tag, after_tag) = match receipt_block {
                    Some(n) => (format!("0x{:x}", n.saturating_sub(1)), format!("0x{n:x}")),
                    None => ("pending".to_string(), "latest".to_string()),
                };
                let after = erc20_balance(&receipt_rpc, &receipt_asset, &receipt_executor, &after_tag)
                    .await
                    .unwrap_or(asset_balance_before);
                let before = erc20_balance(&receipt_rpc, &receipt_asset, &receipt_executor, &before_tag)
                    .await
                    .unwrap_or(asset_balance_before);
                // Signed on purpose: `saturating_sub` would report every losing trade as a
                // flat 0, and the shadow-policy learning loop cannot tell break-even from a
                // loss it must stop repeating. Matches telemetry.py::reconcile_receipt_economics.
                let profit = (after as i128) - (before as i128);
                let wrapped_native = env::var("BASE_WETH").unwrap_or_default().eq_ignore_ascii_case(&receipt_asset);
                let net = if wrapped_native { Some(profit - (native_fee as i128)) } else { None };
                post_reconciliation(&opportunity_id, serde_json::json!({
                    "tx_hash":receipt_tx,"receipt_status":status,"gas_used":gas_used.to_string(),
                    "effective_gas_price_wei":effective.to_string(),"l1_fee_wei":l1_fee.to_string(),
                    "actual_native_fee_wei":native_fee.to_string(),"asset_balance_before":before.to_string(),
                    "asset_balance_after":after.to_string(),"realized_profit_units_ex_gas":profit.to_string(),
                    "realized_net_profit_units":net.map(|v| v.to_string()),"net_profit_exact":wrapped_native,
                    "failure_reason": if status == 1 { Value::Null } else { Value::String("transaction reverted".into()) }
                })).await;
            }
            Err(e) => post_reconciliation(&opportunity_id, serde_json::json!({"tx_hash":receipt_tx,"failure_reason":e})).await,
        }
    });

    Ok(Json(SubmitResponse {
        accepted: true,
        dry_run: false,
        submission_id: id,
        message: "signed transaction submitted through external signer".into(),
        provider,
        tx_hash: Some(tx_hash),
        latency_ms: start.elapsed().as_millis(),
    }))
}

async fn pack_batch(Json(req): Json<packed::PackedBatchRequest>) -> Result<Json<packed::PackedBatchResponse>, (StatusCode, String)> {
    let raw = packed::encode_batch(&req).map_err(|e| (StatusCode::BAD_REQUEST, e))?;
    packed::validate_batch_bytes(&raw).map_err(|e| (StatusCode::BAD_REQUEST, e))?;
    Ok(Json(packed::PackedBatchResponse {
        packed: format!("0x{}", hex::encode(&raw)),
        bytes_len: raw.len(),
        candidate_count: req.candidates.len(),
    }))
}

async fn health(State(state): State<Arc<RwLock<ChainState>>>) -> Json<Value> {
    let s = state.read().await.clone();
    Json(serde_json::json!({"status":"ok","chain":s}))
}

async fn submit(Json(req): Json<SubmitRequest>) -> Result<Json<SubmitResponse>, (StatusCode, String)> {
    let start = Instant::now();
    if !req.score.approved {
        return Err((
            StatusCode::UNPROCESSABLE_ENTITY,
            "strategy gate rejected route".into(),
        ));
    }
    if req.size_percent == 0 || req.size_percent > 100 {
        return Err((StatusCode::BAD_REQUEST, "invalid size_percent".into()));
    }
    if !req.calldata.starts_with("0x") {
        return Err((StatusCode::BAD_REQUEST, "calldata must be hex".into()));
    }

    let dry = dry_run_enabled();
    let mut h = Sha256::new();
    h.update(serde_json::to_vec(&req.candidate).unwrap_or_default());
    h.update(req.score.expected_value_usd.as_bytes());
    let id = hex::encode(&h.finalize()[..12]);
    let rpc = env::var("BASE_HTTP_RPC").unwrap_or_else(|_| "https://mainnet.base.org".into());
    let provider = env::var("SUBMISSION_PROVIDER_NAME").unwrap_or_else(|_| "base-rpc".into());

    if dry {
        let executor = env::var("EXECUTOR_ADDRESS").unwrap_or_default();
        if executor.is_empty() {
            return Err((
                StatusCode::PRECONDITION_FAILED,
                "EXECUTOR_ADDRESS is required for dry-run simulation".into(),
            ));
        }
        // Base Flashblocks endpoints make the pending tag reflect pre-confirmed state.
        let sim = rpc_call(
            &rpc,
            "eth_call",
            serde_json::json!([{"from":env::var("EXECUTOR_OWNER_ADDRESS").unwrap_or_default(),"to":executor,"data":req.calldata},"pending"]),
        )
        .await;
        let msg = match sim {
            Ok(v) => format!(
                "dry-run pending-state eth_call completed: {}",
                v.get("result").and_then(|x| x.as_str()).unwrap_or("no result")
            ),
            Err(e) => format!("dry-run only; pending-state eth_call unavailable: {e}"),
        };
        return Ok(Json(SubmitResponse {
            accepted: true,
            dry_run: true,
            submission_id: id,
            message: msg,
            provider,
            tx_hash: None,
            latency_ms: start.elapsed().as_millis(),
        }));
    }

    if !env::var("LIVE_TRADING_ACK")
        .unwrap_or_default()
        .eq("I_UNDERSTAND_MAINNET_RISK")
    {
        return Err((
            StatusCode::PRECONDITION_FAILED,
            "set LIVE_TRADING_ACK=I_UNDERSTAND_MAINNET_RISK".into(),
        ));
    }
    Err((
        StatusCode::NOT_IMPLEMENTED,
        "live signing requires an external signer integration; raw private keys are intentionally unsupported".into(),
    ))
}

async fn post_route_trigger(
    state: Arc<RwLock<ChainState>>,
    client: reqwest::Client,
    trigger: PendingLogTrigger,
) {
    let url = env::var("PYTHON_ROUTE_TRIGGER_URL")
        .unwrap_or_else(|_| "http://python-api:8080/route-trigger".into());
    match client.post(&url).json(&trigger).send().await {
        Ok(resp) if resp.status().is_success() => {
            state.write().await.route_triggers_sent += 1;
        }
        Ok(resp) => {
            let status = resp.status();
            state.write().await.route_trigger_failures += 1;
            warn!(%status, "route trigger rejected");
        }
        Err(e) => {
            state.write().await.route_trigger_failures += 1;
            warn!(%e, "route trigger request failed");
        }
    }
}

async fn ws_loop(
    state: Arc<RwLock<ChainState>>,
    seen: Arc<Mutex<SeenLogs>>,
    route_trigger_slots: Arc<Semaphore>,
    http_client: reqwest::Client,
) {
    // A healthy Base feed pushes a `newHeads` message roughly every block (~2s), so silence
    // longer than this means the subscription stalled silently (socket still "connected" but
    // delivering nothing). Force a reconnect instead of blocking forever on `socket.next()`.
    let ws_idle_timeout_secs: u64 = env::var("WS_IDLE_TIMEOUT_SECONDS")
        .ok()
        .and_then(|value| value.parse().ok())
        .filter(|value| *value >= 5 && *value <= 600)
        .unwrap_or(30);
    let ws_idle_limit = Duration::from_secs(ws_idle_timeout_secs);
    loop {
        let ws = env::var("BASE_FLASHBLOCKS_WS")
            .or_else(|_| env::var("BASE_WS_RPC"))
            .ok()
            .filter(|v| !v.trim().is_empty());
        let Some(ws) = ws else {
            {
                let mut s = state.write().await;
                s.ws_connected = false;
                s.pending_logs_enabled = false;
                s.last_error = Some("BASE_FLASHBLOCKS_WS or BASE_WS_RPC is required; Base public endpoints do not provide WebSocket subscriptions".into());
            }
            tokio::time::sleep(Duration::from_secs(5)).await;
            continue;
        };

        match tokio_tungstenite::connect_async(&ws).await {
            Ok((mut socket, _)) => {
                {
                    let mut s = state.write().await;
                    s.ws_connected = true;
                    s.last_error = None;
                }

                let heads = serde_json::json!({
                    "jsonrpc":"2.0","id":1,"method":"eth_subscribe","params":["newHeads"]
                });
                if socket
                    .send(tokio_tungstenite::tungstenite::Message::Text(heads.to_string().into()))
                    .await
                    .is_err()
                {
                    continue;
                }

                if env::var("FLASHBLOCK_TX_STREAM_ENABLED")
                    .unwrap_or_else(|_| "false".into())
                    .eq_ignore_ascii_case("true")
                {
                    let sub = serde_json::json!({
                        "jsonrpc":"2.0","id":3,"method":"eth_subscribe","params":["newFlashblockTransactions",true]
                    });
                    if let Err(e) = socket
                        .send(tokio_tungstenite::tungstenite::Message::Text(sub.to_string().into()))
                        .await
                    {
                        state.write().await.last_error = Some(e.to_string());
                        continue;
                    }
                    info!("newFlashblockTransactions full-data subscription enabled");
                }

                let watched_pools = load_watched_pools().await;
                let mut watch_loaded_at = Instant::now();
                let watched_topics = watched_topic_set();
                let mut watched_vec: Vec<String> = watched_pools.iter().cloned().collect();
                watched_vec.sort();
                let mut topic_vec: Vec<String> = watched_topics.iter().cloned().collect();
                topic_vec.sort();
                info!(count=watched_vec.len(), "loaded watched pools from discovery registry + manual overrides");

                let chunk_size: usize = env::var("WATCH_POOL_SUBSCRIPTION_CHUNK_SIZE")
                    .ok().and_then(|v| v.parse().ok()).unwrap_or(200).clamp(1, 1000);
                let address_chunks: Vec<Vec<String>> = if watched_vec.is_empty() {
                    vec![Vec::new()]
                } else {
                    watched_vec.chunks(chunk_size).map(|c| c.to_vec()).collect()
                };
                let mut subscribed_any = false;
                for (idx, chunk) in address_chunks.iter().enumerate() {
                    match build_pending_logs_params_for(chunk, &topic_vec) {
                        Ok(Some(params)) => {
                            let sub = serde_json::json!({
                                "jsonrpc":"2.0","id":2000 + idx,"method":"eth_subscribe","params":params
                            });
                            if let Err(e) = socket.send(tokio_tungstenite::tungstenite::Message::Text(sub.to_string().into())).await {
                                state.write().await.last_error = Some(e.to_string());
                                subscribed_any = false;
                                break;
                            }
                            subscribed_any = true;
                        }
                        Ok(None) => {}
                        Err(e) => {
                            state.write().await.last_error = Some(e.clone());
                            warn!(%e, "pendingLogs configuration rejected");
                        }
                    }
                }
                state.write().await.pending_logs_enabled = subscribed_any;
                if subscribed_any {
                    info!(subscriptions=address_chunks.len(), chunk_size, "pendingLogs subscriptions enabled");
                } else {
                    warn!("pendingLogs disabled: no verified discovered/manual watched pools");
                }

                loop {
                    let msg = match tokio::time::timeout(ws_idle_limit, socket.next()).await {
                        // No frame at all within the idle window: the feed has gone silent.
                        Err(_elapsed) => {
                            warn!(
                                idle_secs = ws_idle_timeout_secs,
                                "websocket feed stalled (no messages received); reconnecting"
                            );
                            state.write().await.last_error =
                                Some(format!("websocket feed idle timeout after {ws_idle_timeout_secs}s"));
                            break;
                        }
                        // Stream ended cleanly.
                        Ok(None) => break,
                        Ok(Some(msg)) => msg,
                    };
                    match msg {
                        Ok(m) => {
                            let refresh_secs: u64 = env::var("WATCH_POOL_REFRESH_SECONDS").ok().and_then(|v| v.parse().ok()).unwrap_or(30);
                            if watch_loaded_at.elapsed() >= Duration::from_secs(refresh_secs) {
                                // Belt-and-braces on top of the client timeout: this await sits
                                // directly in the log-processing path, so it is capped rather
                                // than allowed to block triggers behind a stalled registry.
                                let refreshed = tokio::time::timeout(
                                    registry_fetch_budget().saturating_add(Duration::from_millis(500)),
                                    load_watched_pools(),
                                )
                                .await;
                                watch_loaded_at = Instant::now();
                                match refreshed {
                                    Ok(refreshed) if refreshed != watched_pools => {
                                        info!(old=watched_pools.len(), new=refreshed.len(), "pool registry changed; reconnecting Flashblock subscriptions");
                                        break;
                                    }
                                    Ok(_) => {}
                                    Err(_) => warn!("pool registry refresh timed out; keeping current watch set"),
                                }
                            }
                            let text = m.to_string();
                            let Ok(v) = serde_json::from_str::<Value>(&text) else {
                                continue;
                            };
                            let Some(result) = v.pointer("/params/result") else {
                                continue;
                            };

                            if result.get("hash").is_some() && result.get("logs").is_some() {
                                let triggers = decode_flashblock_transaction_triggers(result, &watched_pools, &watched_topics);
                                {
                                    let mut s = state.write().await;
                                    s.flashblock_transactions_received += 1;
                                    s.flashblock_transaction_logs_matched += triggers.len() as u64;
                                }
                                for trigger in triggers {
                                    let key = dedup_key(&trigger);
                                    if !seen.lock().await.insert_if_new(key) {
                                        state.write().await.duplicate_logs_dropped += 1;
                                        continue;
                                    }
                                    let mut trigger = trigger;
                                    {
                                        let mut s = state.write().await;
                                        stamp_pool_state(&mut s, &mut trigger);
                                        s.last_pending_log_unix_ms = Some(trigger.observed_at_unix_ms);
                                        s.last_pool_address = Some(trigger.pool_address.clone());
                                        if let Some(block) = trigger.block_number {
                                            s.latest_block = s.latest_block.max(block);
                                        }
                                    }
                                    match route_trigger_slots.clone().try_acquire_owned() {
                                        Ok(permit) => {
                                            let trigger_state = state.clone();
                                            let trigger_client = http_client.clone();
                                            tokio::spawn(async move {
                                                post_route_trigger(trigger_state, trigger_client, trigger).await;
                                                drop(permit);
                                            });
                                        }
                                        Err(_) => {
                                            state.write().await.route_triggers_dropped_backpressure += 1;
                                            warn!("route trigger dropped by bounded backpressure");
                                        }
                                    }
                                }
                                continue;
                            }

                            if let Some(hexn) = result.get("number").and_then(Value::as_str) {
                                if let Ok(n) = u64::from_str_radix(hexn.trim_start_matches("0x"), 16) {
                                    state.write().await.latest_block = n;
                                }
                                continue;
                            }

                            let Some(trigger) = decode_pending_log(result) else {
                                continue;
                            };
                            let key = dedup_key(&trigger);
                            let is_new = seen.lock().await.insert_if_new(key);
                            if !is_new {
                                state.write().await.duplicate_logs_dropped += 1;
                                continue;
                            }

                            let mut trigger = trigger;
                            {
                                let mut s = state.write().await;
                                stamp_pool_state(&mut s, &mut trigger);
                                s.pending_logs_received += 1;
                                s.last_pending_log_unix_ms = Some(trigger.observed_at_unix_ms);
                                s.last_pool_address = Some(trigger.pool_address.clone());
                                if let Some(block) = trigger.block_number {
                                    s.latest_block = s.latest_block.max(block);
                                }
                            }

                            match route_trigger_slots.clone().try_acquire_owned() {
                                Ok(permit) => {
                                    let trigger_state = state.clone();
                                    let trigger_client = http_client.clone();
                                    tokio::spawn(async move {
                                        post_route_trigger(trigger_state, trigger_client, trigger).await;
                                        drop(permit);
                                    });
                                }
                                Err(_) => {
                                    state.write().await.route_triggers_dropped_backpressure += 1;
                                    warn!("route trigger dropped by bounded backpressure");
                                }
                            }
                        }
                        Err(e) => {
                            warn!(%e, "websocket error");
                            state.write().await.last_error = Some(e.to_string());
                            break;
                        }
                    }
                }
            }
            Err(e) => {
                let mut s = state.write().await;
                s.ws_connected = false;
                s.pending_logs_enabled = false;
                s.last_error = Some(e.to_string());
            }
        }

        {
            let mut s = state.write().await;
            s.ws_connected = false;
            s.pending_logs_enabled = false;
        }
        tokio::time::sleep(Duration::from_secs(3)).await;
    }
}

#[tokio::main]
async fn main() {
    tracing_subscriber::fmt::init();
    let state = Arc::new(RwLock::new(ChainState::default()));
    let seen = Arc::new(Mutex::new(SeenLogs::new(8192)));
    let max_inflight: usize = env::var("MAX_INFLIGHT_ROUTE_TRIGGERS")
        .ok()
        .and_then(|value| value.parse().ok())
        .filter(|value| *value > 0 && *value <= 64)
        .unwrap_or(16);
    let route_trigger_slots = Arc::new(Semaphore::new(max_inflight));
    // One shared client so route-trigger POSTs reuse pooled keep-alive connections instead of
    // building a fresh pool per request. Bounded timeouts are mandatory: each in-flight POST
    // holds a semaphore permit, so a request that hangs would leak its permit forever and
    // eventually wedge all `max_inflight` slots.
    let route_trigger_timeout_secs: u64 = env::var("ROUTE_TRIGGER_TIMEOUT_SECONDS")
        .ok()
        .and_then(|value| value.parse().ok())
        .filter(|value| *value > 0 && *value <= 120)
        .unwrap_or(15);
    let http_client = reqwest::Client::builder()
        .timeout(Duration::from_secs(route_trigger_timeout_secs))
        .connect_timeout(Duration::from_secs(5))
        .pool_idle_timeout(Duration::from_secs(15))
        .build()
        .expect("failed to build route-trigger HTTP client");
    tokio::spawn(ws_loop(state.clone(), seen, route_trigger_slots, http_client));

    let port: u16 = env::var("RUST_API_PORT")
        .ok()
        .and_then(|v| v.parse().ok())
        .unwrap_or(8081);
    let app = Router::new()
        .route("/health", get(health))
        .route("/submit", post(submit))
        .route("/submit-plan", post(submit_plan))
        .route("/pack-batch", post(pack_batch))
        .with_state(state);
    let addr = SocketAddr::from(([0, 0, 0, 0], port));
    info!(%addr, "rust executor listening");
    let listener = tokio::net::TcpListener::bind(addr).await.unwrap();
    axum::serve(listener, app).await.unwrap();
}


#[cfg(test)]
mod adaptive_tests {
    use super::*;

    #[test]
    fn adaptive_gate_decays_with_latency() {
        let (capture, survival, total) = adaptive_expected_capture(1_000, 1_000, 1_100, 100, 1_000, 9_000, 10).unwrap();
        assert_eq!(total, 200);
        assert_eq!(survival, 8_000);
        assert_eq!(capture, 710);
    }

    #[test]
    fn adaptive_gate_reaches_zero_after_window() {
        let (capture, survival, _) = adaptive_expected_capture(1_000, 1_000, 2_000, 1, 500, 10_000, 0).unwrap();
        assert_eq!(survival, 0);
        assert_eq!(capture, 0);
    }

    #[test]
    fn flashblock_transaction_only_emits_watched_pool_logs() {
        std::env::set_var("WATCHED_POOL_ADDRESSES", "0x0000000000000000000000000000000000000011,0x0000000000000000000000000000000000000022");
        std::env::remove_var("WATCHED_TOPIC0S");
        let tx = serde_json::json!({
            "hash":"0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
            "from":"0x00000000000000000000000000000000000000aa",
            "blockNumber":"0x64",
            "transactionIndex":"0x1",
            "logs":[
                {"address":"0x0000000000000000000000000000000000000011","topics":[],"data":"0x","logIndex":"0x0"},
                {"address":"0x0000000000000000000000000000000000000022","topics":[],"data":"0x","logIndex":"0x1"},
                {"address":"0x0000000000000000000000000000000000000099","topics":[],"data":"0x","logIndex":"0x2"}
            ]
        });
        let watched = watched_pool_set();
        let topics = watched_topic_set();
        let triggers = decode_flashblock_transaction_triggers(&tx, &watched, &topics);
        assert_eq!(triggers.len(), 2);
        assert_eq!(triggers[0].pool_address, "0x0000000000000000000000000000000000000011");
        assert_eq!(triggers[0].block_number, Some(100));
        assert_eq!(triggers[0].source_sender.as_deref(), Some("0x00000000000000000000000000000000000000aa"));
        assert_eq!(triggers[0].tx_touched_pools.len(), 2);
    }
    #[test]
    fn pool_state_version_changes_monotonically() {
        let mut state=ChainState::default();
        let mut t=PendingLogTrigger{pool_address:"0x0000000000000000000000000000000000000011".into(),topic0:None,transaction_hash:Some("0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa".into()),block_number:Some(100),transaction_index:Some(0),log_index:Some(1),observed_at_unix_ms:1000,state_version:None,state_sequence:None,raw_data:"0x01".into(),source_sender:None,tx_touched_pools:vec![]};
        stamp_pool_state(&mut state,&mut t);
        let first=t.state_version.clone().unwrap();
        assert_eq!(t.state_sequence,Some(1));
        t.raw_data="0x02".into();
        stamp_pool_state(&mut state,&mut t);
        assert_eq!(t.state_sequence,Some(2));
        assert_ne!(t.state_version.unwrap(),first);
        assert_eq!(state.cached_pool_states,1);
    }

    #[test]
    fn state_validation_tracks_trigger_pool_not_global_sequence() {
        let mut state=ChainState::default();
        let mut t=PendingLogTrigger{pool_address:"0x0000000000000000000000000000000000000011".into(),topic0:None,transaction_hash:None,block_number:Some(1),transaction_index:None,log_index:Some(0),observed_at_unix_ms:1,state_version:None,state_sequence:None,raw_data:"0x".into(),source_sender:None,tx_touched_pools:vec![]};
        stamp_pool_state(&mut state,&mut t);
        let req=SubmitPlanRequest{route_id:"r".into(),calldata:"0x12345678".into(),gas_limit:None,deterministic_net_profit_units:"1".into(),asset:"0x0000000000000000000000000000000000000001".into(),target_block:None,opportunity_observed_at_unix_ms:None,expected_execution_latency_ms:None,survival_window_ms:None,inclusion_probability_bps:None,expected_failure_cost_units:None,safety_margin_units:None,trigger_pool:Some(t.pool_address.clone()),state_version:t.state_version.clone(),state_sequence:t.state_sequence};
        assert!(validate_state_version(&state,&req).is_ok());

        // An unrelated pool emitting a log advances the global counter but must NOT stale this
        // route: its own trigger pool has not moved.
        let mut other=PendingLogTrigger{pool_address:"0x0000000000000000000000000000000000000022".into(),topic0:None,transaction_hash:None,block_number:Some(2),transaction_index:None,log_index:Some(0),observed_at_unix_ms:2,state_version:None,state_sequence:None,raw_data:"0x".into(),source_sender:None,tx_touched_pools:vec![]};
        stamp_pool_state(&mut state,&mut other);
        assert!(state.global_state_sequence > req.state_sequence.unwrap());
        assert!(validate_state_version(&state,&req).is_ok());

        // A new event on the trigger pool itself changes its fingerprint and must reject.
        let mut again=t.clone();
        again.raw_data="0xdeadbeef".into();
        stamp_pool_state(&mut state,&mut again);
        assert!(validate_state_version(&state,&req).is_err());
    }

    #[test]
    fn consensus_url_dedup_is_order_independent() {
        std::env::set_var("BASE_HTTP_RPCS", "https://a.example,https://b.example,https://a.example");
        let urls = configured_rpc_urls();
        std::env::remove_var("BASE_HTTP_RPCS");
        assert_eq!(urls, vec!["https://a.example".to_string(), "https://b.example".to_string()]);
    }

    #[test]
    fn bearer_token_file_requires_private_permissions() {
        let path = std::env::temp_dir().join(format!(
            "base-arb-signer-token-test-{}",
            std::process::id()
        ));
        fs::write(&path, "0123456789abcdef0123456789abcdef\n").unwrap();
        fs::set_permissions(&path, fs::Permissions::from_mode(0o600)).unwrap();
        assert_eq!(
            read_bearer_token_file(&path).unwrap(),
            "0123456789abcdef0123456789abcdef"
        );
        fs::set_permissions(&path, fs::Permissions::from_mode(0o644)).unwrap();
        assert!(read_bearer_token_file(&path).is_err());
        fs::remove_file(path).unwrap();
    }

}
