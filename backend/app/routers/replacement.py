"""伸缩缝成组更换接口：提交预占、安全评估、更换落账、批次详情与施工工作台。

三处推进（材料预占、缝台账、施工工作台）统一走 ReplacementService，
路由层只做参数接收与结果包装，不再各自维护进度。
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.schemas import ActionResult, PageResult
from app.services.replacement import replacement_service

router = APIRouter(prefix="/api/replacements", tags=["伸缩缝成组更换"])


class ReplacementPayload(BaseModel):
    """成组更换各阶段提交体；按施工批次号保证幂等。"""

    values: dict = Field(default_factory=dict)


@router.get("", response_model=PageResult[dict])
def list_batches(page: int = 1, size: int = 20) -> PageResult[dict]:
    """成组更换批次列表：阶段与工单进度取统一编排写入的同一份数据。"""
    if size > 200:
        size = 200
    items, total = replacement_service.list_batches(page=page, size=size)
    return PageResult(items=items, total=total, page=page, size=size)


@router.get("/workbench", response_model=dict)
def workbench() -> dict:
    """施工工作台：仅返回已评估待更换工单，按评估意见的安全优先顺序排列。"""
    return {"items": replacement_service.workbench()}


@router.get("/{batch_no}", response_model=dict)
def get_batch(batch_no: str) -> dict:
    """批次详情：与列表、工作台同源，重试或刷新看到的阶段、工单、占用完全一致。"""
    batch = replacement_service.get_batch(batch_no)
    if batch is None:
        raise HTTPException(status_code=404, detail=f"施工批次「{batch_no}」不存在或已归档")
    return batch


@router.post("/submit", response_model=ActionResult)
def submit_batch(payload: ReplacementPayload) -> ActionResult:
    """阶段一：提交一批缝并预占新材料；同批次号重复提交幂等回显。"""
    batch, message = replacement_service.submit(payload.values)
    if batch is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=batch)


@router.post("/assess", response_model=ActionResult)
def assess_batch(payload: ReplacementPayload) -> ActionResult:
    """阶段二：按各缝评估意见确定安全优先顺序（紧急>严重>一般）。"""
    batch, message = replacement_service.assess(payload.values)
    if batch is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=batch)


@router.post("/confirm", response_model=ActionResult)
def confirm_batch(payload: ReplacementPayload) -> ActionResult:
    """阶段三：更换落账，结论回写缝台账、旧占用迁移释放、新材料预占转消耗。"""
    batch, message = replacement_service.confirm(payload.values)
    if batch is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=batch)
