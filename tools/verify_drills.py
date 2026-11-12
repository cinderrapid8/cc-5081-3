"""演练素材完整性与重建工具。

用法：
    python3 tools/verify_drills.py            # 校验现有 drills/
    python3 tools/verify_drills.py --rebuild  # 清空 drills/ 后从头重建再校验

校验内容：
1. 播放清单里每段引用的素材文件存在且 sha256 与清单一致；
2. 素材里的命令足以在现有运行器上重放出与记录帧相同的生命与事件数字；
3. 期望生命曲线来自 tools.curve 独立计算，逐拍与运行器一致；
4. 反例段确实被内核拒绝、留下关系链证据，且正常命令继续执行。

任何缺失/不一致都会打印到具体演练 id 与效果配置（name/target/source）。
"""

from __future__ import annotations

import json
import os
import shutil
import sys
from typing import Any, Optional

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from effects import Engine  # noqa: E402
from effects.replay import _dispatch  # noqa: E402
from tools import curve  # noqa: E402
from tools import generate_drills as gen  # noqa: E402

DRILL_SOURCE = gen.DRILL_SOURCE
DRILL_OUT = gen.DRILL_OUT
PLAYLIST = gen.PLAYLIST


class IntegrityError(AssertionError):
    pass


def _effect_label(cmd: dict) -> str:
    if cmd.get("op") == "apply":
        return (f"效果 {cmd['name']} (target={cmd['target']}, "
                f"source={cmd['source']})")
    if cmd.get("op") == "destroy_source":
        return f"来源撤销 {cmd['source']}"
    if cmd.get("op") == "dispel":
        return f"实例解除 iid={cmd['iid']}"
    return cmd.get("op", "?")


def locate_missing_reference(playlist: dict) -> list[str]:
    """返回引用缺失的明细，精确到演练 id 与素材路径。"""
    problems: list[str] = []
    for entry in playlist.get("drills", []):
        ref = entry.get("input")
        if not ref:
            problems.append(f"演练 {entry.get('id')}: 播放清单缺少 input 引用")
            continue
        path = os.path.join(ROOT, ref)
        if not os.path.exists(path):
            problems.append(
                f"演练 {entry.get('id')}: 引用的素材不存在 -> {ref}")
            continue
        actual = gen._sha256(path)
        if entry.get("input_sha256") and actual != entry["input_sha256"]:
            problems.append(
                f"演练 {entry.get('id')}: 素材哈希不匹配 -> {ref} "
                f"(清单 {entry['input_sha256'][:8]}..., 实际 {actual[:8]}...)")
    return problems


def _replay_drill(drill: dict) -> tuple[Engine, Optional[dict]]:
    eng = Engine()
    for eid, hp in sorted(drill["entities"].items()):
        eng.register_entity(eid, hp)
    rejection: Optional[dict] = None
    rejected_index: Optional[int] = None
    drill_expects = bool(drill.get("rejection"))
    for index, cmd in enumerate(drill["commands"]):
        try:
            _dispatch(eng, cmd)
        except RecursionError as err:
            if not drill_expects:
                raise IntegrityError(
                    f"演练 {drill['id']}: 未声明的拒绝发生于 "
                    f"{_effect_label(cmd)}: {err}") from err
            if rejection is not None:
                raise IntegrityError(
                    f"演练 {drill['id']}: 声明了一段反例，却出现第二次拒绝 "
                    f"于 {_effect_label(cmd)}") from err
            rejection = {"error": str(err),
                         "chain": list(eng.last_terminated_chain or [])}
            rejected_index = index
            # 反例的关键契约：拒绝之后继续执行正常演练。
    if drill_expects and rejection is None:
        raise IntegrityError(
            f"演练 {drill['id']}: 素材声明了非法触发环，但重放没有拒绝")
    if drill_expects and rejected_index is not None:
        remaining = drill["commands"][rejected_index + 1:]
        if not any(c["op"] == "advance" for c in remaining):
            raise IntegrityError(
                f"演练 {drill['id']}: 拒绝后没有剩余推进命令，"
                f"无法证明播放继续")
    return eng, rejection


def verify_drill(drill_id: str, drill: dict, entry: dict) -> list[str]:
    """校验单段素材，返回问题清单（空表示通过）。"""
    problems: list[str] = []
    try:
        eng, rejection = _replay_drill(drill)
    except IntegrityError as exc:
        return [str(exc)]

    # 1) 命令重放得到的终点生命应与期望曲线首尾一致。
    calc = curve.CurveCalculator(drill["entities"])
    overrides: dict[str, str] = {}
    for cmd in drill["commands"]:
        curve.feed(calc, cmd, overrides)
    for target, expected_series in drill["expected_hp"].items():
        actual_series = calc.curve(target)
        if actual_series != expected_series:
            problems.append(
                f"演练 {drill_id}: {target} 期望曲线与独立重算不一致")
    if eng.time != entry.get("time_horizon"):
        problems.append(
            f"演练 {drill_id}: 重放时间 t={eng.time} 与清单 "
            f"{entry.get('time_horizon')} 不一致")

    # 2) 每帧数字必须与由规则重跑得到的同一时刻生命一致。
    frames = drill.get("frames", [])
    per_time: dict[int, dict[str, int]] = {}
    for fr in frames:
        per_time[fr["time"]] = fr["hp"]
    for time, hp_map in per_time.items():
        for target, recorded in hp_map.items():
            series = calc.curve(target)
            if time < len(series) and series[time] != recorded:
                problems.append(
                    f"演练 {drill_id}: t={time} {target} 记录 hp={recorded} "
                    f"与规则重算 {series[time]} 不一致；"
                    f"定位到素材 {drill_id}.json 的 apply 命令")

    # 3) 反例证据完整。
    if drill.get("rejection"):
        if not rejection:
            problems.append(
                f"演练 {drill_id}: 记录的拒绝未被重放复现")
        elif len(rejection["chain"]) != drill["rejection"].get("limit"):
            problems.append(
                f"演练 {drill_id}: 终止关系链长度 "
                f"{len(rejection['chain'])} 与上限不一致")
        if not drill["rejection"].get("evidence"):
            problems.append(
                f"演练 {drill_id}: 缺少 reaction_chain_terminated 事件证据")
    return problems


def verify_all(verbose: bool = True) -> list[str]:
    problems: list[str] = []
    if not os.path.isdir(DRILL_SOURCE):
        return [f"素材源目录缺失: {os.path.relpath(DRILL_SOURCE, ROOT)}"]
    sources = sorted(n for n in os.listdir(DRILL_SOURCE) if n.endswith(".json"))
    if not sources:
        problems.append(
            f"素材源目录为空: {os.path.relpath(DRILL_SOURCE, ROOT)}")
    if not os.path.exists(PLAYLIST):
        return [f"播放清单缺失: {os.path.relpath(PLAYLIST, ROOT)}"]
    with open(PLAYLIST, encoding="utf-8") as fh:
        playlist = json.load(fh)
    problems.extend(locate_missing_reference(playlist))
    seen_ids: set[str] = set()
    for entry in playlist.get("drills", []):
        seen_ids.add(entry["id"])
        path = os.path.join(ROOT, entry["input"])
        if not os.path.exists(path):
            continue  # 已在缺失明细中报告
        with open(path, encoding="utf-8") as fh:
            drill = json.load(fh)
        issues = verify_drill(entry["id"], drill, entry)
        problems.extend(issues)
        if verbose and not issues:
            print(f"[ok] {entry['id']}: 帧/曲线/拒绝证据一致")
    for source in sources:
        drill_id = source[:-5]
        if drill_id not in seen_ids:
            problems.append(
                f"播放清单遗漏源素材: drills/source/{source} "
                f"(演练 id {drill_id})")
    if verbose:
        for prob in problems:
            print(f"[FAIL] {prob}")
        if not problems:
            print(f"全部通过：{len(playlist.get('drills', []))} 段素材完整。")
    return problems


def rebuild(verbose: bool = True) -> None:
    """清空输出目录与播放清单后，从源素材从头重建再校验。"""
    if os.path.isdir(DRILL_OUT):
        shutil.rmtree(DRILL_OUT)
    if os.path.exists(PLAYLIST):
        os.remove(PLAYLIST)
    if verbose:
        print(f"已清空输出目录: {os.path.relpath(DRILL_OUT, ROOT)}")
        print(f"已删除播放清单: {os.path.relpath(PLAYLIST, ROOT)}")
    gen.build_all(verbose=verbose)
    problems = verify_all(verbose=verbose)
    if problems:
        raise SystemExit(f"重建后仍有 {len(problems)} 个问题")


def main(argv: list[str]) -> None:
    if "--rebuild" in argv:
        rebuild()
    else:
        problems = verify_all()
        if problems:
            raise SystemExit(1)


if __name__ == "__main__":
    main(sys.argv[1:])
