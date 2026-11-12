"""效果图标视图。

视图是内核的只读投影：图标层数、剩余持续期、挂起状态全部当场从 Engine
的实例集合计算，视图自身不保存任何可变战斗状态，因此重放同一输入必然
得到相同图标序列。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from .kernel import EffectInstance, Engine, StackPolicy


@dataclass(frozen=True)
class Icon:
    name: str
    iids: tuple[int, ...]
    stacks: int
    remaining: tuple[int, ...]       # 与 iids 一一对应的剩余持续期
    suppressed: tuple[bool, ...]
    policy: str

    @property
    def primary_iid(self) -> int:
        return self.iids[0]


class IconView:
    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def icons(self, target: str) -> list[Icon]:
        grouped: dict[str, list[EffectInstance]] = {}
        for inst in self._engine.find(target):
            grouped.setdefault(inst.name, []).append(inst)
        icons: list[Icon] = []
        for name in sorted(grouped):
            group = sorted(grouped[name], key=lambda i: i.iid)
            icons.append(Icon(
                name=name,
                iids=tuple(i.iid for i in group),
                stacks=len(group),
                remaining=tuple(i.remaining for i in group),
                suppressed=tuple(not i.active for i in group),
                policy=group[0].policy.value,
            ))
        return icons

    def hp(self, target: str) -> int:
        return self._engine.hp[target]

    def render(self, target: str) -> str:
        """单行文本渲染，供演示与断言使用。"""
        parts = []
        for icon in self.icons(target):
            tag = f"{icon.name}x{icon.stacks}"
            rem = ",".join(str(r) for r in icon.remaining)
            sup = ",".join("z" if s else "a" for s in icon.suppressed)
            parts.append(f"{tag}[iid {','.join(map(str, icon.iids))}|rem {rem}|{sup}]")
        hp = self.hp(target)
        return f"t={self._engine.time} {target} hp={hp} :: " + (" | ".join(parts) if parts else "(no effects)")
