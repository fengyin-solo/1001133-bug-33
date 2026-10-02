"""伸缩缝「成组更换」统一编排：提交预占 → 评估排序 → 更换落账。

背景：更换一组伸缩缝以前散落在材料预占、缝台账、施工工作台三处各自推进，
三处进度对不上、重试还会把同一条缝的工单重复建出来。这里把整条链路收拢成唯一入口：

- 任务以「施工批次号」幂等：同一批次重复提交/评估/落账都回显既有结果，
  不会重复预占、重复建单、重复扣料；
- 每个阶段都在单个事务里同时改「批次阶段 + 工单状态 + 材料库存/预占 + 缝台账」，
  任一步失败整体回滚，不留下半成品；
- 同一施工批次的并发推进串行化，只放一个请求进入临界区；
- 安全优先顺序以评估意见为准（紧急 > 严重 > 一般），同档按缝编号稳定排序；
- 落账只改结论字段，**不覆盖既有「当前缝宽」**；
- 旧材料占用在落账时从旧批次迁移并释放，恢复为可用库存。

列表 / 详情 / 工作台读取的都是这里写入的同一份批次与工单数据，因此天然一致。
"""
from __future__ import annotations

from typing import Any, Callable

from app.store import store

EXPANSION_MODULE = "expansion"
MATERIAL_MODULE = "material"
BATCH_MODULE = "replacement_batch"
ORDER_MODULE = "replacement_order"

# 三个阶段单向推进。
STAGE_SUBMITTED = "已提交"
STAGE_ASSESSED = "已评估待更换"
STAGE_DONE = "已完成"

# 工单随批次阶段流转，列表/详情/工作台看到的状态都从这里推导。
ORDER_STAGED = "已预占待评估"
ORDER_WAITING = "待更换"
ORDER_DONE = "已更换"

# 安全优先顺序以评估意见为准：紧急 > 严重 > 一般；未命中关键字归入一般。
PRIORITY_RANK = {"紧急": 0, "严重": 1, "一般": 2}
DEFAULT_PRIORITY = 2


class ReplacementError(Exception):
    """业务规则不满足时抛出；事务回滚后由对外方法转成可读消息。"""


def _as_int(value: Any, field: str) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        raise ReplacementError(f"参数「{field}」必须是整数") from None
    if number <= 0:
        raise ReplacementError(f"参数「{field}」必须大于 0")
    return number


def _as_nonneg_int(value: Any, field: str) -> int:
    """库存/预占等存量字段允许为 0，只要求是非负整数。"""
    try:
        number = int(value)
    except (TypeError, ValueError):
        raise ReplacementError(f"参数「{field}」必须是整数") from None
    if number < 0:
        raise ReplacementError(f"参数「{field}」不能为负数")
    return number


def _priority_rank(opinion: str) -> int:
    for keyword, rank in PRIORITY_RANK.items():
        if keyword in opinion:
            return rank
    return DEFAULT_PRIORITY


def _next_id(module: str) -> int:
    return max((int(row.get("id", 0)) for row in store.rows(module)), default=0) + 1


class ReplacementService:
    # ---------- 读取：列表、详情、工作台共用同一份事实 ----------
    def _find_batch(self, batch_no: str) -> dict[str, Any] | None:
        for row in store.rows(BATCH_MODULE):
            if row.get("施工批次号") == batch_no:
                return row
        return None

    def _orders_of(self, batch_no: str) -> list[dict[str, Any]]:
        return [
            row for row in store.rows(ORDER_MODULE)
            if row.get("施工批次号") == batch_no
        ]

    def _order_view(self, order: dict[str, Any]) -> dict[str, Any]:
        return dict(order)

    def _batch_view(self, batch: dict[str, Any]) -> dict[str, Any]:
        """组装批次视图：阶段与工单状态从同一处推导，保证三处看到的进度一致。"""
        orders = self._orders_of(str(batch["施工批次号"]))
        view = {k: v for k, v in batch.items() if k != "id"}
        view["工单列表"] = [self._order_view(order) for order in orders]
        view["缝数量"] = len(orders)
        view["已更换数量"] = sum(1 for order in orders if order["工单状态"] == ORDER_DONE)
        return view

    def get_batch(self, batch_no: str) -> dict[str, Any] | None:
        batch = self._find_batch(batch_no)
        return self._batch_view(batch) if batch else None

    def list_batches(self, *, page: int = 1, size: int = 20) -> tuple[list[dict[str, Any]], int]:
        rows = list(store.rows(BATCH_MODULE))
        total = len(rows)
        start = max(page - 1, 0) * size
        return [self._batch_view(row) for row in rows[start:start + size]], total

    def workbench(self) -> list[dict[str, Any]]:
        """施工工作台：按安全优先顺序返回各批次待更换工单（顺序来自评估意见）。"""
        result: list[dict[str, Any]] = []
        for batch in store.rows(BATCH_MODULE):
            if batch["批次阶段"] != STAGE_ASSESSED:
                continue
            for order in self._orders_of(str(batch["施工批次号"])):
                if order["工单状态"] == ORDER_WAITING:
                    result.append(self._order_view(order))
        result.sort(key=lambda order: (
            _priority_rank(str(order.get("评估意见", ""))),
            int(order.get("安全顺序", 0)),
            str(order.get("缝编号", "")),
        ))
        return result

    # ---------- 推进壳：同批次串行 + 事务原子提交 + 提交后重读 ----------
    def _locked(self, batch_no: str, handler: Callable[[], str]) -> tuple[dict[str, Any] | None, str]:
        try:
            with store.batch_advance(batch_no):
                with store.transaction():
                    message = handler()
                # 事务已提交，再读一次权威视图，列表/详情/返回值同源不打架。
                return self.get_batch(batch_no), message
        except _Idempotent:
            # 幂等回显分支：本就没有写入，放行给各阶段方法按成功回显既有结果。
            raise
        except ReplacementError as exc:
            return None, str(exc)

    # ---------- 阶段一：提交（预占新材料） ----------
    def submit(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, str]:
        batch_no = str(values.get("施工批次号") or "").strip()
        if not batch_no:
            return None, "缺少施工批次号，无法保证成组更换幂等"
        try:
            seam_ids = _normalize_seam_ids(values.get("缝id列表") or values.get("seams"))
            material_items = _normalize_materials(values.get("材料列表") or [])
        except ReplacementError as exc:
            return None, str(exc)

        def handler() -> str:
            existing = self._find_batch(batch_no)
            if existing is not None:
                # 幂等：同批次重复提交不预占、不建单，用标记跳过后续写入。
                raise _Idempotent(f"批次「{batch_no}」已提交，沿用既有预占，不重复建单")

            batch = {
                "id": _next_id(BATCH_MODULE),
                "施工批次号": batch_no,
                "批次名称": str(values.get("批次名称") or batch_no),
                "所属桥梁": str(values.get("所属桥梁") or ""),
                "批次阶段": STAGE_SUBMITTED,
                "材料预占": [],
            }

            seam_rows = [store.find(EXPANSION_MODULE, seam_id) for seam_id in seam_ids]
            missing = [sid for sid, row in zip(seam_ids, seam_rows) if row is None]
            if missing:
                raise ReplacementError(f"伸缩缝 {', '.join(map(str, missing))} 不存在，无法成组提交")
            replaced = [str(row["缝编号"]) for row in seam_rows if row and row.get("status") == "已更换"]
            if replaced:
                raise ReplacementError(f"伸缩缝 {', '.join(replaced)} 已更换，无需重复提交")

            # 预占新材料：库存足够才预占，否则随事务整体回滚，阶段与占用都不落半成品。
            pre_occupy: list[dict[str, int]] = []
            for item in material_items:
                material = store.find(MATERIAL_MODULE, item["材料id"])
                if material is None:
                    raise ReplacementError(f"养护材料 {item['材料id']} 不存在，无法预占")
                qty = item["数量"]
                stock = _as_nonneg_int(material.get("库存数量", 0), "库存数量")
                reserved = _as_nonneg_int(material.get("预占数量", 0), "预占数量")
                if reserved + qty > stock:
                    raise ReplacementError(
                        f"材料「{material.get('材料编号', item['材料id'])}」可用库存不足，"
                        f"需预占 {qty}，当前可用 {stock - reserved}"
                    )
                material["预占数量"] = reserved + qty
                material["占用批次"] = batch_no
                pre_occupy.append({"材料id": item["材料id"], "数量": qty})

            order_id = _next_id(ORDER_MODULE)
            for row in seam_rows:
                store.rows(ORDER_MODULE).append({
                    "id": order_id,
                    "缝id": row["id"],
                    "缝编号": row.get("缝编号"),
                    "所属桥梁": row.get("所属桥梁"),
                    "施工批次号": batch_no,
                    "工单状态": ORDER_STAGED,
                    "评估意见": "",
                    "安全顺序": 0,
                })
                order_id += 1

            batch["材料预占"] = pre_occupy
            store.rows(BATCH_MODULE).append(batch)
            return f"批次「{batch_no}」已提交，新材料已预占，等待安全评估"

        try:
            return self._locked(batch_no, handler)
        except _Idempotent as repeat:
            # 幂等回显走正常成功返回，不计为失败、也不产生任何写入。
            return self.get_batch(batch_no), str(repeat)

    # ---------- 阶段二：评估（安全优先顺序以评估意见为准） ----------
    def assess(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, str]:
        batch_no = str(values.get("施工批次号") or "").strip()
        if not batch_no:
            return None, "缺少施工批次号，无法定位评估批次"
        opinions = values.get("评估意见")
        if not isinstance(opinions, dict) or not opinions:
            return None, "缺少各缝的评估意见，无法确定安全优先顺序"

        def handler() -> str:
            batch = self._require_batch(batch_no)
            if batch["批次阶段"] in (STAGE_ASSESSED, STAGE_DONE):
                # 重试评估：结论已落，直接回显，不改顺序、不建工单。
                raise _Idempotent("安全评估已完成，沿用既有安全优先顺序")
            if batch["批次阶段"] != STAGE_SUBMITTED:
                raise ReplacementError(f"批次「{batch_no}」当前为「{batch['批次阶段']}」，不能评估")

            orders = self._orders_of(batch_no)
            by_seam = {str(order["缝id"]): order for order in orders}
            unknown = [key for key in opinions if key not in by_seam]
            if unknown:
                raise ReplacementError(f"评估意见里的缝 {', '.join(unknown)} 不在本批次工单内")
            missing = [str(order["缝id"]) for order in orders
                       if not str(opinions.get(str(order["缝id"]), "")).strip()]
            if missing:
                raise ReplacementError(f"伸缩缝 {', '.join(missing)} 缺少评估意见")

            for order in orders:
                order["评估意见"] = str(opinions[str(order["缝id"])]).strip()

            # 紧急 > 严重 > 一般，同档按缝编号稳定排序。
            ranked = sorted(
                orders,
                key=lambda order: (_priority_rank(order["评估意见"]), str(order.get("缝编号", ""))),
            )
            for index, order in enumerate(ranked, start=1):
                order["安全顺序"] = index
                order["工单状态"] = ORDER_WAITING

            batch["批次阶段"] = STAGE_ASSESSED
            return "安全评估完成，已按评估意见确定更换顺序"

        try:
            return self._locked(batch_no, handler)
        except _Idempotent as repeat:
            return self.get_batch(batch_no), str(repeat)

    # ---------- 阶段三：落账（结论回写台账、释放旧占用、预占转消耗） ----------
    def confirm(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, str]:
        batch_no = str(values.get("施工批次号") or "").strip()
        if not batch_no:
            return None, "缺少施工批次号，无法定位更换批次"

        def handler() -> str:
            batch = self._require_batch(batch_no)
            if batch["批次阶段"] == STAGE_DONE:
                # 重试落账：不重复扣料、不重复写结论，直接回显既有完成结果。
                raise _Idempotent(f"批次「{batch_no}」已完成更换，沿用既有落账结果")
            if batch["批次阶段"] != STAGE_ASSESSED:
                raise ReplacementError(f"批次「{batch_no}」尚未完成安全评估，不能更换落账")

            orders = self._orders_of(batch_no)
            if not orders:
                raise ReplacementError("该批次没有工单，无法落账")
            if any(order["工单状态"] != ORDER_WAITING for order in orders):
                raise ReplacementError("存在未完成评估的工单，不能落账")

            seams = [store.find(EXPANSION_MODULE, int(order["缝id"])) for order in orders]
            if any(seam is None for seam in seams):
                raise ReplacementError("有伸缩缝已归档，无法落账")

            # 旧材料占用迁移释放：把指向旧批次的预占归还为可用库存，再解除缝上的占用标记。
            legacy_batches = {
                seam.get("占用批次") for seam in seams
                if seam.get("占用批次") and seam.get("占用批次") != batch_no
            }
            for legacy in legacy_batches:
                for material in store.rows(MATERIAL_MODULE):
                    if material.get("占用批次") != legacy:
                        continue
                    material["预占数量"] = 0
                    material["占用批次"] = None
                for seam in seams:
                    if seam.get("占用批次") == legacy:
                        seam["旧占用已释放"] = True
                        seam["占用批次"] = None

            replaced_codes: list[str] = []
            for order, seam in zip(orders, seams):
                opinion = str(order.get("评估意见", ""))
                # 更换结论落到缝台账（缝详情读取同一行）；明确不覆盖「当前缝宽」。
                seam["status"] = "已更换"
                seam["pending"] = False
                seam["abnormal"] = False
                seam["缝状态"] = "已更换"
                seam["更换批次"] = batch_no
                seam["评估意见"] = opinion
                seam["更换结论"] = f"按安全顺序第{order['安全顺序']}位更换：{opinion}"
                # 注意：此处刻意不写 seam["当前缝宽"]，维持此前缝宽记录。
                replaced_codes.append(str(seam.get("缝编号", order["缝id"])))
                order["工单状态"] = ORDER_DONE

            # 新材料预占转消耗：预占清 0、库存核减；阶段与物资在同一事务提交。
            for item in batch.get("材料预占", []):
                material = store.find(MATERIAL_MODULE, int(item["材料id"]))
                if material is None:
                    raise ReplacementError(f"养护材料 {item['材料id']} 已归档，无法核减")
                qty = _as_int(item["数量"], "预占数量")
                material["预占数量"] = max(0, _as_nonneg_int(material.get("预占数量", 0), "预占数量") - qty)
                material["库存数量"] = _as_nonneg_int(material.get("库存数量", 0), "库存数量") - qty
                if material.get("占用批次") == batch_no:
                    material["占用批次"] = None

            batch["批次阶段"] = STAGE_DONE
            return f"批次「{batch_no}」更换完成：{', '.join(replaced_codes)}"

        try:
            return self._locked(batch_no, handler)
        except _Idempotent as repeat:
            return self.get_batch(batch_no), str(repeat)

    def _require_batch(self, batch_no: str) -> dict[str, Any]:
        batch = self._find_batch(batch_no)
        if batch is None:
            raise ReplacementError(f"施工批次「{batch_no}」不存在或已归档")
        return batch


class _Idempotent(ReplacementError):
    """同批次重试的正常回显分支：触发事务回滚（本就没写入），对外按成功返回。"""


def _normalize_seam_ids(seams: Any) -> list[int]:
    if not isinstance(seams, list) or not seams:
        raise ReplacementError("缺少缝id列表，无法成组提交")
    ids: list[int] = []
    for raw in seams:
        if isinstance(raw, dict):
            raw = raw.get("缝id") if raw.get("缝id") is not None else raw.get("id")
        ids.append(_as_int(raw, "缝id"))
    if len(set(ids)) != len(ids):
        raise ReplacementError("同一批次内不能重复提交相同伸缩缝")
    return ids


def _normalize_materials(materials: Any) -> list[dict[str, int]]:
    if not isinstance(materials, list):
        raise ReplacementError("材料列表格式不正确")
    merged: dict[int, int] = {}
    for raw in materials:
        if not isinstance(raw, dict):
            raise ReplacementError("材料列表格式不正确")
        material_id = raw.get("材料id") if raw.get("材料id") is not None else raw.get("id")
        mid = _as_int(material_id, "材料id")
        merged[mid] = merged.get(mid, 0) + _as_int(raw.get("数量", 1), "数量")
    return [{"材料id": mid, "数量": qty} for mid, qty in merged.items()]


replacement_service = ReplacementService()
