"""Internal authenticated HTTP API for immutable deployment approvals."""

from __future__ import annotations

from dataclasses import asdict
from typing import Annotated, Optional

from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from starlette.responses import JSONResponse

from .deployment_auth import (
    DeploymentActor, DeploymentActorAuthenticator, DeploymentActorAuthorizationError,
)
from .deployment_executor import DeploymentExecutionError
from .deployment_policy import DeploymentPolicyError
from .deployment_proposals import DeploymentApprovalError
from .deployment_service import ImmutableDeploymentService


MAX_BODY_BYTES = 8_192


class ProposalRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    plugin_id: str = Field(pattern=r"^[a-z0-9](?:[a-z0-9-]{0,62})$")
    git_sha: str = Field(pattern=r"^[0-9a-f]{40}$")
    image_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    health_timeout_seconds: int = Field(default=60, ge=10, le=300)
    ttl_seconds: int = Field(default=900, ge=30, le=3600)


class RequestBodyLimitMiddleware:
    def __init__(self, app): self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        headers = {key.lower(): value for key, value in scope.get("headers", [])}
        raw_length = headers.get(b"content-length")
        if raw_length:
            try:
                length = int(raw_length)
                if length < 0:
                    raise ValueError
                if length > MAX_BODY_BYTES:
                    return await JSONResponse({"detail": "request body exceeds policy limit"}, status_code=413)(scope, receive, send)
            except (TypeError, ValueError):
                return await JSONResponse({"detail": "invalid content-length"}, status_code=400)(scope, receive, send)
        messages, received = [], 0
        while True:
            message = await receive(); messages.append(message)
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > MAX_BODY_BYTES:
                    return await JSONResponse({"detail": "request body exceeds policy limit"}, status_code=413)(scope, receive, send)
                if not message.get("more_body", False): break
            else: break
        async def replay_receive():
            return messages.pop(0) if messages else {"type": "http.request", "body": b"", "more_body": False}
        return await self.app(scope, replay_receive, send)


def create_app(
    service: ImmutableDeploymentService, authenticator: DeploymentActorAuthenticator,
) -> FastAPI:
    app = FastAPI(title="Aegis Immutable Deployment Service", docs_url=None, redoc_url=None, openapi_url=None)
    app.add_middleware(RequestBodyLimitMiddleware)

    def actor(authorization: Annotated[Optional[str], Header()] = None) -> DeploymentActor:
        try: return authenticator.authenticate(authorization)
        except DeploymentActorAuthorizationError as exc:
            raise HTTPException(status_code=401, detail=str(exc)) from exc

    def invoke(operation):
        try: return operation()
        except DeploymentActorAuthorizationError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except (DeploymentApprovalError, DeploymentPolicyError, DeploymentExecutionError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.get("/health")
    def health(): return {"status": "ok"}

    @app.post("/v1/image-deployments")
    def propose(request: ProposalRequest, current: DeploymentActor = Depends(actor)):
        return invoke(lambda: service.propose(
            plugin_id=request.plugin_id, git_sha=request.git_sha,
            image_digest=request.image_digest,
            health_timeout_seconds=request.health_timeout_seconds,
            ttl_seconds=request.ttl_seconds, actor=current,
        ))

    @app.get("/v1/image-deployments/{proposal_id}")
    def get_proposal(proposal_id: str, current: DeploymentActor = Depends(actor)):
        return invoke(lambda: service.get(proposal_id, actor=current))

    @app.post("/v1/image-deployments/{proposal_id}/approve")
    def approve(proposal_id: str, current: DeploymentActor = Depends(actor)):
        return invoke(lambda: service.approve(proposal_id, actor=current))

    @app.post("/v1/image-deployments/{proposal_id}/execute")
    def execute(proposal_id: str, current: DeploymentActor = Depends(actor)):
        return invoke(lambda: asdict(service.execute(proposal_id, actor=current)))

    return app
