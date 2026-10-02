"""内存数据仓库：给每个业务模块准备一份可筛选、可流转的示例数据。

真实项目里这里会换成数据库访问层；当前实现只依赖标准库，保证克隆下来就能起。

除了基础的读写，这里还提供两类成组更换要用的能力：

- ``transaction()``：阶段推进与物资预占/释放放在同一事务里，任一步失败整体回滚，
  不会再出现「批次阶段变了、材料数量没动」这类半成品状态；
- ``batch_advance(批次号)``：同一施工批次的并发推进串行化，只放一个请求进去，
  配合业务层的重读幂等判断，保证并发提交只推进一次。
"""
from __future__ import annotations

import copy
import threading
from contextlib import contextmanager
from typing import Any, Iterator

from app.seed import SEED_ROWS


class Store:
    def __init__(self) -> None:
        # 写操作统一走这把全局可重入锁，保证事务快照/提交期间不会被其它线程穿插修改。
        self._lock = threading.RLock()
        self._tables: dict[str, list[dict[str, Any]]] = self._load_seed()
        # 每个施工批次一把独立的推进锁：同批次互斥、不同批次不互相阻塞。
        self._batch_locks: dict[str, threading.Lock] = {}

    @staticmethod
    def _load_seed() -> dict[str, list[dict[str, Any]]]:
        return {name: [dict(row) for row in rows] for name, rows in SEED_ROWS.items()}

    def reset(self) -> None:
        """恢复到种子数据（主要供测试与本地重新初始化使用）。"""
        with self._lock:
            self._tables = self._load_seed()
            self._batch_locks = {}

    def module_names(self) -> list[str]:
        with self._lock:
            return sorted(self._tables)

    def rows(self, module: str) -> list[dict[str, Any]]:
        # setdefault 会在事务回滚时随快照一起被替换，调用方无需关心表是否存在。
        return self._tables.setdefault(module, [])

    def find(self, module: str, entry_id: int) -> dict[str, Any] | None:
        for row in self.rows(module):
            if int(row.get("id", 0)) == entry_id:
                return row
        return None

    @contextmanager
    def transaction(self) -> Iterator[None]:
        """阶段 + 物资的原子提交：成功整体生效，抛异常整体回滚到进入前快照。"""
        with self._lock:
            snapshot = copy.deepcopy(self._tables)
            try:
                yield
            except BaseException:
                # 连业务异常一起回滚，确保失败的推进不会留下半套阶段/占用数据。
                self._tables = snapshot
                raise

    def _batch_lock(self, batch_no: str) -> threading.Lock:
        with self._lock:
            lock = self._batch_locks.get(batch_no)
            if lock is None:
                lock = threading.Lock()
                self._batch_locks[batch_no] = lock
            return lock

    @contextmanager
    def batch_advance(self, batch_no: str) -> Iterator[None]:
        """同一施工批次的推进串行化；拿锁后由业务层重读状态做幂等判断。"""
        lock = self._batch_lock(batch_no)
        lock.acquire()
        try:
            yield
        finally:
            lock.release()

    def overview(self) -> dict[str, object]:
        with self._lock:
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
