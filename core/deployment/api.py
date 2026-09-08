"""Internal, authenticated HTTP surface for human-governed restarts."""

from __future__ import annotations

from dataclasses import asdict
from typing import Annotated, Optional

from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from .auth import ActorAuthenticator
from .body_limit import RequestBodyLimitMiddleware
from .restart import ApprovalError
from .service import Actor, ActorAuthorizationError, DeploymentService


class ProposalRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    plugin_id: str = Field(pattern=r"^[a-z0-9](?:[a-z0-9-]{0,62})$")
    ttl_seconds: int = Field(default=900, ge=30, le=3600)


def create_app(service: DeploymentService, authenticator: ActorAuthenticator) -> FastAPI:
    app = FastAPI(
        title="Aegis Deployment Approval Service", docs_url=None,
        redoc_url=None, openapi_url=None,
    )
    app.add_middleware(RequestBodyLimitMiddleware, max_bytes=8_192)

    def actor(authorization: Annotated[Optional[str], Header()] = None) -> Actor:
        try:
            return authenticator.authenticate(authorization)
        except ActorAuthorizationError as exc:
            raise HTTPException(status_code=401, detail=str(exc)) from exc

    def invoke(operation):
        try:
            return operation()
        except ActorAuthorizationError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except ApprovalError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/v1/restart-proposals")
    def propose(request: ProposalRequest, current: Actor = Depends(actor)):
        return invoke(lambda: service.propose(
            plugin_id=request.plugin_id, actor=current,
            ttl_seconds=request.ttl_seconds,
        ))

    @app.get("/v1/restart-proposals/{proposal_id}")
    def get_proposal(proposal_id: str, current: Actor = Depends(actor)):
        if not current.roles.intersection({"requester", "approver", "executor"}):
            raise HTTPException(status_code=403, detail="actor cannot inspect proposals")
        proposal = invoke(lambda: service.store.get(proposal_id))
        if current.roles == frozenset({"requester"}) and proposal["requested_by"] != current.actor_id:
            raise HTTPException(status_code=403, detail="requester cannot inspect another actor's proposal")
        return proposal

    @app.post("/v1/restart-proposals/{proposal_id}/approve")
    def approve(proposal_id: str, current: Actor = Depends(actor)):
        return invoke(lambda: service.approve(proposal_id, actor=current))

    @app.post("/v1/restart-proposals/{proposal_id}/execute")
    def execute(proposal_id: str, current: Actor = Depends(actor)):
        return invoke(lambda: asdict(service.execute(proposal_id, actor=current)))

    return app
