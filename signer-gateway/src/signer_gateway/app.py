from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import hmac
import json
import os
from pathlib import Path
import stat
from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from .policy import (
    PolicyConfig,
    PolicyError,
    ReviewedDeploymentPlan,
    clef_transaction,
    deployment_method_signature,
    normalize_address,
    read_secret_file,
    validate_deployment,
    validate_execution,
    verify_signed_transaction,
)


class SignRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)
    chain_id: int
    from_address: str = Field(alias="from")
    to: str
    nonce: str
    gas: str
    max_fee_per_gas: str
    max_priority_fee_per_gas: str
    value: str
    data: str
    route_id: str
    asset: str

    def model_dump_for_policy(self) -> dict[str, Any]:
        data = self.model_dump()
        data["from"] = data.pop("from_address")
        return data


class DeploymentSignRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)
    plan_hash: str
    purpose: str
    chain_id: int
    from_address: str = Field(alias="from")
    to: str | None = None
    nonce: str
    gas: str
    max_fee_per_gas: str
    max_priority_fee_per_gas: str
    value: str
    data: str

    def model_dump_for_policy(self) -> dict[str, Any]:
        data = self.model_dump()
        data["from"] = data.pop("from_address")
        return data


def required_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"{name} is required")
    return value


def env_true(name: str) -> bool:
    return os.environ.get(name, "false").lower() == "true"


async def clef_rpc(
    ipc_path: str,
    request: dict[str, Any],
    timeout_seconds: float,
    max_response_bytes: int = 1_048_576,
) -> dict[str, Any]:
    writer: asyncio.StreamWriter | None = None
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_unix_connection(ipc_path, limit=max_response_bytes + 1),
            timeout=timeout_seconds,
        )
        wire_request = json.dumps(request, separators=(",", ":")).encode("utf-8") + b"\n"
        writer.write(wire_request)
        await asyncio.wait_for(writer.drain(), timeout=timeout_seconds)
        wire_response = await asyncio.wait_for(reader.readline(), timeout=timeout_seconds)
        if not wire_response:
            raise RuntimeError("Clef closed the IPC connection without a response")
        if len(wire_response) > max_response_bytes:
            raise RuntimeError("Clef IPC response exceeds the configured size limit")
        body = json.loads(wire_response)
        if not isinstance(body, dict):
            raise RuntimeError("Clef returned a non-object JSON-RPC response")
        return body
    except (OSError, asyncio.TimeoutError, ValueError) as exc:
        raise RuntimeError("Clef IPC request failed") from exc
    finally:
        if writer is not None:
            writer.close()
            try:
                await writer.wait_closed()
            except OSError:
                pass


def create_app() -> FastAPI:
    signer_address = normalize_address(required_env("SIGNER_GATEWAY_ADDRESS"), "SIGNER_GATEWAY_ADDRESS")
    executor_raw = os.environ.get("EXECUTOR_ADDRESS", "").strip()
    executor_address = normalize_address(executor_raw, "EXECUTOR_ADDRESS") if executor_raw else ""
    policy = PolicyConfig(
        chain_id=int(os.environ.get("CHAIN_ID", "8453")),
        signer_address=signer_address,
        executor_address=executor_address,
        max_gas=int(os.environ.get("SIGNER_MAX_GAS", "3000000")),
        max_deployment_gas=int(os.environ.get("SIGNER_MAX_DEPLOYMENT_GAS", "6000000")),
        max_fee_per_gas_wei=int(os.environ.get("SIGNER_MAX_FEE_PER_GAS_WEI", "5000000000")),
        allow_execution_signing=env_true("SIGNER_ALLOW_EXECUTION"),
        allow_deployment_signing=env_true("SIGNER_ALLOW_DEPLOYMENT"),
    )
    if policy.allow_execution_signing and not executor_address:
        raise RuntimeError("EXECUTOR_ADDRESS is required when execution signing is enabled")
    token_file = Path(required_env("SIGNER_GATEWAY_TOKEN_FILE"))
    clef_ipc_path = required_env("CLEF_IPC_PATH")
    plan_path = os.environ.get("SIGNER_DEPLOYMENT_PLAN_PATH", "").strip()
    if policy.allow_deployment_signing and not plan_path:
        raise RuntimeError(
            "SIGNER_DEPLOYMENT_PLAN_PATH is required when deployment signing is enabled"
        )
    plan = ReviewedDeploymentPlan.load(Path(plan_path)) if plan_path else None
    clef_timeout = float(os.environ.get("CLEF_SIGN_TIMEOUT_SECONDS", "300"))
    if clef_timeout <= 0:
        raise RuntimeError("CLEF_SIGN_TIMEOUT_SECONDS must be positive")
    clef_max_response_bytes = int(os.environ.get("CLEF_MAX_RESPONSE_BYTES", "1048576"))
    if clef_max_response_bytes < 65_536:
        raise RuntimeError("CLEF_MAX_RESPONSE_BYTES must be at least 65536")
    @asynccontextmanager
    async def lifespan(_: FastAPI):
        read_secret_file(token_file)
        try:
            socket_mode = Path(clef_ipc_path).stat().st_mode
        except OSError as exc:
            raise RuntimeError(f"Clef IPC socket is unavailable: {clef_ipc_path}") from exc
        if not stat.S_ISSOCK(socket_mode):
            raise RuntimeError(f"CLEF_IPC_PATH is not a Unix socket: {clef_ipc_path}")
        yield

    app = FastAPI(title="Base Arbitrage Clef Gateway", version="0.1.0", lifespan=lifespan)

    async def authenticate(authorization: str | None = Header(default=None)) -> None:
        expected = read_secret_file(token_file)
        supplied = authorization.removeprefix("Bearer ") if authorization else ""
        if not hmac.compare_digest(supplied, expected):
            raise HTTPException(401, "invalid signer gateway authorization")

    async def sign_with_clef(
        normalized: dict[str, Any], method_signature: str | None = None
    ) -> dict[str, str]:
        params: list[Any] = [clef_transaction(normalized)]
        if method_signature is not None:
            params.append(method_signature)
        request = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "account_signTransaction",
            "params": params,
        }
        try:
            body = await clef_rpc(
                clef_ipc_path,
                request,
                clef_timeout,
                clef_max_response_bytes,
            )
        except RuntimeError as exc:
            raise HTTPException(502, "Clef request failed") from exc
        if body.get("error"):
            raise HTTPException(502, f"Clef rejected signing request: {body['error'].get('message', 'unknown')}")
        raw = body.get("result", {}).get("raw", "")
        try:
            verify_signed_transaction(raw, normalized)
        except PolicyError as exc:
            raise HTTPException(502, str(exc)) from exc
        return {"raw_transaction": raw}

    @app.get("/health")
    async def health() -> dict[str, Any]:
        return {
            "status": "ok",
            "chain_id": policy.chain_id,
            "signer_address": policy.signer_address,
            "execution_signing": policy.allow_execution_signing,
            "deployment_signing": policy.allow_deployment_signing,
            "deployment_plan_hash": plan.sha256 if plan else None,
        }

    @app.post("/sign", dependencies=[Depends(authenticate)])
    async def sign(request: SignRequest) -> dict[str, str]:
        try:
            normalized = validate_execution(request.model_dump_for_policy(), policy)
        except PolicyError as exc:
            raise HTTPException(422, str(exc)) from exc
        return await sign_with_clef(normalized)

    @app.post("/sign-deployment", dependencies=[Depends(authenticate)])
    async def sign_deployment(request: DeploymentSignRequest) -> dict[str, str]:
        try:
            normalized = validate_deployment(request.model_dump_for_policy(), policy, plan)
            method_signature = deployment_method_signature(normalized)
        except PolicyError as exc:
            raise HTTPException(422, str(exc)) from exc
        return await sign_with_clef(normalized, method_signature)

    return app


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(create_app(), host="127.0.0.1", port=int(os.environ.get("SIGNER_GATEWAY_PORT", "9000")))
