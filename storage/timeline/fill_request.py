"""
FillRequest — 孤岛边界扩展请求。

FillRequest 的语义是：请求将 author 的某个孤岛边界 anchor_id，
向方向 direction 扩展 count 个含图帖子。
"""
from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from storage.timeline_db import TimelineIslandsDB


@dataclass
class FillRequest:
    author: str
    anchor_id: int       # 孤岛的边界 ID (min_id 或 max_id)
    direction: int       # +1 (扩展 max_id, 往更新扫) 或 -1 (扩展 min_id, 往更旧扫)
    count: int           # 缺口：在此边界之外还需要多少个含图帖子
    score: float         # 任务优先级（越高越先处理）

    event: threading.Event = field(default_factory=threading.Event, repr=False)

    def is_satisfied(self, db: "TimelineIslandsDB") -> bool:
        """检查对应的孤岛边界是否已向方向 D 扩展了 count 个含图帖子。"""
        island = db.find_island(self.author, self.anchor_id)
        if island is None:
            return False

        if self.direction > 0:
            actual = db.count_has_image_in_range(
                self.author, self.anchor_id + 1, island.max_id
            )
            return actual >= self.count or not island.should_explore_newer()
        else:
            actual = db.count_has_image_in_range(
                self.author, island.min_id, self.anchor_id - 1
            )
            return actual >= self.count or island.oldest_boundary
