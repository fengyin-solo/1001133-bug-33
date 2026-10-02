"""伸缩缝管理接口：维护伸缩缝，覆盖清理堵塞、修补锚固、成组更换等动作。

成组更换相关接口（批次开立、阶段推进、施工工作台）统一在本路由声明，
阶段顺序固定为 评估 → 预占 → 施工 → 验收，由服务层保证幂等与并发互斥。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query

from app.schemas import ActionResult, EntryPayload, PageResult
from app.services.expansion import ExpansionService, ServiceError

router = APIRouter(prefix="/api/expansion", tags=["伸缩缝管理"])

service = ExpansionService()

LIST_FIELDS = ["缝编号", "所属桥梁", "缝类型", "设计伸缩量", "当前缝宽", "堵塞情况", "锚固状态", "缝状态", "评估意见", "最近批次号", "更换结论"]
STATUSES = ["正常", "堵塞", "锚固损坏", "已更换"]


# --------------------------------------------------------------- 施工批次

@router.get("/batches")
def list_batches() -> dict[str, Any]:
    """施工批次列表：批次阶段是材料预占、缝台账与工作台共同的唯一进度来源。"""
    return {"total": len(service.list_batches()), "items": service.list_batches()}


@router.post("/batches", response_model=ActionResult)
def create_batch(payload: EntryPayload) -> ActionResult:
    """按施工批次开立成组更换任务（批次号幂等），进入评估阶段。"""
    try:
        batch, message = service.create_batch(payload.values)
    except ServiceError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message) from None
    if batch is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=batch)


@router.get("/batches/{batch_id}")
def get_batch(batch_id: int) -> dict[str, Any]:
    """读取单个施工批次（含工单与材料计划）；不存在时给出可读说明。"""
    batch = service.get_batch(batch_id)
    if batch is None:
        raise HTTPException(status_code=404, detail=f"施工批次 {batch_id} 不存在")
    return batch


@router.post("/batches/{batch_id}/advance", response_model=ActionResult)
def advance_batch(batch_id: int, payload: EntryPayload) -> ActionResult:
    """把施工批次推进一个阶段；并发提交只允许一个批次推进，其余得到 409。"""
    raw_widths = payload.values.get("复测缝宽") or {}
    widths: dict[int, str] = {}
    if raw_widths:
        if not isinstance(raw_widths, dict):
            return ActionResult(ok=False, message="复测缝宽需为 {缝id: 缝宽} 的映射")
        for key, value in raw_widths.items():
            try:
                widths[int(key)] = str(value)
            except (TypeError, ValueError):
                return ActionResult(ok=False, message=f"缝标识无法识别：{key!r}")
    try:
        batch, advanced, message = service.advance_batch(batch_id, remeasured_widths=widths)
    except ServiceError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message) from None
    return ActionResult(ok=True, message=message, entry=batch)


@router.get("/workbench")
def workbench() -> dict[str, Any]:
    """施工工作台：同一缝工单去重后只显示一条，顺序以评估意见安全优先级为准。"""
    return service.workbench()


# --------------------------------------------------------------- 缝台账

@router.get("", response_model=PageResult[dict])
def list_entries(
    keyword: str | None = Query(default=None, description="按缝编号检索"),
    status: str | None = Query(default=None, description="正常、堵塞、锚固损坏、已更换"),
    page: int = 1,
    size: int = 20,
) -> PageResult[dict]:
    """按缝编号与状态过滤伸缩缝管理列表；没有数据时返回空页，不报错。"""
    if size > 200:
        raise HTTPException(status_code=400, detail="每页最多 200 条，请缩小分页范围")
    items, total = service.list_entries(keyword=keyword, status=status, page=page, size=size)
    return PageResult(items=items, total=total, page=page, size=size)


@router.get("/{entry_id}", response_model=dict)
def get_entry(entry_id: int) -> dict:
    """读取单条伸缩缝明细；不存在时给出可读的错误说明。"""
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"伸缩缝 {entry_id} 不存在或已归档")
    return entry


@router.post("", response_model=ActionResult)
def create_entry(payload: EntryPayload) -> ActionResult:
    """登记一条伸缩缝，缺字段时说明原因而不是静默丢弃。"""
    entry, missing = service.create_entry(payload.values)
    if missing:
        return ActionResult(ok=False, message=f"缺少必填字段：{'、'.join(missing)}")
    return ActionResult(ok=True, message="伸缩缝已登记", entry=entry)


@router.post("/{entry_id}/actions", response_model=ActionResult)
def run_action(entry_id: int, payload: EntryPayload) -> ActionResult:
    """对单条伸缩缝执行清理堵塞、修补锚固、更换伸缩缝；更换动作走统一批次管线。"""
    action = str(payload.values.get("action") or "").strip()
    entry, message = service.run_action(entry_id, action)
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)


@router.get("/export")
def export_entries() -> dict[str, Any]:
    """导出伸缩缝管理清单：返回当前过滤条件下的全量数据。"""
    items, total = service.list_entries(page=1, size=10000)
    return {"module": "expansion", "total": total, "items": items}
