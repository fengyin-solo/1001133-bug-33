"""伸缩缝成组更换业务测试：不依赖 FastAPI，直接驱动服务层。

覆盖核心口径：
* 更换结论同时落到缝台账/缝详情（同一条缝记录）与物资清单；
* 安全优先顺序只以评估意见为准；缝宽记录只追加、不覆盖；
* 旧批次材料占用迁移释放，不与新批次双重计数；
* 批次按批次号幂等、重试不重复工单/预占/结论；
* 阶段推进与材料预占同事务，库存不足整体回滚；
* 并发提交只允许一个批次推进，其它得到 409。
"""
from __future__ import annotations

import threading
import unittest

from app.services import expansion as expansion_module
from app.services.expansion import (
    BATCH_MODULE,
    FINISHED_STAGE,
    MATERIAL_MODULE,
    MODULE,
    BatchBusyError,
    ExpansionService,
    ServiceError,
)
from app.store import store


class ExpansionBatchTestCase(unittest.TestCase):
    def setUp(self) -> None:
        store.reset()
        self.service = ExpansionService()

    def _create(self, joint_ids: list[int], batch_no: str = "BATCH-TEST-001"):
        batch, message = self.service.create_batch(
            {"批次号": batch_no, "缝ids": joint_ids}
        )
        self.assertIsNotNone(batch, message)
        return batch

    def _advance_all(self, batch_id: int, widths: dict[int, str] | None = None):
        # 开立即处于评估阶段，只需推进剩余三个阶段
        for _ in range(3):
            batch, _, _ = self.service.advance_batch(batch_id, remeasured_widths=widths)
        return batch

    def _joint(self, joint_id: int):
        return store.find(MODULE, joint_id)

    def _material(self, material_id: int):
        return store.find(MATERIAL_MODULE, material_id)

    # ------------------------------------------------------------ 主流程

    def test_full_flow_conclusion_lands_in_joint_and_material(self):
        # 缝 2（立即更换）与缝 3（限期更换），C 型材料各 1 件
        batch = self._create([2, 3])
        self.assertEqual(batch["stage"], "评估")

        batch, advanced, _ = self.service.advance_batch(batch["id"])
        self.assertTrue(advanced)
        self.assertEqual(batch["stage"], "预占")
        # 评估阶段即定序：立即更换在前
        self.assertEqual([o["缝id"] for o in batch["orders"]], [2, 3])
        self.assertEqual([o["序号"] for o in batch["orders"]], [1, 2])

        # 两条缝各占 C 型橡胶条与锚固组件各 1 件
        rubber = self._material(1)
        anchor = self._material(2)
        self.assertEqual(rubber["预占数量"], 2)
        self.assertEqual(anchor["预占数量"], 2)
        self.assertEqual(len(rubber["预占明细"]), 2)

        # 第二次推进进入施工：工单转施工中
        batch, advanced, _ = self.service.advance_batch(batch["id"])
        self.assertTrue(advanced)
        self.assertEqual(batch["stage"], "施工")
        self.assertEqual({o["状态"] for o in batch["orders"]}, {"施工中"})

        # 第三次推进验收：更换结论落缝台账与物资清单
        widths = {2: "20mm", 3: "50mm"}
        batch, advanced, _ = self.service.advance_batch(batch["id"], remeasured_widths=widths)
        self.assertTrue(advanced)
        self.assertEqual(batch["stage"], FINISHED_STAGE)
        # 结论落到缝台账（列表与详情读的是同一条记录）
        for joint_id, width in widths.items():
            joint = self._joint(joint_id)
            self.assertEqual(joint["status"], "已更换")
            self.assertEqual(joint["缝状态"], "已更换")
            self.assertEqual(joint["最近批次号"], "BATCH-TEST-001")
            self.assertIn("成组更换完成", joint["更换结论"])
            self.assertEqual(joint["当前缝宽"], width)
            self.assertFalse(joint["pending"])
            self.assertFalse(joint["abnormal"])

        # 结论同步到物资清单：预占转消耗，库存扣减，留有更换记录
        self.assertEqual(rubber["库存数量"], 8)
        self.assertEqual(rubber["预占数量"], 0)
        self.assertEqual(rubber["最近批次号"], "BATCH-TEST-001")
        self.assertIn("成组更换", rubber["更换结论"])
        self.assertEqual(len(rubber["更换记录"]), 2)
        self.assertTrue(all(d["状态"] == "已消耗" for d in rubber["预占明细"]))

    # ------------------------------------------------------------ 优先级

    def test_safety_priority_follows_evaluation_opinion_only(self):
        # 故意按 [限期更换, 立即更换, 观察使用] 的缝 id 顺序开立
        batch = self._create([3, 2, 1])
        self.service.advance_batch(batch["id"])  # 评估
        batch = self.service.get_batch(batch["id"])
        self.assertEqual([o["缝id"] for o in batch["orders"]], [2, 3, 1])
        self.assertEqual([o["安全优先顺序"] for o in batch["orders"]], [1, 2, 3])

    # -------------------------------------------------------- 缝宽记录保留

    def test_width_history_is_kept_and_appended(self):
        joint_before = self._joint(2)
        history_len_before = len(joint_before["缝宽记录"])
        old_width = joint_before["当前缝宽"]

        batch = self._create([2], "BATCH-WIDTH-001")
        self._advance_all(batch["id"])  # 不传复测缝宽

        joint = self._joint(2)
        self.assertEqual(len(joint["缝宽记录"]), history_len_before + 1)
        self.assertEqual(joint["缝宽记录"][0]["说明"], "定检测量")
        self.assertEqual(joint["缝宽记录"][-1]["批次号"], "BATCH-WIDTH-001")
        self.assertEqual(joint["缝宽记录"][-1]["缝宽"], old_width)
        self.assertEqual(joint["当前缝宽"], old_width)

    # ------------------------------------------------------------ 幂等性

    def test_batch_no_idempotent_and_no_duplicate_orders(self):
        batch1 = self._create([2, 3], "BATCH-IDEM-001")
        batch2 = self._create([2, 3], "BATCH-IDEM-001")
        self.assertEqual(batch1["id"], batch2["id"])
        self.service.advance_batch(batch1["id"])  # 评估
        batch = self.service.get_batch(batch1["id"])
        self.assertEqual(len(batch["orders"]), 2)

        # 同一批次一路推进后重复推进，不产生重复工单/预占/消耗
        self._advance_all(batch1["id"])
        self.service.advance_batch(batch1["id"])
        self.service.advance_batch(batch1["id"])

        batch = self.service.get_batch(batch1["id"])
        self.assertEqual(len(batch["orders"]), 2)
        rubber = self._material(1)
        self.assertEqual(rubber["库存数量"], 8)
        self.assertEqual(rubber["预占数量"], 0)
        self.assertEqual([d["状态"] for d in rubber["预占明细"]].count("已消耗"), 2)
        self.assertEqual(len(rubber["更换记录"]), 2)
        joint = self._joint(2)
        self.assertEqual(len([r for r in joint["缝宽记录"] if r["批次号"] == "BATCH-IDEM-001"]), 1)

        # 工作台同一缝只显示一条
        workbench = self.service.workbench()
        joint_occurrences = [item for item in workbench["items"] if item["缝id"] == 2]
        self.assertEqual(len(joint_occurrences), 1)

    def test_same_batch_no_after_evaluation_does_not_append_joints(self):
        batch = self._create([2], "BATCH-MERGE-001")
        self.service.advance_batch(batch["id"])  # 评估完成
        again, message = self.service.create_batch(
            {"批次号": "BATCH-MERGE-001", "缝ids": [2, 3]}
        )
        self.assertEqual(again["缝ids"], [2])
        self.assertIn("已存在", message)

    # ------------------------------------------------------ 旧占用迁移释放

    def test_old_occupation_migrates_and_releases(self):
        # 第一批对缝 2 预占 C 型橡胶条
        first = self._create([2], "BATCH-OLD-001")
        self._advance_to_stage(first["id"], "预占")
        rubber = self._material(1)
        self.assertEqual(rubber["预占数量"], 1)

        # 第二批再次包含缝 2（旧批次未验收），同材料预占应迁移而不是叠加
        second = self._create([2, 3], "BATCH-NEW-001")
        self.service.advance_batch(second["id"])  # 预占（开立时已完成评估）
        rubber = self._material(1)
        statuses = [d["状态"] for d in rubber["预占明细"]]
        self.assertIn("已迁移释放", statuses)
        new_details = [d for d in rubber["预占明细"] if d["批次号"] == "BATCH-NEW-001"]
        migrated = [d for d in new_details if d["迁移自"] == "BATCH-OLD-001"]
        self.assertEqual(len(migrated), 1)
        # 缝2迁移占1 + 缝3新增占1，总预占 2，迁移部分没有二次扣减
        self.assertEqual(rubber["预占数量"], 2)
        plan_for_joint2 = next(p for p in second["材料计划"]
                               if p["材料id"] == 1 and p["缝id"] == 2)
        self.assertEqual(plan_for_joint2["迁移自"], "BATCH-OLD-001")

    # ------------------------------------------------------------ 事务回滚

    def test_shortage_rolls_back_stage_and_reservation(self):
        # 锚固组件库存 8，先开一个大批次到预占，占掉大部分
        other = self._create([2, 3], "BATCH-OCCUPY-001")
        self._advance_to_stage(other["id"], "预占")
        anchor = self._material(2)
        self.assertEqual(anchor["预占数量"], 2)

        # 把库存改成不足以再容纳 1 件：新批次进入预占时必须整体回滚
        anchor["库存数量"] = 2
        failing = self._create([1], "BATCH-FAIL-001")
        with self.assertRaises(ServiceError):
            self.service.advance_batch(failing["id"])  # 评估
            self.service.advance_batch(failing["id"])  # 预占 -> 库存不足
        failing = self.service.get_batch(failing["id"])
        self.assertEqual(failing["stage"], "评估")
        anchor = self._material(2)
        self.assertEqual(anchor["预占数量"], 2)  # 未增加
        self.assertEqual(anchor["库存数量"], 2)  # 未扣减
        self.assertNotIn("BATCH-FAIL-001", [d["批次号"] for d in anchor["预占明细"]])

    # ------------------------------------------------------------ 并发互斥

    def test_concurrent_advance_only_one_batch_proceeds(self):
        batch = self._create([2, 3], "BATCH-CONC-001")
        barrier = threading.Barrier(2, timeout=5)
        results: list[object] = []

        def worker(stage_committed):
            try:
                batch_ref, advanced, message = self.service.advance_batch(
                    batch["id"], stage_committed=stage_committed
                )
                results.append(("ok", advanced, message))
            except BatchBusyError as exc:
                results.append(("busy", exc.status_code))
            except Exception as exc:  # pragma: no cover - 测试失败时暴露异常类型
                results.append(("error", repr(exc)))

        def hook(_batch, _stage):
            # 阶段已落库但外层事务未提交时，第二个并发提交到达推进闸
            barrier.wait()
            barrier.wait()

        t1 = threading.Thread(target=worker, args=(hook,))
        t2 = threading.Thread(target=worker, args=(None,))
        t1.start()
        barrier.wait()          # 等 t1 进入事务
        t2.start()
        barrier.wait()          # 放行 t1 提交
        t1.join()
        t2.join()

        self.assertEqual(len(results), 2)
        self.assertIn(("busy", 409), results)
        ok = [r for r in results if r[0] == "ok"]
        self.assertEqual(len(ok), 1)
        self.service.advance_batch(batch["id"])
        self.assertEqual(self.service.get_batch(batch["id"])["stage"], "施工")

    def test_other_batch_blocked_while_one_advancing(self):
        batch_a = self._create([2], "BATCH-A-001")
        batch_b = self._create([4], "BATCH-B-001")
        entered = threading.Event()
        release = threading.Event()

        def slow_advance():
            try:
                self.service.advance_batch(
                    batch_a["id"],
                    stage_committed=lambda *_: (entered.set(), release.wait(timeout=5)),
                )
            except Exception:  # pragma: no cover
                pass

        thread = threading.Thread(target=slow_advance)
        thread.start()
        self.assertTrue(entered.wait(timeout=5))
        with self.assertRaises(BatchBusyError):
            self.service.advance_batch(batch_b["id"])
        release.set()
        thread.join()
        # 释放后批次 B 可以正常推进
        _, advanced, _ = self.service.advance_batch(batch_b["id"])
        self.assertTrue(advanced)

    # ------------------------------------------------------------ 单缝更换

    def test_replace_single_runs_unified_pipeline_idempotently(self):
        entry, message = self.service.run_action(4, "更换伸缩缝")
        self.assertIsNotNone(entry, message)
        self.assertEqual(entry["status"], "已更换")
        self.assertEqual(entry["最近批次号"], "AUTO-J4")
        self.assertIn("成组更换完成", entry["更换结论"])
        f_type_material = self._material(4)
        self.assertEqual(f_type_material["库存数量"], 4)
        self.assertEqual(f_type_material["预占数量"], 0)
        self.assertEqual(f_type_material["最近批次号"], "AUTO-J4")

        # 再次执行更换动作：幂等，不重复扣材料、不重复写缝宽
        entry2, _ = self.service.run_action(4, "更换伸缩缝")
        self.assertEqual(entry2["更换结论"], entry["更换结论"])
        self.assertEqual(f_type_material["库存数量"], 4)
        self.assertEqual(len(f_type_material["更换记录"]), 1)

    def test_single_action_after_group_replacement_stays_idempotent(self):
        # 先成组更换，再走单缝动作，不允许再生成自动批次重复扣料
        group = self._create([2], "BATCH-GRP-002")
        self._advance_all(group["id"])
        rubber_before = self._material(1)["库存数量"]
        width_records_before = len(self._joint(2)["缝宽记录"])
        entry, _ = self.service.run_action(2, "更换伸缩缝")
        self.assertEqual(entry["最近批次号"], "BATCH-GRP-002")
        self.assertEqual(self._material(1)["库存数量"], rubber_before)
        self.assertEqual(len(self._joint(2)["缝宽记录"]), width_records_before)
        self.assertIsNone(self.service._find_batch_by_no("AUTO-J2"))

    # ------------------------------------------------------------ 列表/详情一致

    def test_list_and_detail_read_same_source(self):
        batch = self._create([2], "BATCH-VIEW-001")
        self._advance_all(batch["id"], widths={2: "21mm"})
        items, total = self.service.list_entries(keyword="EXPA-0002")
        self.assertEqual(total, 1)
        listed = items[0]
        detail = self.service.get_entry(2)
        self.assertIs(listed, detail)  # 列表与详情是同一条缝台账记录
        self.assertEqual(listed["更换结论"], detail["更换结论"])
        self.assertEqual(listed["当前缝宽"], "21mm")

    # ------------------------------------------------------------ 工具

    def _advance_to_stage(self, batch_id: int, stage: str):
        # 开立时已在评估阶段；从当前阶段逐次推进到目标阶段
        from app.services.expansion import STAGES
        batch = self.service.get_batch(batch_id)
        for _ in range(STAGES.index(stage) - STAGES.index(batch["stage"])):
            self.service.advance_batch(batch_id)


if __name__ == "__main__":
    unittest.main()
