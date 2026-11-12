"""命令录制与重放。

一份输入（实体表 + 有序命令表）足以从空引擎完整重建战斗。命令只描述
“谁在何时申请/解除什么”，不含任何随机量；周期跳伤完全由整数战斗时间
驱动，因此重放得到的快照、触发编号与生命差值逐字节一致。
"""

from __future__ import annotations

import json
from typing import Any, Optional

from .kernel import Engine


def run_commands(entities: dict[str, int],
                 commands: list[dict[str, Any]],
                 on_command=None) -> Engine:
    eng = Engine()
    for eid, hp in sorted(entities.items()):
        eng.register_entity(eid, hp)
    for idx, cmd in enumerate(commands):
        _dispatch(eng, cmd)
        if on_command is not None:
            on_command(eng, idx, cmd)
    return eng


def run_drill(entities: dict[str, int],
              commands: list[dict[str, Any]],
              on_command=None,
              on_rejection=None) -> Engine:
    """演练用运行入口。

    与 run_commands 同一条调度路径，但允许标注 ``expect`` 的命令被内核拒绝：
    拒绝会记录到 ``engine.last_terminated_chain`` 并回调 ``on_rejection``，
    随后继续执行剩余命令，证明正常演练不受影响。未标注的拒绝仍原样抛出。
    """
    eng = Engine()
    for eid, hp in sorted(entities.items()):
        eng.register_entity(eid, hp)
    for idx, cmd in enumerate(commands):
        try:
            _dispatch(eng, cmd)
        except RecursionError as exc:
            if cmd.get("expect") != "reject_reaction_chain":
                raise
            if on_rejection is not None:
                on_rejection(eng, idx, cmd, exc)
        if on_command is not None:
            on_command(eng, idx, cmd)
    return eng


def _dispatch(eng: Engine, cmd: dict[str, Any]) -> None:
    op = cmd["op"]
    if op == "apply":
        kwargs = {k: v for k, v in cmd.items()
                  if k in ("duration", "amount", "potency") and v is not None}
        eng.apply_effect(cmd["name"], cmd["target"], cmd["source"], **kwargs)
    elif op == "advance":
        eng.advance(cmd.get("steps", 1))
    elif op == "pause":
        eng.pause()
    elif op == "resume":
        eng.resume()
    elif op == "dispel":
        eng.dispel_instance(cmd["iid"])
    elif op == "dispel_all":
        eng.dispel_all(cmd["target"], set(cmd["names"]) if cmd.get("names") else None)
    elif op == "destroy_source":
        eng.destroy_source(cmd["source"])
    elif op == "register":
        eng.register_entity(cmd["entity"], cmd["hp"])
    elif op == "define_transform":
        # 反例素材声明的非法触发关系；仅影响该演练实例，随命令重放恢复。
        eng.transform_overrides[cmd["from_name"]] = cmd["to_name"]
    elif op == "clear_transform":
        eng.transform_overrides.pop(cmd["from_name"], None)
    elif op == "camera":
        # 仅作画面标记，不改变战斗状态；生成器据此取帧。
        eng._emit("camera", mark=cmd["mark"])
    else:
        raise ValueError(f"未知命令: {op}")


def save_scenario(path: str, entities: dict[str, int],
                  commands: list[dict[str, Any]]) -> None:
    with open(path, "w", encoding="utf-8") as fh:
        json.dump({"entities": entities, "commands": commands}, fh,
                  ensure_ascii=False, indent=2, sort_keys=True)


def replay(path: str) -> Engine:
    with open(path, "r", encoding="utf-8") as fh:
        data = json.load(fh)
    return run_commands(data["entities"], data["commands"])
