"""独立生命曲线计算器（第二实现）。

刻意不复用 effects.kernel 的实例对象、hp 状态与事件流：这里维护一套自己的
标量模拟，只依据效果规格 + 命令表重算每拍生命值，再从主引擎的规则事件流
做交叉核对。它不读主引擎 hp，也不把任何结果写回角色状态。

因此“期望生命曲线”来自一次独立的小规模计算，而不是把运行结果直接塞进
角色状态。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from effects.kernel import MAX_REACTION_DEPTH, SPECS, StackPolicy

SUPPRESS_PAIRS = {("burn", "freeze"), ("freeze", "burn")}
TRANSFORM_MAP = {"curse": "hex", "hex": "curse"}


class ExpectedReject(Exception):
    """计算器与内核同构地拒绝非法反应链。"""


@dataclass
class SimInstance:
    iid: int
    name: str
    target: str
    source: str
    start: int
    duration: int
    period: Optional[int]
    amount: int
    policy: StackPolicy
    suppressed_by: Optional[int] = None
    elapsed: int = 0
    ticks_done: int = 0

    @property
    def active(self) -> bool:
        return self.suppressed_by is None


class CurveCalculator:
    def __init__(self, entities: dict[str, int]) -> None:
        self.hp = dict(entities)
        self.hp_max = dict(entities)
        self.time = 0
        self.paused = False
        self.instances: dict[int, SimInstance] = {}
        self._next_iid = 1
        # 每拍结束后的生命快照；下标即绝对战斗时间，0 位是初始值。
        self.hp_curve: dict[str, list[int]] = {e: [v] for e, v in self.hp.items()}

    # -- 申请：与内核同语义的独立实现 -----------------------------------
    def apply(self, name: str, target: str, source: str,
              duration: Optional[int] = None, amount: Optional[int] = None,
              potency: Optional[int] = None,
              overrides: Optional[dict[str, str]] = None) -> None:
        if name not in SPECS:
            raise KeyError(f"未知效果: {name}")
        if target not in self.hp:
            raise KeyError(f"未知角色: {target}")
        spec = SPECS[name]
        mapping = dict(TRANSFORM_MAP)
        if overrides:
            mapping.update(overrides)
        depth = 0

        while True:
            depth += 1
            if depth > MAX_REACTION_DEPTH:
                raise ExpectedReject(f"反应链超过 {MAX_REACTION_DEPTH} 层")

            existing = self._find(target)
            transformed = False
            for old in existing:
                if old.active and mapping.get(old.name) == spec.name:
                    old.name = spec.name
                    old.period = spec.period
                    old.amount = spec.amount if amount is None else amount
                    old.duration = duration if duration is not None else spec.duration
                    old.elapsed = 0
                    old.ticks_done = 0
                    old.policy = spec.policy
                    old.source = source
                    old.start = self.time
                    transformed = True
                    break
            if transformed:
                continue

            for old in existing:
                if old.active and (old.name, spec.name) in SUPPRESS_PAIRS:
                    self._create(spec, target, source, duration, amount, potency)
                    old.suppressed_by = self._next_iid - 1
                    return
            break

        same = self._find(target, name)
        if spec.policy is StackPolicy.STACK:
            same_src = [i for i in same if i.source == source and i.active]
            if same_src:
                inst = same_src[0]
                inst.amount += spec.amount if potency is None else potency
                inst.duration = (duration if duration is not None
                                 else max(inst.duration, spec.duration))
                return
        elif spec.policy is StackPolicy.REFRESH:
            active = [i for i in same if i.active]
            if active:
                inst = active[0]
                inst.duration = (duration if duration is not None
                                 else spec.duration)
                inst.elapsed = 0
                inst.ticks_done = 0
                inst.amount = spec.amount if amount is None else amount
                return
        elif spec.policy is StackPolicy.REPLACE:
            for inst in same:
                self._remove(inst.iid)
        self._create(spec, target, source, duration, amount, potency)

    def _create(self, spec, target, source, duration, amount,
                potency) -> SimInstance:
        iid = self._next_iid
        self._next_iid += 1
        inst = SimInstance(
            iid=iid, name=spec.name, target=target, source=source,
            start=self.time,
            duration=duration if duration is not None else spec.duration,
            period=spec.period,
            amount=((spec.amount if amount is None else amount)
                    if potency is None else potency),
            policy=spec.policy,
        )
        self.instances[iid] = inst
        return inst

    def _find(self, target: str,
              name: Optional[str] = None) -> list[SimInstance]:
        out = [i for i in self.instances.values() if i.target == target
               and (name is None or i.name == name)]
        return sorted(out, key=lambda x: x.iid)

    def _remove(self, iid: int) -> None:
        inst = self.instances.pop(iid, None)
        if inst is None:
            return
        for other in self.instances.values():
            if other.suppressed_by == iid:
                other.suppressed_by = None

    def destroy_source(self, source: str) -> list[int]:
        victims = [i.iid for i in self.instances.values()
                   if i.source == source
                   and i.policy is not StackPolicy.REFRESH]
        for iid in victims:
            self._remove(iid)
        return victims

    def dispel(self, iid: int) -> bool:
        if iid not in self.instances:
            return False
        self._remove(iid)
        return True

    def dispel_all(self, target: str,
                   names: Optional[set[str]] = None) -> list[int]:
        victims = [i.iid for i in self._find(target)
                   if names is None or i.name in names]
        for iid in victims:
            self._remove(iid)
        return victims

    def register(self, eid: str, hp: int) -> None:
        self.hp[eid] = hp
        self.hp_max[eid] = hp
        self.hp_curve.setdefault(eid, [0] * (self.time + 1))
        self.hp_curve[eid][-1] = hp

    # -- 逐拍推进：逐实例按 iid 顺序结算，与内核一致 --------------------
    def advance(self, steps: int = 1) -> None:
        if steps < 0:
            raise ValueError("steps 不能为负")
        if self.paused:
            raise ExpectedReject("暂停期间不能推进")
        for _ in range(steps):
            self.time += 1
            for iid in sorted(self.instances):
                inst = self.instances.get(iid)
                if inst is None or not inst.active:
                    continue
                inst.elapsed += 1
                if inst.period is None:
                    continue
                due = (inst.ticks_done + 1) * inst.period
                if due <= inst.elapsed and due < inst.duration:
                    inst.ticks_done = due // inst.period
                    spec = SPECS[inst.name]
                    if spec.kind == "dot":
                        self._apply_hp(inst.target, -inst.amount)
                    elif spec.kind == "hot":
                        self._apply_hp(inst.target, inst.amount)
            for iid in sorted(self.instances):
                inst = self.instances.get(iid)
                if inst and inst.active and inst.elapsed >= inst.duration:
                    self._remove(iid)
            for entity in self.hp:
                self.hp_curve.setdefault(entity, [self.hp[entity] * self.time])
                self.hp_curve[entity].append(self.hp[entity])

    def _apply_hp(self, target: str, delta: int) -> None:
        before = self.hp[target]
        after = max(0, min(self.hp_max.get(target, before), before + delta))
        self.hp[target] = after

    def pause(self) -> None:
        self.paused = True

    def resume(self) -> None:
        self.paused = False

    def curve(self, target: str) -> list[int]:
        return list(self.hp_curve[target])


def feed(calc: CurveCalculator, cmd: dict,
         overrides: dict[str, str]) -> None:
    """把一条素材命令喂给独立计算器；相机命令对生命无影响，直接跳过。"""
    op = cmd["op"]
    if op == "camera":
        return
    if op == "apply":
        kw = {k: cmd[k] for k in ("duration", "amount", "potency") if k in cmd}
        try:
            calc.apply(cmd["name"], cmd["target"], cmd["source"],
                       overrides=overrides, **kw)
        except ExpectedReject:
            if cmd.get("expect") != "reject_reaction_chain":
                raise
    elif op == "advance":
        calc.advance(cmd.get("steps", 1))
    elif op == "pause":
        calc.pause()
    elif op == "resume":
        calc.resume()
    elif op == "destroy_source":
        calc.destroy_source(cmd["source"])
    elif op == "dispel":
        calc.dispel(cmd["iid"])
    elif op == "dispel_all":
        calc.dispel_all(cmd["target"],
                        set(cmd["names"]) if cmd.get("names") else None)
    elif op == "register":
        calc.register(cmd["entity"], cmd["hp"])
    elif op == "define_transform":
        overrides[cmd["from_name"]] = cmd["to_name"]
    elif op == "clear_transform":
        overrides.pop(cmd["from_name"], None)
    else:
        raise ValueError(f"曲线计算器不支持: {op}")


def expected_curve(entities: dict[str, int],
                   commands: list[dict]) -> CurveCalculator:
    """执行素材命令表，返回携带逐拍期望曲线的独立计算器。"""
    calc = CurveCalculator(entities)
    overrides: dict[str, str] = {}
    for cmd in commands:
        feed(calc, cmd, overrides)
    return calc
