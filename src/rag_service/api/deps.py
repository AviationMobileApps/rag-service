from __future__ import annotations

from dataclasses import dataclass

from fastapi import Depends, Header, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from rag_service.config.settings import settings


security = HTTPBearer(auto_error=False)


@dataclass(frozen=True)
class RequestContext:
    tenant_id: str
    workspace_id: str | None
    principal_id: str | None


def get_request_context(
    credentials: HTTPAuthorizationCredentials | None = Depends(security),
    x_workspace_id: str | None = Header(default=None, alias="X-Workspace-Id"),
    x_principal_id: str | None = Header(default=None, alias="X-Principal-Id"),
) -> RequestContext:
    if not credentials or (credentials.scheme or "").lower() != "bearer":
        raise HTTPException(status_code=401, detail="Missing Bearer token")

    tenant_id = settings.tenant_id_for_api_key(credentials.credentials)
    if not tenant_id:
        raise HTTPException(status_code=401, detail="Invalid tenant API key")

    return RequestContext(tenant_id=tenant_id, workspace_id=x_workspace_id, principal_id=x_principal_id)

