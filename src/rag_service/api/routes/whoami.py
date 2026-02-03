from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from rag_service.api.deps import RequestContext, get_request_context


router = APIRouter(prefix="/v1", tags=["meta"])


class WhoAmIResponse(BaseModel):
    tenant_id: str
    workspace_id: str | None
    principal_id: str | None


@router.get("/whoami", response_model=WhoAmIResponse)
def whoami(ctx: RequestContext = Depends(get_request_context)) -> WhoAmIResponse:
    return WhoAmIResponse(tenant_id=ctx.tenant_id, workspace_id=ctx.workspace_id, principal_id=ctx.principal_id)

