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
                 commands: list[dict[str, Any]]) -> Engine:
    eng = Engine()
    for eid, hp in sorted(entities.items()):
        eng.register_entity(eid, hp)
    for cmd in commands:
        _dispatch(eng, cmd)
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
