"""战斗状态效果系统演示。

运行方式： python3 demo/run_demo.py

脚本一边驱动真实战斗（含真实扣血/回血），一边从内核只读投影渲染效果图标，
并把全部输入保存到 demo/scenario.json，随后从空引擎重放，逐字段核对
快照与触发日志完全一致。
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from effects import Engine, IconView
from effects import kernel as kernel_mod
from effects.replay import _dispatch, replay, run_commands, save_scenario

HERE = os.path.dirname(os.path.abspath(__file__))
SCENARIO_PATH = os.path.join(HERE, "scenario.json")

# 全部输入只描述“谁在何时申请/解除什么”，没有任何随机量。
ENTITIES = {"hero": 100, "enemy": 60}
COMMANDS = [
    # 1) STACK 独立计时：两个来源各挂一层 poison
    {"op": "apply", "name": "poison", "target": "hero", "source": "spider1"},
    {"op": "apply", "name": "poison", "target": "hero", "source": "spider2"},
    # 2) burn（周期 2）用于观察与到期的同刻顺序
    {"op": "apply", "name": "burn", "target": "hero", "source": "imp",
     "duration": 8},
    # 3) 控制效果（无周期）
    {"op": "apply", "name": "freeze", "target": "enemy", "source": "mage"},
    {"op": "advance", "steps": 2},
    # 4) 暂停/恢复：暂停区间不允许推进，恢复后不补发
    {"op": "pause"},
    {"op": "resume"},
    # 5) 临界刷新：regen 重新申请把持续期回满
    {"op": "apply", "name": "regen", "target": "enemy", "source": "priest"},
    {"op": "advance", "steps": 1},                         # t=3：poison 首跳
    {"op": "apply", "name": "regen", "target": "enemy", "source": "priest2"},
    # 6) 相互抑制：freeze 挂起 burn
    {"op": "apply", "name": "freeze", "target": "hero", "source": "mage"},
    {"op": "advance", "steps": 5},                        # freeze 到期释放 burn
    # 7) 按实例解除（不是按名字删集合）
    {"op": "dispel", "iid": 1},
    # 8) 来源退出：STACK 实例随来源移除；来源重建后产生全新 iid
    {"op": "destroy_source", "source": "spider2"},
    {"op": "apply", "name": "poison", "target": "hero", "source": "spider2"},
    # 9) 全体解除：逐实例记录 iid
    {"op": "dispel_all", "target": "hero"},
    {"op": "advance", "steps": 3},
]


def _new_engine() -> Engine:
    eng = Engine()
    for eid, hp in sorted(ENTITIES.items()):
        eng.register_entity(eid, hp)
    return eng


def _compact(ev: dict) -> str:
    skip = {"tid", "time", "type"}
    return ", ".join(f"{k}={v}" for k, v in ev.items() if k not in skip)


def _summarize_events(events, since: int) -> list[str]:
    lines = []
    for ev in events[since:]:
        t = ev["type"]
        if t == "hp":
            lines.append(
                f"  [tid {ev['tid']:>2}] HP {ev['target']} "
                f"{ev['before']}->{ev['after']} ({ev['delta']:+d}) "
                f"由 {ev['reason']['name']} 第 {ev['reason']['tick_no']} 跳触发")
        elif t == "tick":
            lines.append(
                f"  [tid {ev['tid']:>2}] 周期触发 tick_no={ev['tick_no']} "
                f"{ev['name']}@iid{ev['iid']} 作用于 {ev['target']}")
        elif t == "remove":
            lines.append(
                f"  [tid {ev['tid']:>2}] 移除 iid{ev['iid']} {ev['name']} "
                f"原因={ev['cause']} 剩余={ev['remaining']} 已跳={ev['ticks_done']}")
        elif t in ("suppress", "resume", "transform", "refresh", "restack",
                   "apply", "dispel_all", "source_destroy", "source_exit_kept",
                   "pause", "resume", "reaction_chain_terminated"):
            lines.append(f"  [tid {ev['tid']:>2}] {t}: {_compact(ev)}")
    return lines


def main() -> None:
    eng = _new_engine()
    view = IconView(eng)
    step = 0
    mark = 0

    def show(title: str) -> None:
        nonlocal step
        step += 1
        print(f"\n=== 步骤 {step}: {title} (t={eng.time}) ===")
        print("  " + view.render("hero"))
        print("  " + view.render("enemy"))

    show("初始状态")
    for idx, cmd in enumerate(COMMANDS):
        before = len(eng.events)
        _dispatch(eng, cmd)
        label = _compact({"op": cmd["op"], **{k: v for k, v in cmd.items()
                                              if k != "op"}})
        print(f"\n--- 命令 #{idx}: {label} ---")
        for line in _summarize_events(eng.events, before):
            print(line)
        show(f"命令后 #{idx}")

    # 链上限演示：制造无穷互相触发（自转换白盒场景），内核必须终止并报告关系链。
    print("\n=== 无穷互相触发链限制 ===")
    eng.apply_effect("burn", "hero", "loop_seed")
    saved = dict(kernel_mod.TRANSFORM_MAP)
    kernel_mod.TRANSFORM_MAP["burn"] = "burn"
    try:
        eng.apply_effect("burn", "hero", "loop_src")
    except RecursionError as exc:
        print(f"  内核终止: {exc}")
        print(f"  导致终止的效果关系链（长度 {len(eng.last_terminated_chain)}）:")
        for rel in eng.last_terminated_chain:
            print(f"    - {rel}")
    finally:
        kernel_mod.TRANSFORM_MAP.clear()
        kernel_mod.TRANSFORM_MAP.update(saved)

    # 保存输入并从空状态重放（与演示使用同一份命令表、同一条调度代码路径）。
    save_scenario(SCENARIO_PATH, ENTITIES, COMMANDS)
    reference = run_commands(ENTITIES, COMMANDS)
    replayed = replay(SCENARIO_PATH)
    print("\n=== 重放核对（从空引擎重建）===")
    print(f"  场景文件: {SCENARIO_PATH}")
    print(f"  快照一致: {replayed.snapshot() == reference.snapshot()}")
    print(f"  事件日志一致（含触发编号 tid 与生命差值）: "
          f"{replayed.events == reference.events}")
    print(f"  事件总数: {len(replayed.events)}")
    assert replayed.snapshot() == reference.snapshot()
    assert replayed.events == reference.events
    print("\n演示完成：重放与原执行逐字段一致。")


if __name__ == "__main__":
    main()
