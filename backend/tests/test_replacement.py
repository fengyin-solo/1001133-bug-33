"""成组更换统一编排的验收测试（仅标准库，离线可跑：python3 -m unittest -v）。

覆盖：
- 三处进度收拢：批次列表 / 批次详情 / 工作台读到同一阶段与工单；
- 幂等：提交、评估、落账按施工批次重试不重复预占/建单/扣料；
- 并发：同批次并发提交只允许一个推进（工单不重复）；
- 事务：预占库存不足时阶段与占用整体回滚；
- 安全优先顺序以评估意见为准（紧急>严重>一般，同档按缝编号）；
- 落账不覆盖既有「当前缝宽」，更换结论落到缝台账；
- 旧材料占用迁移释放；物资清单可用数量随之变化。
"""
from __future__ import annotations

import sys
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, ".")

from app.services.replacement import (  # noqa: E402
    ORDER_DONE,
    ORDER_WAITING,
    STAGE_ASSESSED,
    STAGE_DONE,
    STAGE_SUBMITTED,
    replacement_service as svc,
)
from app.services.material import MaterialService  # noqa: E402
from app.store import store  # noqa: E402

SEAM_IDS = [1, 2]
NEW_MATERIAL = 1


def _submit(batch_no: str = "BATCH-A", seam_ids=None, materials=None):
    return svc.submit({
        "施工批次号": batch_no,
        "批次名称": "成组更换A",
        "所属桥梁": "测试桥",
        "缝id列表": seam_ids if seam_ids is not None else SEAM_IDS,
        "材料列表": materials if materials is not None else [{"材料id": NEW_MATERIAL, "数量": 4}],
    })


def _assess(batch_no: str = "BATCH-A", opinions=None):
    if opinions is None:
        opinions = {"1": "一般，可择期", "2": "紧急，影响行车安全"}
    return svc.assess({"施工批次号": batch_no, "评估意见": opinions})


def _material_view(material_id: int):
    return MaterialService().get_entry(material_id)


class ReplacementFlowTests(unittest.TestCase):
    def setUp(self):
        store.reset()
        # 记录待更换缝的原始缝宽，用于断言落账不覆盖历史缝宽。
        self.original_widths = {
            sid: store.find("expansion", sid).get("当前缝宽") for sid in SEAM_IDS
        }

    def test_full_flow_lands_conclusion_on_ledger(self):
        batch, msg = _submit()
        self.assertIsNotNone(batch, msg)
        self.assertEqual(batch["批次阶段"], STAGE_SUBMITTED)
        self.assertEqual(len(batch["工单列表"]), 2)

        batch, msg = _assess()
        self.assertEqual(batch["批次阶段"], STAGE_ASSESSED)
        orders = {str(o["缝id"]): o for o in batch["工单列表"]}
        # 紧急的缝(2)排第1，一般的缝(1)排第2。
        self.assertEqual(orders["2"]["安全顺序"], 1)
        self.assertEqual(orders["1"]["安全顺序"], 2)
        self.assertTrue(all(o["工单状态"] == ORDER_WAITING for o in batch["工单列表"]))

        batch, msg = svc.confirm({"施工批次号": "BATCH-A"})
        self.assertIsNotNone(batch, msg)
        self.assertEqual(batch["批次阶段"], STAGE_DONE)
        self.assertTrue(all(o["工单状态"] == ORDER_DONE for o in batch["工单列表"]))

        # 更换结论落到缝台账（缝详情读同一行）。
        for sid in SEAM_IDS:
            seam = store.find("expansion", sid)
            self.assertEqual(seam["status"], "已更换")
            self.assertEqual(seam["缝状态"], "已更换")
            self.assertEqual(seam["更换批次"], "BATCH-A")
            self.assertIn("更换", seam["更换结论"])
            # 维持此前缝宽记录，不被落账覆盖。
            self.assertEqual(seam.get("当前缝宽"), self.original_widths[sid])

    def test_list_detail_workbench_share_one_source(self):
        _submit()
        _assess()
        listed, total = svc.list_batches()
        self.assertEqual(total, 1)
        detail = svc.get_batch("BATCH-A")
        # 列表与详情阶段、工单数、工单状态完全一致。
        self.assertEqual(listed[0]["批次阶段"], detail["批次阶段"])
        self.assertEqual(
            [o["工单状态"] for o in listed[0]["工单列表"]],
            [o["工单状态"] for o in detail["工单列表"]],
        )
        bench = svc.workbench()
        self.assertEqual({o["缝id"] for o in bench}, set(SEAM_IDS))
        # 工作台第一条是评估为紧急的缝2。
        self.assertEqual(bench[0]["缝id"], 2)
        # 落账后工作台不再出现该批次工单（不重复显示同一缝工单）。
        svc.confirm({"施工批次号": "BATCH-A"})
        self.assertEqual(svc.workbench(), [])

    def test_submit_is_idempotent_by_batch(self):
        first, _ = _submit()
        material_before = _material_view(NEW_MATERIAL)
        second, msg = _submit()
        material_after = _material_view(NEW_MATERIAL)
        self.assertIsNotNone(second)
        self.assertEqual(len(first["工单列表"]), len(second["工单列表"]))
        self.assertEqual(len(store.rows("replacement_order")), 2)  # 未重复建单
        self.assertEqual(material_before["预占数量"], material_after["预占数量"])  # 未重复预占

    def test_assess_and_confirm_retry_are_idempotent(self):
        _submit()
        _assess()
        batch1, _ = _assess()  # 重试评估
        self.assertEqual(batch1["批次阶段"], STAGE_ASSESSED)
        order_snapshot = [(o["缝id"], o["安全顺序"]) for o in batch1["工单列表"]]

        svc.confirm({"施工批次号": "BATCH-A"})
        stock_after_confirm = _material_view(NEW_MATERIAL)["库存数量"]
        batch2, _ = svc.confirm({"施工批次号": "BATCH-A"})  # 重试落账
        self.assertEqual(batch2["批次阶段"], STAGE_DONE)
        self.assertEqual(
            order_snapshot,
            [(o["缝id"], o["安全顺序"]) for o in batch2["工单列表"]],
        )
        # 重试落账没有再次扣料。
        self.assertEqual(_material_view(NEW_MATERIAL)["库存数量"], stock_after_confirm)

    def test_concurrent_submit_only_one_batch_advances(self):
        barrier = threading.Barrier(8)

        def fire():
            barrier.wait()
            return _submit("BATCH-CONC")

        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(lambda _: fire(), range(8)))
        oks = [b for b, _ in results if b is not None]
        # 只有一个批次对象、工单恰好两条，绝不出现重复缝工单。
        self.assertEqual(len(store.rows("replacement_batch")), 1)
        self.assertEqual(len(store.rows("replacement_order")), 2)
        self.assertEqual(len(oks), 8)  # 其余请求幂等回显，也算成功
        self.assertEqual(_material_view(NEW_MATERIAL)["预占数量"], 4)  # 只预占一次

    def test_concurrent_confirm_consumes_material_once(self):
        _submit()
        _assess()
        stock_before = _material_view(NEW_MATERIAL)["库存数量"]
        barrier = threading.Barrier(6)

        def fire():
            barrier.wait()
            return svc.confirm({"施工批次号": "BATCH-A"})

        with ThreadPoolExecutor(max_workers=6) as pool:
            results = list(pool.map(lambda _: fire(), range(6)))
        self.assertTrue(all(b is not None for b, _ in results))
        self.assertEqual(_material_view(NEW_MATERIAL)["库存数量"], stock_before - 4)
        self.assertEqual(_material_view(NEW_MATERIAL)["预占数量"], 0)

    def test_insufficient_stock_rolls_back_stage_and_reservation(self):
        # 需要 999 超出可用库存：阶段不能留成「已提交」，也不能预占。
        reserved_before = _material_view(NEW_MATERIAL)["预占数量"]
        batch, msg = _submit("BATCH-SHORT", materials=[{"材料id": NEW_MATERIAL, "数量": 999}])
        self.assertIsNone(batch)
        self.assertIn("可用库存不足", msg)
        self.assertIsNone(svc.get_batch("BATCH-SHORT"))  # 批次阶段整体回滚
        self.assertEqual(len(store.rows("replacement_order")), 0)  # 工单也回滚
        self.assertEqual(_material_view(NEW_MATERIAL)["预占数量"], reserved_before)

    def test_invalid_stage_transition_rejected(self):
        # 未评估直接落账被拒，且不改动任何数据。
        _submit()
        batch, msg = svc.confirm({"施工批次号": "BATCH-A"})
        self.assertIsNone(batch)
        self.assertIn("安全评估", msg)
        self.assertEqual(svc.get_batch("BATCH-A")["批次阶段"], STAGE_SUBMITTED)

    def test_legacy_material_occupancy_migrated_and_released(self):
        # 构造旧批次对缝1的旧材料占用（材料3 被旧批次预占）。
        legacy_material = store.find("material", 3)
        legacy_material["库存数量"] = 20
        legacy_material["预占数量"] = 5
        legacy_material["占用批次"] = "BATCH-OLD"
        seam = store.find("expansion", 1)
        seam["占用批次"] = "BATCH-OLD"

        _submit("BATCH-NEW", seam_ids=[1], materials=[{"材料id": NEW_MATERIAL, "数量": 2}])
        _assess("BATCH-NEW", {"1": "严重，尽快更换"})
        svc.confirm({"施工批次号": "BATCH-NEW"})

        # 旧占用迁移释放：预占清零、占用批次解除。
        view = MaterialService().get_entry(3)
        self.assertEqual(view["预占数量"], 0)
        self.assertIsNone(view["占用批次"])
        self.assertEqual(view["可用数量"], 20)
        self.assertTrue(store.find("expansion", 1).get("旧占用已释放"))

    def test_material_list_reflects_available_quantity(self):
        view = _material_view(NEW_MATERIAL)
        self.assertEqual(view["可用数量"], view["库存数量"] - view["预占数量"])
        _submit(materials=[{"材料id": NEW_MATERIAL, "数量": 3}])
        view2 = _material_view(NEW_MATERIAL)
        self.assertEqual(view2["预占数量"], view["预占数量"] + 3)
        self.assertEqual(view2["可用数量"], view2["库存数量"] - view2["预占数量"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
