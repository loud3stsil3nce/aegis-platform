"""Governed lifecycle mutations for Aegis plugins."""

from .restart import (
    ApprovalError,
    RestartExecutor,
    RestartProposalStore,
    RestartResult,
)
from .lifecycle import HttpLifecycleAdapter, LifecycleAdapterError
from .auth import ActorAuthenticator
from .service import Actor, ActorAuthorizationError, DeploymentService

__all__ = [
    "Actor", "ActorAuthenticator", "ActorAuthorizationError", "ApprovalError",
    "DeploymentService", "HttpLifecycleAdapter", "LifecycleAdapterError",
    "RestartExecutor", "RestartProposalStore", "RestartResult",
]
