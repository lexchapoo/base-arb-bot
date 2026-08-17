mod packed;

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
    collections::{HashSet, VecDeque},
    env,
    fs,
    net::SocketAddr,
    os::unix::fs::PermissionsExt,
    path::Path,
    sync::Arc,
    time::{Duration, Instant, SystemTime, UNIX_EPOCH},
};
use tokio::sync::{Mutex, RwLock, Semaphore};
use tracing::{info, warn};

#[derive(Default, Serialize, Clone)]
struct ChainState {
    latest_block: u64,
    ws_connected: bool,
    pending_logs_enabled: bool,
    pending_logs_received: u64,
    duplicate_logs_dropped: u64,
    route_triggers_sent: u64,
    route_trigger_failures: u64,
    route_triggers_dropped_backpressure: u64,
    last_pending_log_unix_ms: Option<u128>,
    last_pool_address: Option<String>,
    last_error: Option<String>,
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
    raw_data: String,
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

fn valid_address(value: &str) -> bool {
    value.len() == 42
        && value.starts_with("0x")
        && value[2..].chars().all(|c| c.is_ascii_hexdigit())
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

fn websocket_url_from_http(http_url: &str) -> Option<String> {
    if let Some(rest) = http_url.strip_prefix("https://") {
        return Some(format!("wss://{rest}"));
    }
    if let Some(rest) = http_url.strip_prefix("http://") {
        return Some(format!("ws://{rest}"));
    }
    None
}

fn configured_websocket_url() -> Option<String> {
    let configured = env::var("BASE_FLASHBLOCKS_WS")
        .or_else(|_| env::var("BASE_WS_RPC"))
        .ok()
        .filter(|value| !value.trim().is_empty())?;
    if configured.eq_ignore_ascii_case("from-base-http-rpc") {
        return env::var("BASE_HTTP_RPC")
            .ok()
            .and_then(|value| websocket_url_from_http(&value));
    }
    Some(configured)
}

fn build_pending_logs_params() -> Result<Option<Value>, String> {
    let addresses = csv_env("WATCHED_POOL_ADDRESSES");
    let topic0s = csv_env("WATCHED_TOPIC0S");
    let allow_unfiltered = env::var("ALLOW_UNFILTERED_PENDING_LOGS")
        .unwrap_or_default()
        .eq_ignore_ascii_case("true");

    for address in &addresses {
        if !valid_address(address) {
            return Err(format!("invalid WATCHED_POOL_ADDRESSES entry: {address}"));
        }
    }
    for topic in &topic0s {
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
        // eth_getLogs topic syntax: nested array in position 0 means OR across event signatures.
        filter.insert("topics".into(), serde_json::json!([topic0s]));
    }
    Ok(Some(serde_json::json!(["pendingLogs", Value::Object(filter)])))
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
        pool_address: address,
        topic0,
        transaction_hash: result
            .get("transactionHash")
            .and_then(Value::as_str)
            .map(str::to_lowercase),
        block_number: parse_hex_u64(result.get("blockNumber").and_then(Value::as_str)),
        transaction_index: parse_hex_u64(result.get("transactionIndex").and_then(Value::as_str)),
        log_index: parse_hex_u64(result.get("logIndex").and_then(Value::as_str)),
        observed_at_unix_ms: now_unix_ms(),
        raw_data: result
            .get("data")
            .and_then(Value::as_str)
            .unwrap_or("0x")
            .to_string(),
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

async fn rpc_call(url: &str, method: &str, params: Value) -> Result<Value, String> {
    let body = serde_json::json!({"jsonrpc":"2.0","id":1,"method":method,"params":params});
    reqwest::Client::new()
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

async fn submit_plan(Json(req): Json<SubmitPlanRequest>) -> Result<Json<SubmitResponse>, (StatusCode, String)> {
    let start = Instant::now();
    if !req.calldata.starts_with("0x") || req.calldata.len() < 10 {
        return Err((StatusCode::BAD_REQUEST, "calldata must be non-empty hex".into()));
    }
    if !valid_address(&req.asset) {
        return Err((StatusCode::BAD_REQUEST, "asset must be an EVM address".into()));
    }
    let dry = env::var("DRY_RUN")
        .unwrap_or_else(|_| "true".into())
        .eq_ignore_ascii_case("true");
    let rpc = env::var("BASE_HTTP_RPC").unwrap_or_else(|_| "https://mainnet.base.org".into());
    let provider = env::var("SUBMISSION_PROVIDER_NAME").unwrap_or_else(|_| "base-rpc".into());
    let expected_chain: u128 = env::var("CHAIN_ID").ok().and_then(|v| v.parse().ok()).unwrap_or(8453);
    let chain_body = rpc_call(&rpc, "eth_chainId", serde_json::json!([]))
        .await.map_err(|e| (StatusCode::BAD_GATEWAY, e))?;
    let actual_chain = rpc_hex_u128(&chain_body).map_err(|e| (StatusCode::BAD_GATEWAY, e))?;
    if actual_chain != expected_chain {
        return Err((StatusCode::PRECONDITION_FAILED, format!("wrong chain: expected {expected_chain}, got {actual_chain}")));
    }
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
                if latest > target as u128 {
                    return Err((StatusCode::GONE, "route target block already passed".into()));
                }
            }
        }
    }

    // Re-simulate the exact final calldata immediately before either dry-run acceptance or live signing.
    // This is deliberately inside the Rust submission boundary so Python cannot bypass it.
    let sim = rpc_call(
        &rpc,
        "eth_call",
        serde_json::json!([{"from":owner,"to":executor,"data":req.calldata,"value":"0x0"},"pending"]),
    )
    .await
    .map_err(|e| (StatusCode::BAD_GATEWAY, e))?;
    let sim_result = rpc_result_value(&sim).map_err(|e| (StatusCode::UNPROCESSABLE_ENTITY, e))?;

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
    let client = reqwest::Client::new();
    let mut signer_request = client.post(&signer_url).json(&signer_payload);
    if let Some(token) = signer_bearer_token()
        .map_err(|e| (StatusCode::PRECONDITION_FAILED, e))?
    {
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

    let dry = env::var("DRY_RUN")
        .unwrap_or_else(|_| "true".into())
        .eq_ignore_ascii_case("true");
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
    // A healthy Base feed pushes a `newHeads` message roughly every block
    // (~2s), so any silence longer than this means the subscription has
    // silently stalled (socket still "connected" but delivering nothing).
    // When that happens we force a reconnect instead of blocking forever on
    // `socket.next()`.
    let ws_idle_timeout_secs: u64 = env::var("WS_IDLE_TIMEOUT_SECONDS")
        .ok()
        .and_then(|value| value.parse().ok())
        .filter(|value| *value >= 5 && *value <= 600)
        .unwrap_or(30);
    let ws_idle_limit = Duration::from_secs(ws_idle_timeout_secs);
    loop {
        let ws = configured_websocket_url();
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

                match build_pending_logs_params() {
                    Ok(Some(params)) => {
                        let sub = serde_json::json!({
                            "jsonrpc":"2.0","id":2,"method":"eth_subscribe","params":params
                        });
                        if let Err(e) = socket
                            .send(tokio_tungstenite::tungstenite::Message::Text(sub.to_string().into()))
                            .await
                        {
                            state.write().await.last_error = Some(e.to_string());
                            continue;
                        }
                        state.write().await.pending_logs_enabled = true;
                        info!("pendingLogs subscription enabled");
                    }
                    Ok(None) => {
                        state.write().await.pending_logs_enabled = false;
                        warn!("pendingLogs disabled: set WATCHED_POOL_ADDRESSES or explicitly set ALLOW_UNFILTERED_PENDING_LOGS=true");
                    }
                    Err(e) => {
                        state.write().await.pending_logs_enabled = false;
                        state.write().await.last_error = Some(e.clone());
                        warn!(%e, "pendingLogs configuration rejected");
                    }
                }

                loop {
                    let msg = match tokio::time::timeout(ws_idle_limit, socket.next()).await {
                        // No frame at all within the idle window: the feed has
                        // gone silent. Break to trigger the reconnect path below.
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
                            let text = m.to_string();
                            let Ok(v) = serde_json::from_str::<Value>(&text) else {
                                continue;
                            };
                            let Some(result) = v.pointer("/params/result") else {
                                continue;
                            };

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

                            {
                                let mut s = state.write().await;
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
    // One shared client so route-trigger POSTs reuse pooled keep-alive
    // connections instead of building a fresh pool per request. Bounded
    // timeouts are mandatory here: each in-flight POST holds a semaphore
    // permit, so a request that hangs (e.g. a stale keep-alive connection)
    // would leak its permit forever and eventually wedge all `max_inflight`
    // slots. The timeout turns a stuck POST into a fast failure that
    // releases the permit. Idle pooled connections are also expired well
    // before a typical server keep-alive timeout to avoid reusing a
    // half-closed socket.
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
mod main_tests {
    use super::{read_bearer_token_file, websocket_url_from_http};
    use std::{fs, os::unix::fs::PermissionsExt};

    #[test]
    fn derives_websocket_scheme_without_changing_endpoint_identity() {
        assert_eq!(
            websocket_url_from_http("https://base.example/v2/credential"),
            Some("wss://base.example/v2/credential".into())
        );
        assert_eq!(
            websocket_url_from_http("http://localhost:8545"),
            Some("ws://localhost:8545".into())
        );
        assert_eq!(websocket_url_from_http("ftp://invalid"), None);
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
