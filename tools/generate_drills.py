"""战斗演练素材生成器。

职责分工：
- 本文件只“组装素材”：角色配置、施加/推进/解除事件、相机标记、文字说明。
- 实际运行交给 effects.replay 的现有运行器；画面伤害数字由内核规则算出。
- 期望生命曲线由 tools.curve 的独立小规模计算取得，不写入角色状态；
  生成器只把两边逐拍交叉核对，不一致即报出具体演练与效果配置。

相同随机种子 -> 相同输入序列。种子只用于切分预定义的推进节奏，不会把
随机量带进战斗内核。
"""

from __future__ import annotations

import hashlib
import json
import os
import random
import sys
from typing import Any, Optional

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from effects import IconView  # noqa: E402
from effects.replay import run_drill as replay_run_drill  # noqa: E402
from tools import curve  # noqa: E402

DRILL_SOURCE = os.path.join(ROOT, "drills", "source")
DRILL_OUT = os.path.join(ROOT, "drills", "out")
PLAYLIST = os.path.join(ROOT, "drills", "playlist.json")


# --------------------------------------------------------------------- #
# 素材加载：可编辑源素材位于 drills/source/*.json
# --------------------------------------------------------------------- #
def load_sources() -> list[dict[str, Any]]:
    if not os.path.isdir(DRILL_SOURCE):
        raise FileNotFoundError(f"素材源目录不存在: {DRILL_SOURCE}")
    drills: list[dict[str, Any]] = []
    for name in sorted(os.listdir(DRILL_SOURCE)):
        if not name.endswith(".json"):
            continue
        with open(os.path.join(DRILL_SOURCE, name), encoding="utf-8") as fh:
            drills.append(json.load(fh))
    if not drills:
        raise FileNotFoundError(f"素材源目录为空: {DRILL_SOURCE}")
    return drills


# --------------------------------------------------------------------- #
# 确定性输入序列
# --------------------------------------------------------------------- #
def seeded_commands(drill: dict[str, Any]) -> list[dict[str, Any]]:
    """同一素材 + 同一种子 -> 完全相同的命令序列。

    种子只决定把单条 advance 切成 1~2 段的位置；命令顺序、相机标记位置
    和战斗内容一律不动，因此重放输入可复现，且不含内核可见的随机量。
    """
    rng = random.Random(drill["seed"])
    out: list[dict[str, Any]] = []
    for cmd in drill["commands"]:
        if cmd["op"] != "advance":
            out.append(dict(cmd))
            continue
        steps = cmd.get("steps", 1)
        splits = [steps]
        if steps >= 2 and rng.random() < 0.5:
            cut = rng.randint(1, steps - 1)
            splits = [cut, steps - cut]
        for piece in splits:
            out.append({"op": "advance", "steps": piece})
    return out


def _camera_marks(commands: list[dict]) -> list[dict]:
    """相机标记定位为“已执行完的战斗命令条数”，与运行器索引一致。"""
    marks: list[dict] = []
    battle_done = 0
    for cmd in commands:
        if cmd["op"] == "camera":
            marks.append({"mark": cmd["mark"], "after_battle_command": battle_done})
        else:
            battle_done += 1
    return marks


# --------------------------------------------------------------------- #
# 执行 + 取帧（全部经 effects.replay 的现有运行器路径）
# --------------------------------------------------------------------- #
class DrillRun:
    def __init__(self) -> None:
        self.engine = None
        self.view = None
        self.rejection: Optional[dict[str, Any]] = None


def run_drill(entities: dict[str, int],
              commands: list[dict]) -> tuple[DrillRun, list[dict]]:
    """逐命令执行；预期拒绝记录证据后由运行器继续跑后续正常命令。"""
    run = DrillRun()
    frames: list[dict] = []

    def on_rejection(eng, idx, cmd, exc) -> None:
        run.rejection = {
            "after_command": idx,
            "effect": {"name": cmd.get("name"), "target": cmd.get("target"),
                       "source": cmd.get("source")},
            "error": str(exc),
            "chain": list(eng.last_terminated_chain or []),
            "limit": curve.MAX_REACTION_DEPTH,
            "evidence": [e for e in eng.events
                         if e["type"] == "reaction_chain_terminated"],
        }

    def on_command(eng, idx, cmd) -> None:
        if run.view is None:
            run.view = IconView(eng)
        camera = cmd["mark"] if cmd["op"] == "camera" else (
            frames[-1]["camera"] if frames else None)
        events_since = frames[-1]["_seen"] if frames else 0
        new_events = eng.events[events_since:]
        frames.append({
            "after_command": idx,
            "camera": camera,
            "time": eng.time,
            "hp": dict(eng.hp),
            "renders": {t: run.view.render(t) for t in sorted(eng.hp)},
            # 画面数字全部取自内核事件，不在此处手工计算。
            "numbers": [
                {"kind": e["type"], "target": e.get("target"),
                 "before": e.get("before"), "after": e.get("after"),
                 "delta": e.get("delta"), "reason": e.get("reason")}
                for e in new_events if e["type"] == "hp"
            ],
            "events": [e["type"] for e in new_events],
            "_seen": len(eng.events),
        })

    engine = replay_run_drill(entities, commands,
                              on_command=on_command,
                              on_rejection=on_rejection)
    run.engine = engine
    run.view = IconView(engine)
    for fr in frames:
        fr.pop("_seen", None)
    return run, frames


# --------------------------------------------------------------------- #
def build_all(verbose: bool = True) -> list[dict]:
    os.makedirs(DRILL_OUT, exist_ok=True)
    playlist: list[dict] = []
    for drill in load_sources():
        entry = build_drill(drill, verbose)
        playlist.append(entry)
    with open(PLAYLIST, "w", encoding="utf-8") as fh:
        json.dump({"drills": playlist}, fh, ensure_ascii=False, indent=2,
                  sort_keys=True)
    if verbose:
        print(f"\n播放清单: {os.path.relpath(PLAYLIST, ROOT)} "
              f"({len(playlist)} 段)")
    return playlist


def build_drill(drill: dict, verbose: bool = True) -> dict:
    drill_id = drill["id"]
    commands = seeded_commands(drill)
    entities = drill["entities"]

    run, frames = run_drill(entities, commands)

    # 期望曲线：独立计算器在相同命令序列上再跑一遍，逐拍交叉核对。
    calc = curve.CurveCalculator(entities)
    overrides: dict[str, str] = {}
    for cmd in commands:
        curve.feed(calc, cmd, overrides)

    _check_frames(drill_id, frames, calc)
    expected = {t: calc.curve(t) for t in entities}

    path = os.path.join(DRILL_OUT, f"{drill_id}.json")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump({
            "id": drill_id,
            "title": drill["title"],
            "seed": drill["seed"],
            "note": drill["note"],
            "observe": drill["observe"],
            "entities": entities,
            "camera_order": drill["camera_order"],
            "camera_marks": _camera_marks(commands),
            "commands": commands,
            "frames": frames,
            "rejection": run.rejection,
            "expected_curve_source": "tools.curve（独立小规模计算，非内核状态）",
            "expected_hp": expected,
        }, fh, ensure_ascii=False, indent=2, sort_keys=True)

    entry = {
        "id": drill_id,
        "title": drill["title"],
        "seed": drill["seed"],
        "input": os.path.relpath(path, ROOT),
        "input_sha256": _sha256(path),
        "time_horizon": run.engine.time,
        "camera_order": drill["camera_order"],
        "observe": drill["observe"],
        "note": drill["note"],
        "rejected": run.rejection is not None,
        "expected_hp_end": {t: v[-1] for t, v in expected.items()},
    }
    if verbose:
        tail = " [预期拒绝已捕获]" if run.rejection else ""
        ends = ", ".join(f"{t}={v[-1]}" for t, v in expected.items())
        print(f"[ok] {drill_id}: {len(commands)} 条命令, "
              f"t={run.engine.time}, 期望终点 {ends}{tail}")
    return entry


def _check_frames(drill_id: str, frames: list[dict],
                  calc: curve.CurveCalculator) -> None:
    """把内核帧里的 hp 与独立计算的逐拍曲线对齐校验。

    同一时刻可能有多帧；取该时刻最后一帧与独立曲线比对。
    """
    per_time: dict[int, dict[str, int]] = {}
    for fr in frames:
        per_time[fr["time"]] = fr["hp"]
    for time, hp_map in per_time.items():
        for target, actual in hp_map.items():
            series = calc.curve(target)
            if time < len(series) and series[time] != actual:
                raise AssertionError(
                    f"{drill_id}: t={time} {target} 实际 hp={actual} "
                    f"与独立期望 {series[time]} 不一致；"
                    f"效果配置需要定位到素材中的 apply 命令")


def _sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()


def main() -> None:
    build_all()


if __name__ == "__main__":
    main()
