"""战斗状态效果系统内核。

设计约束（全部行为对重放确定，不使用任何随机计时器）：

- 战斗时间为整数时钟，周期触发按战斗时间累计：第 period、2*period ... 跳一次，
  严格小于实例存活期，到达到期点的同一时刻先执行到期、不允许补出额外一跳。
- 每个效果都是可追溯实例（iid 全局唯一），解除操作针对实例 id，不按名字整体删除。
- 三种叠层政策对“来源退出（source 被销毁/重建）”各自有明确语义：
    STACK   每个来源是一个独立实例，来源退出只移除该来源拥有的实例；
    REFRESH 来源退出后实例保留并自然到期（新来源是一次全新的申请）；
    REPLACE 后到的申请整体替换旧实例；来源退出时实例立即移除。
- 暂停期间时钟不前进，恢复后不补发暂停区间的跳伤。
- 效果互相抑制/转换沿有向关系边执行，链深超过上限即终止并返回终止关系链。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class EnginePausedError(RuntimeError):
    """引擎处于暂停状态时推进战斗时间。"""


class StackPolicy(str, Enum):
    STACK = "stack"
    REFRESH = "refresh"
    REPLACE = "replace"


@dataclass(frozen=True)
class EffectSpec:
    name: str
    kind: str                       # dot / hot / buff / control
    period: Optional[int]           # 跳伤/跳疗周期，None 表示无周期
    amount: int                     # 每跳数值（dot 扣血、hot 回血）
    duration: int                   # 默认持续期
    policy: StackPolicy


# 内置效果目录。burn/freeze 互为控制关系（见引擎中的关系表）。
SPECS: dict[str, EffectSpec] = {
    "poison": EffectSpec("poison", "dot", period=3, amount=4, duration=10,
                         policy=StackPolicy.STACK),
    "burn":   EffectSpec("burn", "dot", period=2, amount=3, duration=8,
                         policy=StackPolicy.STACK),
    "bleed":  EffectSpec("bleed", "dot", period=4, amount=5, duration=12,
                         policy=StackPolicy.STACK),
    "regen":  EffectSpec("regen", "hot", period=3, amount=6, duration=6,
                         policy=StackPolicy.REFRESH),
    "shield": EffectSpec("shield", "buff", period=None, amount=0, duration=6,
                         policy=StackPolicy.REFRESH),
    "rage":   EffectSpec("rage", "buff", period=5, amount=2, duration=10,
                         policy=StackPolicy.REPLACE),
    "freeze": EffectSpec("freeze", "control", period=None, amount=0, duration=5,
                         policy=StackPolicy.REPLACE),
    "curse":  EffectSpec("curse", "dot", period=4, amount=3, duration=10,
                         policy=StackPolicy.STACK),
    "hex":    EffectSpec("hex", "dot", period=2, amount=2, duration=8,
                         policy=StackPolicy.STACK),
}

# 申请新效果时对目标上既有实例产生的有向关系：
#   suppress: 旧实例挂起（计时与周期冻结），关系结束后恢复，剩余持续期不损失。
#   transform: 旧实例转换为新效果（保留同一 iid，事件链可追溯）。
# 两类关系使用不同的效果对，避免同一对关系语义冲突。
SUPPRESS_PAIRS: set[tuple[str, str]] = {("burn", "freeze"), ("freeze", "burn")}
TRANSFORM_MAP: dict[str, str] = {
    # curse 与 hex 可互相转换，实例 id 贯穿始终。
    "curse": "hex",
    "hex": "curse",
}
MAX_REACTION_DEPTH = 8


@dataclass
class EffectInstance:
    iid: int
    name: str
    target: str
    source: str
    start_time: int
    duration: int
    period: Optional[int]
    amount: int
    policy: StackPolicy
    suppressed_by: Optional[int] = None     # 挂起来源实例 iid
    # 已累计的有效战斗时间（挂起期间不增长）。
    elapsed: int = 0
    # 已经结算的周期数；恢复后从下一个周期继续，不补发。
    ticks_done: int = 0
    history: list[tuple[str, int]] = field(default_factory=list)

    @property
    def active(self) -> bool:
        return self.suppressed_by is None

    @property
    def remaining(self) -> int:
        return max(0, self.duration - self.elapsed)

    def next_tick_at(self) -> Optional[int]:
        """从当前进度看，下一跳在多少有效时间之后；无下一跳返回 None。"""
        if self.period is None:
            return None
        nxt = (self.ticks_done + 1) * self.period
        if nxt >= self.duration:
            return None
        return nxt - self.elapsed


class Engine:
    def __init__(self, hp: Optional[dict[str, int]] = None) -> None:
        self.time = 0
        self.paused = False
        self.hp: dict[str, int] = dict(hp or {})
        self.hp_max: dict[str, int] = dict(hp or {})
        self.instances: dict[int, EffectInstance] = {}
        self._next_iid = 1
        self._next_tid = 1
        self.events: list[dict] = []
        # 源实体 -> 它拥有的实例 iid 集合。
        self._owned: dict[str, set[int]] = {}
        # 白盒/反例素材可显式声明的转换覆盖；生产场景不声明时为空，
        # 行为与 TRANSFORM_MAP 完全一致。覆盖随引擎实例保存，重放可复原。
        self.transform_overrides: dict[str, str] = {}
        # 最近一次被反应深度上限终止的关系链。
        self.last_terminated_chain: Optional[list[str]] = None

    # ------------------------------------------------------------------ #
    # 事件与快照
    # ------------------------------------------------------------------ #
    def _emit(self, etype: str, **payload) -> dict:
        ev = {"tid": self._next_tid, "time": self.time, "type": etype}
        ev.update(payload)
        self._next_tid += 1
        self.events.append(ev)
        return ev

    def snapshot(self) -> dict:
        return {
            "time": self.time,
            "paused": self.paused,
            "hp": dict(self.hp),
            "instances": [
                {
                    "iid": i.iid,
                    "name": i.name,
                    "target": i.target,
                    "source": i.source,
                    "start_time": i.start_time,
                    "duration": i.duration,
                    "elapsed": i.elapsed,
                    "remaining": i.remaining,
                    "ticks_done": i.ticks_done,
                    "suppressed_by": i.suppressed_by,
                    "policy": i.policy.value,
                    "history": [list(h) for h in i.history],
                }
                for i in sorted(self.instances.values(), key=lambda x: x.iid)
            ],
        }

    def find(self, target: str, name: Optional[str] = None) -> list[EffectInstance]:
        out = [i for i in self.instances.values() if i.target == target
               and (name is None or i.name == name)]
        return sorted(out, key=lambda x: x.iid)

    # ------------------------------------------------------------------ #
    # 生命与源实体
    # ------------------------------------------------------------------ #
    def register_entity(self, eid: str, hp: int) -> None:
        self.hp[eid] = hp
        self.hp_max[eid] = hp
        self._emit("entity_register", entity=eid, hp=hp)

    def _apply_hp(self, target: str, delta: int, reason: dict) -> None:
        if target not in self.hp:
            raise KeyError(f"未知角色: {target}")
        before = self.hp[target]
        after = max(0, min(self.hp_max.get(target, before), before + delta))
        self.hp[target] = after
        if after == before:
            # 满血回血或零血扣伤不产生生命变化事件，避免伪造“跳字”。
            return
        self._emit("hp", target=target, before=before, after=after,
                   delta=after - before, reason=reason)

    def destroy_source(self, source: str) -> list[int]:
        """来源退出。返回被移除的实例 id 列表（REFRESH 类实例保留）。"""
        removed: list[int] = []
        for iid in sorted(self._owned.get(source, set())):
            inst = self.instances.get(iid)
            if inst is None:
                continue
            if inst.policy is StackPolicy.REFRESH:
                # 刷新政策：来源退出不影响已申请到的持续期，实例自然到期。
                inst.history.append(("source_exited_kept", self.time))
                self._emit("source_exit_kept", iid=iid, source=source,
                           name=inst.name, target=inst.target)
                continue
            removed.append(iid)
        for iid in removed:
            self._remove_instance(iid, cause="source_exit", source=source)
        self._owned.pop(source, None)
        self._emit("source_destroy", source=source, removed=removed)
        return removed

    # ------------------------------------------------------------------ #
    # 暂停
    # ------------------------------------------------------------------ #
    def pause(self) -> None:
        if not self.paused:
            self.paused = True
            self._emit("pause")

    def resume(self) -> None:
        if self.paused:
            self.paused = False
            self._emit("resume")

    # ------------------------------------------------------------------ #
    # 申请效果（含三种叠层政策与相互关系反应）
    # ------------------------------------------------------------------ #
    def _transform_target(self, name: str) -> Optional[str]:
        """实例级转换覆盖优先；未声明时回落到内置 TRANSFORM_MAP。"""
        if name in self.transform_overrides:
            return self.transform_overrides[name]
        return TRANSFORM_MAP.get(name)

    def apply_effect(self, name: str, target: str, source: str,
                     duration: Optional[int] = None,
                     amount: Optional[int] = None,
                     potency: Optional[int] = None) -> EffectInstance:
        if name not in SPECS:
            raise KeyError(f"未知效果: {name}")
        if target not in self.hp:
            raise KeyError(f"未知角色: {target}")
        spec = SPECS[name]
        chain: list[str] = []
        prebuilt: Optional[EffectInstance] = None
        depth = 0

        # 阶段一：沿有向关系边迭代结算转换/抑制。每经过一条边深度 +1，
        # 超过上限即终止并保留导致终止的效果关系链。
        while True:
            depth += 1
            if depth > MAX_REACTION_DEPTH:
                self.last_terminated_chain = list(chain)
                self._emit("reaction_chain_terminated", chain=list(chain),
                           limit=MAX_REACTION_DEPTH)
                raise RecursionError(
                    f"效果反应链超过 {MAX_REACTION_DEPTH} 层被终止: "
                    f"{' -> '.join(chain)}")

            existing = self.find(target)
            transformed = False
            for old in existing:
                if old.active and self._transform_target(old.name) == spec.name:
                    chain.append(f"transform:{old.name}->{spec.name}")
                    self._emit("transform", iid=old.iid, target=target,
                               from_name=old.name, to_name=spec.name,
                               source=source, chain=list(chain))
                    old.name = spec.name
                    old.period = spec.period
                    old.amount = spec.amount if amount is None else amount
                    old.duration = duration if duration is not None else spec.duration
                    old.elapsed = 0
                    old.ticks_done = 0
                    old.policy = spec.policy
                    old.source = source
                    old.start_time = self.time
                    old.history.append(("transform", self.time))
                    self._retag_ownership(old, source)
                    prebuilt = old
                    transformed = True
                    break
            if transformed:
                continue

            for old in existing:
                if old.active and (old.name, spec.name) in SUPPRESS_PAIRS:
                    chain.append(f"suppress:{old.name}<-{spec.name}")
                    inst = self._create_instance(spec, target, source,
                                                 duration, amount, potency)
                    old.suppressed_by = inst.iid
                    old.history.append(("suppressed", self.time))
                    self._emit("suppress", iid=old.iid, by=inst.iid,
                               target=target, chain=list(chain))
                    self._emit("apply", iid=inst.iid, name=inst.name,
                              target=target, source=source,
                              policy=spec.policy.value,
                              duration=inst.duration, chain=list(chain))
                    return inst
            break

        return self._policy_settle(prebuilt, spec, target, source, duration,
                                   amount, potency, chain)

    def _policy_settle(self, prebuilt: Optional[EffectInstance],
                       spec: EffectSpec, target: str, source: str,
                       duration: Optional[int], amount: Optional[int],
                       potency: Optional[int],
                       chain: list[str]) -> EffectInstance:
        # 转换而来的实例已经按新效果规格就地改写，直接落账，不再二次叠加。
        if prebuilt is not None:
            self._emit("apply", iid=prebuilt.iid, name=prebuilt.name,
                      target=target, source=source, policy=spec.policy.value,
                      duration=prebuilt.duration, chain=list(chain))
            return prebuilt

        same_name = [i for i in self.find(target, spec.name)]

        if spec.policy is StackPolicy.STACK:
            # 每个来源持有自己的叠层实例；同源再申请则在该实例上叠加一层强度。
            same_source = [i for i in same_name if i.source == source and i.active]
            if same_source:
                inst = same_source[0]
                add = spec.amount if potency is None else potency
                inst.amount += add
                inst.duration = (duration if duration is not None
                                 else max(inst.duration, spec.duration))
                inst.history.append(("restack", self.time))
                self._emit("restack", iid=inst.iid, name=inst.name,
                           target=target, source=source,
                           potency=inst.amount, chain=list(chain))
                return inst
            inst = self._create_instance(spec, target, source,
                                                   duration, amount, potency)
            self._emit("apply", iid=inst.iid, name=inst.name, target=target,
                      source=source, policy=spec.policy.value,
                      duration=inst.duration, chain=list(chain))
            return inst

        if spec.policy is StackPolicy.REFRESH:
            active = [i for i in same_name if i.active]
            if active:
                inst = active[0]
                new_duration = duration if duration is not None else spec.duration
                # 刷新持续期：剩余时间回满为一个全新持续期，周期计数归零。
                before_remaining = inst.remaining
                inst.duration = new_duration
                inst.elapsed = 0
                inst.ticks_done = 0
                inst.amount = spec.amount if amount is None else amount
                inst.history.append(("refresh", self.time))
                self._emit("refresh", iid=inst.iid, name=inst.name,
                           target=target, source=source,
                           remaining_before=before_remaining,
                           remaining_after=inst.remaining,
                           chain=list(chain))
                return inst
            inst = self._create_instance(spec, target, source,
                                                   duration, amount, potency)
            self._emit("apply", iid=inst.iid, name=inst.name, target=target,
                      source=source, policy=spec.policy.value,
                      duration=inst.duration, chain=list(chain))
            return inst

        # REPLACE：替换强度——同名实例整体被新实例替换。
        for old in same_name:
            self._remove_instance(old.iid, cause="replaced")
        inst = self._create_instance(spec, target, source,
                                             duration, amount, potency)
        self._emit("apply", iid=inst.iid, name=inst.name, target=target,
                  source=source, policy=spec.policy.value,
                  duration=inst.duration, chain=list(chain))
        return inst

    def _create_instance(self, spec: EffectSpec, target: str, source: str,
                         duration: Optional[int], amount: Optional[int],
                         potency: Optional[int]) -> EffectInstance:
        iid = self._next_iid
        self._next_iid += 1
        inst = EffectInstance(
            iid=iid,
            name=spec.name,
            target=target,
            source=source,
            start_time=self.time,
            duration=duration if duration is not None else spec.duration,
            period=spec.period,
            amount=(spec.amount if amount is None else amount)
                   if potency is None else potency,
            policy=spec.policy,
        )
        self.instances[iid] = inst
        self._owned.setdefault(source, set()).add(iid)
        inst.history.append(("created", self.time))
        return inst

    def _retag_ownership(self, inst: EffectInstance, source: str) -> None:
        for owner, ids in self._owned.items():
            ids.discard(inst.iid)
        self._owned.setdefault(source, set()).add(inst.iid)
        inst.source = source

    # ------------------------------------------------------------------ #
    # 时间推进
    # ------------------------------------------------------------------ #
    def advance(self, steps: int = 1) -> list[dict]:
        """逐整数步推进战斗时间，返回推进期间产生的事件。"""
        if steps < 0:
            raise ValueError("steps 不能为负")
        if self.paused:
            raise EnginePausedError("引擎已暂停，不能推进战斗时间")
        produced: list[dict] = []
        for _ in range(steps):
            mark = len(self.events)
            self.time += 1
            self._tick()
            produced.extend(self.events[mark:])
        return produced

    def _tick(self) -> None:
        # 第一遍：只处理周期跳伤。挂起实例不累计时间，因此不会跳。
        for inst in sorted(self.instances.values(), key=lambda x: x.iid):
            if not inst.active:
                continue
            inst.elapsed += 1
            if inst.period is not None:
                due = (inst.ticks_done + 1) * inst.period
                # 严格小于持续期：到期点上的一跳不结算（先到期）。
                if due <= inst.elapsed and due < inst.duration:
                    self._resolve_tick(inst, due)
        # 第二遍：到期移除（与跳伤同一时刻时，到期优先，上一跳已被严格条件排除）。
        for iid in sorted(self.instances, key=lambda x: x):
            inst = self.instances.get(iid)
            if inst and inst.active and inst.elapsed >= inst.duration:
                self._remove_instance(iid, cause="expire")

    def _resolve_tick(self, inst: EffectInstance, due_at_elapsed: int) -> None:
        n = due_at_elapsed // inst.period  # type: ignore[operator]
        inst.ticks_done = n
        spec = SPECS[inst.name]
        if spec.kind == "dot":
            delta = -inst.amount
        elif spec.kind == "hot":
            delta = inst.amount
        else:
            # buff 的周期效果（如 rage）：作为增益跳，记录但不改生命。
            self._emit("tick", iid=inst.iid, name=inst.name, target=inst.target,
                      tick_no=n, kind=spec.kind, amount=inst.amount)
            inst.history.append(("tick", self.time))
            return
        before = self.hp[inst.target]
        self._apply_hp(inst.target, delta,
                       reason={"event": "tick", "iid": inst.iid,
                               "name": inst.name, "tick_no": n})
        self._emit("tick", iid=inst.iid, name=inst.name, target=inst.target,
                  tick_no=n, kind=spec.kind, amount=inst.amount,
                  hp_before=before, hp_after=self.hp[inst.target])
        inst.history.append(("tick", self.time))

    # ------------------------------------------------------------------ #
    # 解除（全部作用于可追溯实例）
    # ------------------------------------------------------------------ #
    def _remove_instance(self, iid: int, cause: str,
                         source: Optional[str] = None) -> Optional[EffectInstance]:
        inst = self.instances.pop(iid, None)
        if inst is None:
            return None
        # 它若挂起着别人，恢复那些实例（剩余持续期原样保留）。
        for other in self.instances.values():
            if other.suppressed_by == iid:
                other.suppressed_by = None
                other.history.append(("resumed", self.time))
                self._emit("resume", iid=other.iid, from_iid=iid,
                           target=other.target, remaining=other.remaining)
        for ids in self._owned.values():
            ids.discard(iid)
        inst.history.append((f"removed:{cause}", self.time))
        self._emit("remove", iid=iid, name=inst.name, target=inst.target,
                  source=inst.source, cause=cause, by_source=source,
                  remaining=inst.remaining, ticks_done=inst.ticks_done)
        return inst

    def dispel_instance(self, iid: int) -> bool:
        """按实例 id 解除单个效果。"""
        if iid not in self.instances:
            return False
        self._remove_instance(iid, cause="dispel")
        return True

    def dispel_all(self, target: str, names: Optional[set[str]] = None) -> list[int]:
        """全体解除：逐个实例处理并返回被解除的 iid 清单，不按名字整块删除。"""
        victims = [i for i in self.find(target)
                   if names is None or i.name in names]
        out: list[int] = []
        for inst in victims:
            if self.dispel_instance(inst.iid):
                out.append(inst.iid)
        self._emit("dispel_all", target=target, names=sorted(names) if names else None,
                   removed=out)
        return out

    # ------------------------------------------------------------------ #
    # 查询辅助
    # ------------------------------------------------------------------ #
    def ticks_of(self, iid: int) -> list[dict]:
        return [e for e in self.events
                if e["type"] == "tick" and e["iid"] == iid]

    def hp_deltas(self, target: str) -> list[dict]:
        return [e for e in self.events if e["type"] == "hp" and e["target"] == target]
