"""伸缩缝管理业务规则：缝台账与「成组更换」施工批次的统一推进都收在这里。

历史上成组更换之后，材料预占、缝台账、施工工作台各自推进各自的进度，
列表/详情对不上，重试还会重复生成同一缝工单。本模块把推进收拢成一条路径：

* ``advance_batch`` 是阶段推进的唯一入口：评估 → 预占 → 施工 → 验收，
  每一步都在同一个 ``store.transaction()`` 里联动改缝台账、批次工单与
  材料库存/预占，任一环节失败整批回滚；
* 批次按批次号幂等：同一批次重复提交、同一缝重复工单、同一材料重复预占
  都会被识别并跳过；
* 全局推进闸（``_advance_gate``）保证并发提交时只有一个批次能推进，
  其余提交得到 409，而不是互相覆盖；
* 安全优先顺序完全以评估意见为准（立即更换 > 限期更换 > 观察使用），
  更换验收时保留此前的缝宽记录，只在原有序列后追加复测记录；
* 旧批次仍处于「已预占」的材料占用会迁移到新批次并释放旧占用，
  避免同一材料被新旧两批同时占用。
"""
from __future__ import annotations

import threading
from datetime import date
from typing import Any

from app.store import store

MODULE = "expansion"
MATERIAL_MODULE = "material"
BATCH_MODULE = "expansion_batch"

REQUIRED_FIELDS = ["缝编号", "所属桥梁", "缝类型"]
STATUS_ORDER = ["正常", "堵塞", "锚固损坏", "已更换"]
ACTION_RULES = {"清理堵塞": "正常", "修补锚固": "正常", "更换伸缩缝": "已更换"}
NEGATIVE_ACTIONS = []

# 成组更换的统一阶段序列：批次 stage 只允许沿这四个阶段单向往前。
STAGES = ["评估", "预占", "施工", "验收"]
FINISHED_STAGE = "验收"

# 安全优先顺序以评估意见为准，序号越小越优先；未知意见排在最后。
EVALUATION_PRIORITY = {"立即更换": 1, "限期更换": 2, "观察使用": 3}
DEFAULT_PRIORITY = 99

REPLACED_STATUS = "已更换"
# 工单状态沿阶段推进，工作台只以 batch 内工单为唯一来源，不再另存一份进度。
ORDER_STATES = {"评估": "评估通过", "预占": "评估通过", "施工": "施工中", "验收": "已更换"}


class ServiceError(Exception):
    """业务校验失败：调用方应转成 400，并保证事务已回滚。"""

    def __init__(self, message: str, status_code: int = 400) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code


class BatchBusyError(ServiceError):
    """已有批次正在推进（并发提交）：调用方应转成 409。"""

    def __init__(self, message: str = "已有施工批次正在推进，请稍后再试") -> None:
        super().__init__(message, status_code=409)


def _joint_priority(joint: dict[str, Any]) -> tuple[int, int]:
    opinion = str(joint.get("评估意见") or "").strip()
    return EVALUATION_PRIORITY.get(opinion, DEFAULT_PRIORITY), int(joint.get("id", 0))


def _today() -> str:
    return date.today().isoformat()


class ExpansionService:
    def __init__(self) -> None:
        # 全局推进闸：任意时刻只允许一个批次执行阶段推进事务。
        self._advance_gate = threading.Lock()

    # ------------------------------------------------------------------ 台账

    def list_entries(
        self,
        *,
        keyword: str | None = None,
        status: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        rows = store.rows(MODULE)
        if keyword:
            rows = [row for row in rows if keyword in str(row.get("缝编号", ""))]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        total = len(rows)
        start = max(page - 1, 0) * size
        return rows[start:start + size], total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        return store.find(MODULE, entry_id)

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        missing = [field for field in REQUIRED_FIELDS if not str(values.get(field) or "").strip()]
        if missing:
            return None, missing
        with store.transaction():
            rows = store.rows(MODULE)
            entry: dict[str, Any] = {"id": store.next_id(MODULE)}
            entry.update({field: values.get(field) for field in REQUIRED_FIELDS})
            entry["status"] = STATUS_ORDER[0]
            entry["pending"] = True
            entry["abnormal"] = False
            # 缝台账新字段：评估意见决定安全优先顺序，缝宽记录是不可覆盖的历史序列。
            entry["评估意见"] = str(values.get("评估意见") or "观察使用").strip()
            entry["最近批次号"] = None
            entry["更换结论"] = None
            entry["缝宽记录"] = []
            current_width = str(values.get("当前缝宽") or "").strip()
            if current_width:
                entry["当前缝宽"] = current_width
                entry["缝宽记录"].append(
                    {"日期": _today(), "缝宽": current_width, "批次号": None, "说明": "登记测量"}
                )
            rows.append(entry)
        return entry, []

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"伸缩缝 {entry_id} 不存在或已归档"
        if action not in ACTION_RULES:
            return None, f"动作「{action}」不属于伸缩缝管理可执行范围"
        target = ACTION_RULES[action]
        if target not in STATUS_ORDER:
            return None, f"目标状态「{target}」不在允许的状态序列里"
        if action == "更换伸缩缝":
            # 单缝更换也走统一批次管线，结论写库口径与成组更换完全一致。
            try:
                entry = self.replace_single(entry_id)
            except ServiceError as exc:
                return None, exc.message
            return entry, "伸缩缝已更换"
        entry["status"] = target
        entry["缝状态"] = target
        entry["pending"] = target != STATUS_ORDER[-1]
        entry["abnormal"] = action in NEGATIVE_ACTIONS
        return entry, f"伸缩缝已{action}"

    # ------------------------------------------------------------ 批次查询

    def list_batches(self) -> list[dict[str, Any]]:
        return store.rows(BATCH_MODULE)

    def get_batch(self, batch_id: int) -> dict[str, Any] | None:
        return store.find(BATCH_MODULE, batch_id)

    def _find_batch_by_no(self, batch_no: str) -> dict[str, Any] | None:
        for batch in store.rows(BATCH_MODULE):
            if batch.get("批次号") == batch_no:
                return batch
        return None

    def workbench(self) -> dict[str, Any]:
        """施工工作台：工单只从批次工单里取，按缝编号去重，杜绝同一缝重复显示。"""
        orders: list[dict[str, Any]] = []
        seen_joint_ids: set[int] = set()
        for batch in store.rows(BATCH_MODULE):
            for order in batch.get("orders", []):
                joint_id = int(order["缝id"])
                if joint_id in seen_joint_ids:
                    continue
                seen_joint_ids.add(joint_id)
                view = dict(order)
                view["批次号"] = batch["批次号"]
                view["阶段"] = batch["stage"]
                view["批次状态"] = batch["status"]
                orders.append(view)
        # 工作台排序同样以评估意见的安全优先顺序为准。
        orders.sort(key=lambda item: (int(item.get("安全优先顺序", DEFAULT_PRIORITY)), int(item["缝id"])))
        return {"total": len(orders), "items": orders}

    # ------------------------------------------------------------ 批次开立

    def create_batch(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, str]:
        joint_ids = self._extract_joint_ids(values)
        if not joint_ids:
            return None, "成组更换至少要选择一条伸缩缝"
        batch_no = str(values.get("批次号") or "").strip()
        remark = str(values.get("remark") or values.get("备注") or "").strip()
        material_demand = values.get("材料需求")
        if material_demand is not None and not isinstance(material_demand, list):
            return None, "材料需求需为材料编号列表"

        with store.transaction():
            joints: list[dict[str, Any]] = []
            for joint_id in joint_ids:
                joint = store.find(MODULE, joint_id)
                if joint is None:
                    raise ServiceError(f"伸缩缝 {joint_id} 不存在或已归档", status_code=404)
                joints.append(joint)

            if batch_no:
                existing = self._find_batch_by_no(batch_no)
                if existing is not None:
                    # 批次号幂等：重试不再建工单；评估阶段允许补挂新缝。
                    merged = self._merge_joints(existing, joints)
                    note = "已补挂新增伸缩缝" if merged else "批次已存在，直接返回"
                    return existing, f"批次 {batch_no} {note}"
            else:
                batch_no = self._next_batch_no()

            joints.sort(key=_joint_priority)
            batch: dict[str, Any] = {
                "id": store.next_id(BATCH_MODULE),
                "批次号": batch_no,
                "stage": STAGES[0],
                "status": "进行中",
                "来源": "成组更换",
                "缝ids": [int(joint["id"]) for joint in joints],
                "orders": [],
                "材料计划": [],
                "材料需求": list(material_demand or []),
                "remark": remark,
                "pending": True,
                "abnormal": False,
                "开立日期": _today(),
                "更新时间": _today(),
            }
            store.rows(BATCH_MODULE).append(batch)
            # 开立即进入评估阶段：安全排序、工单序号与材料计划在同一事务定稿，
            # 之后三个阶段只消费这些产物，避免「进了评估阶段却没有评估结果」。
            self._stage_evaluate(batch)
        return batch, f"施工批次 {batch_no} 已开立，进入评估阶段"

    def _extract_joint_ids(self, values: dict[str, Any]) -> list[int]:
        raw = values.get("缝ids") or values.get("joint_ids") or []
        if isinstance(raw, str):
            raw = [part.strip() for part in raw.split(",") if part.strip()]
        if not isinstance(raw, (list, tuple)):
            return []
        joint_ids: list[int] = []
        for part in raw:
            try:
                joint_id = int(part)
            except (TypeError, ValueError):
                raise ServiceError(f"缝标识无法识别：{part!r}")
            if joint_id not in joint_ids:
                joint_ids.append(joint_id)
        return joint_ids

    def _merge_joints(self, batch: dict[str, Any], joints: list[dict[str, Any]]) -> bool:
        if batch["stage"] != STAGES[0]:
            return False
        current = set(batch["缝ids"])
        added = [int(joint["id"]) for joint in joints if int(joint["id"]) not in current]
        if not added:
            return False
        batch["缝ids"].extend(added)
        batch["更新时间"] = _today()
        # 仍在评估阶段：补挂的缝立即并入安全排序与材料计划。
        merged_joints = [store.find(MODULE, joint_id) for joint_id in batch["缝ids"]]
        batch["材料计划"] = self._build_material_plan(batch, merged_joints)  # type: ignore[arg-type]
        self._stage_evaluate(batch)
        return True

    def _next_batch_no(self) -> str:
        prefix = f"BATCH-{_today().replace('-', '')}-"
        seq = 1
        for batch in store.rows(BATCH_MODULE):
            no = str(batch.get("批次号") or "")
            if no.startswith(prefix):
                try:
                    seq = max(seq, int(no[len(prefix):]) + 1)
                except ValueError:
                    continue
        return f"{prefix}{seq:03d}"

    # ------------------------------------------------------------ 统一推进

    def replace_single(self, joint_id: int) -> dict[str, Any]:
        """单缝「更换伸缩缝」动作：幂等地走完整条批次管线。"""
        joint = store.find(MODULE, joint_id)
        if joint is None:
            raise ServiceError(f"伸缩缝 {joint_id} 不存在或已归档", status_code=404)
        auto_no = f"AUTO-J{joint_id}"
        if not self._gate_acquire():
            raise BatchBusyError()
        try:
            with store.transaction():
                batch = self._find_batch_by_no(auto_no)
                if batch is None:
                    # 已在任意批次更换过的缝不再重复开立自动批次（单缝/成组共用此防线）。
                    if joint.get("status") == REPLACED_STATUS and joint.get("最近批次号"):
                        return joint
                    batch = {
                        "id": store.next_id(BATCH_MODULE),
                        "批次号": auto_no,
                        "stage": STAGES[0],
                        "status": "进行中",
                        "来源": "单缝更换",
                        "缝ids": [joint_id],
                        "orders": [],
                        "材料计划": [],
                        "材料需求": [],
                        "remark": "单缝更换动作自动生成",
                        "pending": True,
                        "abnormal": False,
                        "开立日期": _today(),
                        "更新时间": _today(),
                    }
                    store.rows(BATCH_MODULE).append(batch)
                self._stage_evaluate(batch)
                self._advance_to(batch, STAGES.index(FINISHED_STAGE))
            return store.find(MODULE, joint_id) or joint
        finally:
            self._advance_gate.release()

    def advance_batch(
        self,
        batch_id: int,
        *,
        remeasured_widths: dict[int, str] | None = None,
        stage_committed: Any = None,
    ) -> tuple[dict[str, Any], bool, str]:
        """把批次向前推进一个阶段（已完成则幂等返回）。

        ``stage_committed`` 是阶段变更落库后、事务提交前的回调，仅用于
        并发测试制造「另一提交同时到达」的窗口。
        """
        batch = store.find(BATCH_MODULE, batch_id)
        if batch is None:
            raise ServiceError(f"施工批次 {batch_id} 不存在", status_code=404)
        if batch["stage"] == FINISHED_STAGE:
            # 幂等：重复提交完工批次不再产生任何工单/预占/结论。
            return batch, False, f"批次 {batch['批次号']} 已完成验收，无需重复推进"
        if not self._gate_acquire():
            raise BatchBusyError()
        acquired = True
        try:
            with store.transaction():
                # 拿锁后复检：可能在等锁前已有提交把批次推过了一个阶段。
                fresh = store.find(BATCH_MODULE, batch_id)
                if fresh is not None and fresh["stage"] == FINISHED_STAGE:
                    return fresh, False, f"批次 {fresh['批次号']} 已完成验收，无需重复推进"
                assert fresh is not None
                advanced = self._advance_to(
                    fresh, STAGES.index(fresh["stage"]) + 1, remeasured_widths, stage_committed
                )
            if not advanced:
                return fresh, False, f"批次 {fresh['批次号']} 已在「{fresh['stage']}」阶段"
            return fresh, True, f"批次 {fresh['批次号']} 已推进至「{fresh['stage']}」阶段"
        finally:
            # 只能释放自己抢到的闸：locked() 对持有者之外的线程也为 True，
            # 直接判断会把别人的推进闸误释放。
            if acquired:
                self._advance_gate.release()

    def _gate_acquire(self) -> bool:
        return self._advance_gate.acquire(blocking=False)

    def _advance_to(
        self,
        batch: dict[str, Any],
        target_index: int,
        remeasured_widths: dict[int, str] | None = None,
        stage_committed: Any = None,
    ) -> bool:
        """在调用方已经持有推进闸与事务的前提下，顺序推进到目标阶段。"""
        current_index = STAGES.index(batch["stage"])
        if target_index <= current_index:
            return False
        for stage in STAGES[current_index + 1:target_index + 1]:
            self._apply_stage(batch, stage, remeasured_widths or {})
            batch["stage"] = stage
            batch["更新时间"] = _today()
            if stage == FINISHED_STAGE:
                batch["status"] = "已完成"
                batch["pending"] = False
            if stage_committed is not None:
                stage_committed(batch, stage)
        return True

    # -------------------------------------------------------- 各阶段落库

    def _apply_stage(
        self, batch: dict[str, Any], stage: str, remeasured_widths: dict[int, str]
    ) -> None:
        if stage == "评估":
            self._stage_evaluate(batch)
        elif stage == "预占":
            self._stage_reserve(batch)
        elif stage == "施工":
            self._stage_construct(batch)
        elif stage == "验收":
            self._stage_accept(batch, remeasured_widths)
        else:  # pragma: no cover - STAGES 已限定取值
            raise ServiceError(f"未知施工阶段：{stage}")

    def _stage_evaluate(self, batch: dict[str, Any]) -> None:
        # 安全优先顺序只认评估意见：按（意见优先级, 缝id）稳定排序生成工单序号。
        joints = sorted(
            (store.find(MODULE, joint_id) for joint_id in batch["缝ids"]),
            key=lambda item: _joint_priority(item or {"id": 0}),
        )
        existing_orders = {int(order["缝id"]) for order in batch.get("orders", [])}
        orders = batch.setdefault("orders", [])
        for index, joint in enumerate(joints, start=1):
            assert joint is not None
            joint_id = int(joint["id"])
            if joint_id in existing_orders:
                # 重试幂等：同一缝工单只保留一条，只校正序号与优先顺序。
                order = next(item for item in orders if int(item["缝id"]) == joint_id)
            else:
                order = {"缝id": joint_id, "更换结论": None}
                orders.append(order)
            order["缝编号"] = joint.get("缝编号")
            order["所属桥梁"] = joint.get("所属桥梁")
            order["评估意见"] = joint.get("评估意见") or "观察使用"
            order["安全优先顺序"] = EVALUATION_PRIORITY.get(
                str(order["评估意见"]), DEFAULT_PRIORITY
            )
            order["序号"] = index
            order["状态"] = ORDER_STATES["评估"]
        orders.sort(key=lambda item: (int(item["安全优先顺序"]), int(item["缝id"])))
        # 材料计划在评估阶段定稿：默认按缝型匹配伸缩缝材料，每条缝各 1 件。
        if not batch.get("材料计划"):
            batch["材料计划"] = self._build_material_plan(batch, joints)

    def _build_material_plan(
        self, batch: dict[str, Any], joints: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        demand_codes = [str(code).strip() for code in batch.get("材料需求") or [] if str(code).strip()]
        materials = store.rows(MATERIAL_MODULE)
        chosen: dict[int, dict[str, Any]] = {}
        if demand_codes:
            for code in demand_codes:
                matched = next((m for m in materials if m.get("材料编号") == code), None)
                if matched is None:
                    raise ServiceError(f"材料编号 {code} 不存在，无法制定预占计划")
                chosen[int(matched["id"])] = matched
        plan: list[dict[str, Any]] = []
        for joint in joints:
            joint_type = str(joint.get("缝类型") or "").strip()
            for material in materials:
                if demand_codes and int(material["id"]) not in chosen:
                    continue
                applies = material.get("适用缝型") in (joint_type, "通用")
                if not applies or str(material.get("材料类别") or "") != "伸缩缝材料":
                    continue
                plan.append({
                    "材料id": int(material["id"]),
                    "材料编号": material.get("材料编号"),
                    "材料名称": material.get("材料名称"),
                    "缝id": int(joint["id"]),
                    "缝编号": joint.get("缝编号"),
                    "数量": 1,
                    "状态": "待预占",
                    "迁移自": None,
                })
        if not plan:
            raise ServiceError("所选伸缩缝没有匹配的库存材料，无法制定预占计划")
        return plan

    def _stage_reserve(self, batch: dict[str, Any]) -> None:
        # 先迁移释放旧批次占用，再补新占用；全部在同一事务里，库存不平就整体回滚。
        for line in batch["材料计划"]:
            material = store.find(MATERIAL_MODULE, int(line["材料id"]))
            if material is None:
                raise ServiceError(f"材料 {line.get('材料编号')} 已下架，无法预占")
            details = material.setdefault("预占明细", [])
            active_here = next(
                (d for d in details
                 if d.get("批次号") == batch["批次号"]
                 and int(d.get("缝id", 0)) == int(line["缝id"])
                 and d.get("状态") in ("已预占", "已消耗")),
                None,
            )
            if active_here is not None:
                # 重试幂等：本批次对该（材料, 缝）的预占已存在，不重复扣库存。
                line["状态"] = "已预占"
                continue
            # 迁移只负责释放旧占用，释放出的额度可直接复用；随后按整行数量
            # 重新预占，最终预占 = 释放后预占 + 本行数量。
            migrated = self._release_old_occupation(material, line, batch["批次号"])
            quantity = int(line["数量"])
            occupied = int(material.get("预占数量", 0))
            stock = int(material.get("库存数量", 0))
            # 真正需要新增占用的只有「本行数量 - 迁移释放额度」之外的部分。
            if stock - occupied < quantity - migrated:
                raise ServiceError(
                    f"材料 {material.get('材料编号')} 可用库存不足：需 {quantity} 件，"
                    f"旧占用可迁移 {migrated} 件，可用仅剩 {stock - occupied} 件"
                )
            material["预占数量"] = occupied + quantity
            details.append({
                "批次号": batch["批次号"],
                "缝id": int(line["缝id"]),
                "缝编号": line.get("缝编号"),
                "数量": quantity,
                "状态": "已预占",
                "迁移自": line["迁移自"],
            })
            line["状态"] = "已预占"
            material["status"] = "已预占"
            material["材料状态"] = "已预占"

    def _release_old_occupation(
        self, material: dict[str, Any], line: dict[str, Any], batch_no: str
    ) -> int:
        """释放旧批次挂在同一（材料, 缝）上的预占，返回迁移释放的数量。"""
        released = 0
        for detail in material.get("预占明细", []):
            if detail.get("状态") != "已预占":
                continue
            if detail.get("批次号") == batch_no:
                continue
            if int(detail.get("缝id", -1)) != int(line["缝id"]):
                continue
            quantity = int(detail.get("数量", 0))
            if quantity <= 0:
                continue
            # 旧占用释放：明细标记迁移，预占计数随之回落；新占用由调用方统一挂账。
            detail["状态"] = "已迁移释放"
            detail["释放至批次"] = batch_no
            material["预占数量"] = int(material.get("预占数量", 0)) - quantity
            released += quantity
            if line.get("迁移自") is None:
                line["迁移自"] = detail.get("批次号")
        return released

    def _stage_construct(self, batch: dict[str, Any]) -> None:
        # 工单进入施工中；工作台只读这里的状态，不再维护第二份进度。
        for order in batch["orders"]:
            order["状态"] = ORDER_STATES["施工"]

    def _stage_accept(
        self, batch: dict[str, Any], remeasured_widths: dict[int, str]
    ) -> None:
        batch_no = batch["批次号"]
        for order in batch["orders"]:
            joint_id = int(order["缝id"])
            joint = store.find(MODULE, joint_id)
            if joint is None:
                raise ServiceError(f"伸缩缝 {joint_id} 已归档，无法落更换结论")
            # 幂等：本批次已落过结论的缝直接跳过，重试不重复写历史。
            already = joint.get("最近批次号") == batch_no and joint.get("status") == REPLACED_STATUS
            if not already:
                history = joint.setdefault("缝宽记录", [])
                old_width = str(joint.get("当前缝宽") or "")
                width = str(remeasured_widths.get(joint_id) or old_width)
                # 维持此前缝宽记录：只在原有序列后追加，复测缺省就沿用更换前缝宽。
                history.append({
                    "日期": _today(),
                    "缝宽": width,
                    "批次号": batch_no,
                    "说明": "成组更换复测" if remeasured_widths.get(joint_id) else "成组更换，沿用更换前缝宽",
                })
                if remeasured_widths.get(joint_id):
                    joint["当前缝宽"] = width
                joint["status"] = REPLACED_STATUS
                joint["缝状态"] = REPLACED_STATUS
                joint["pending"] = False
                joint["abnormal"] = False
                joint["堵塞情况"] = "无"
                joint["锚固状态"] = "完好"
                joint["最近批次号"] = batch_no
                conclusion = (
                    f"{batch_no} 成组更换完成，安全优先顺序第 {order['序号']} 位"
                    f"（评估意见：{order['评估意见']}）"
                )
                joint["更换结论"] = conclusion
                order["更换结论"] = conclusion
            order["状态"] = ORDER_STATES["验收"]
        # 材料预占转消耗：结论同步落到物资清单（库存、预占、更换记录同一事务）。
        self._consume_materials(batch)

    def _consume_materials(self, batch: dict[str, Any]) -> None:
        batch_no = batch["批次号"]
        for line in batch["材料计划"]:
            material = store.find(MATERIAL_MODULE, int(line["材料id"]))
            if material is None:
                continue
            consumed = 0
            for detail in material.get("预占明细", []):
                if detail.get("批次号") != batch_no or detail.get("状态") != "已预占":
                    continue
                if int(detail.get("缝id", -1)) != int(line["缝id"]):
                    continue
                quantity = int(detail.get("数量", 0))
                detail["状态"] = "已消耗"
                material["预占数量"] = int(material.get("预占数量", 0)) - quantity
                material["库存数量"] = int(material.get("库存数量", 0)) - quantity
                consumed += quantity
            if consumed:
                line["状态"] = "已消耗"
                records = material.setdefault("更换记录", [])
                if not any(r.get("批次号") == batch_no and int(r.get("缝id", -1)) == int(line["缝id"])
                           for r in records):
                    records.append({
                        "批次号": batch_no,
                        "缝id": int(line["缝id"]),
                        "缝编号": line.get("缝编号"),
                        "数量": consumed,
                        "日期": _today(),
                    })
                material["最近批次号"] = batch_no
                material["更换结论"] = f"{batch_no} 成组更换已领用消耗，共 {consumed} 件"
            stock = int(material.get("库存数量", 0))
            occupied = int(material.get("预占数量", 0))
            if occupied > 0:
                material["status"] = "已预占"
                material["材料状态"] = "已预占"
            elif stock <= 0:
                material["status"] = "已领用"
                material["材料状态"] = "已领用"
            else:
                material["status"] = "在库"
                material["材料状态"] = "在库"


service = ExpansionService()
