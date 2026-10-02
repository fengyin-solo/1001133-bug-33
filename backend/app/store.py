"""内存数据仓库：给每个业务模块准备一份可筛选、可流转的示例数据。

真实项目里这里会换成数据库访问层；当前实现只依赖标准库，保证克隆下来就能起。

事务约定：跨模块联动（伸缩缝成组更换要同时落缝台账、施工批次和材料预占）
统一走 ``store.transaction()``：进入时做整表快照，业务体里直接改内存对象，
抛异常即按快照回滚，正常退出才提交。调用方不要再自行 try/except 吞异常，
否则快照会被当成已提交数据。
"""
from __future__ import annotations

import copy
import threading
from contextlib import contextmanager
from typing import Any, Iterator

from app.seed import SEED_ROWS


class Store:
    def __init__(self) -> None:
        self._tables: dict[str, list[dict[str, Any]]] = {}
        self._write_lock = threading.RLock()
        self._txn_snapshot: dict[str, list[dict[str, Any]]] | None = None
        self.reset()

    def reset(self) -> None:
        """恢复到种子数据；仅供测试与进程启动使用。

        必须深拷贝：种子行里带嵌套结构（缝宽记录、预占明细、更换记录），
        浅拷贝会让这些列表/字典与模块级 SEED_ROWS 共享，业务侧一改写，
        下次 reset 出来的就是被污染的数据。
        """
        with self._write_lock:
            self._tables = copy.deepcopy(
                {name: [dict(row) for row in rows] for name, rows in SEED_ROWS.items()}
            )
            self._txn_snapshot = None

    def module_names(self) -> list[str]:
        return sorted(self._tables)

    def rows(self, module: str) -> list[dict[str, Any]]:
        return self._tables.setdefault(module, [])

    def find(self, module: str, entry_id: int) -> dict[str, Any] | None:
        for row in self.rows(module):
            if int(row.get("id", 0)) == entry_id:
                return row
        return None

    def next_id(self, module: str) -> int:
        return max((int(row.get("id", 0)) for row in self.rows(module)), default=0) + 1

    @contextmanager
    def transaction(self) -> Iterator[None]:
        """跨表写操作的原子边界：异常整体回滚，正常退出整体提交。

        采用整表深拷贝快照而非逐行 diff，是为了让业务代码可以放心地
        直接改 dict / list；嵌套事务复用同一把可重入锁，只有最外层
        负责快照与回滚。
        """
        with self._write_lock:
            if getattr(self, "_txn_snapshot", None) is not None:
                # 嵌套事务：最外层已经持有快照，内层直接透传异常即可
                yield
                return
            self._txn_snapshot = copy.deepcopy(self._tables)
            try:
                yield
            except BaseException:
                self._tables = self._txn_snapshot
                raise
            finally:
                self._txn_snapshot = None

    def overview(self) -> dict[str, object]:
        modules: list[dict[str, object]] = []
        for name in self.module_names():
            rows = self.rows(name)
            modules.append({
                "name": name,
                "created": len(rows),
                "pending": sum(1 for row in rows if row.get("pending")),
                "abnormal": sum(1 for row in rows if row.get("abnormal")),
            })
        cards = [
            {"label": "业务模块", "value": len(modules)},
            {"label": "今日新增", "value": sum(int(item["created"]) for item in modules)},
            {"label": "待处理", "value": sum(int(item["pending"]) for item in modules)},
            {"label": "异常量", "value": sum(int(item["abnormal"]) for item in modules)},
        ]
        return {"cards": cards, "modules": modules}


store = Store()
