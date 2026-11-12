"""演练播放器：读取播放清单，把素材装入现有运行器回放。

用法：
    python3 tools/play_drills.py              # 回放全部
    python3 tools/play_drills.py fast_stacking
    python3 tools/play_drills.py --list

只读取 drills/ 的工程素材并调用 effects.replay.run_drill；不依赖任何预录
视频。每帧的伤害数字来自内核事件，相机标记在对应时点打印。
"""

from __future__ import annotations

import json
import os
import sys
from typing import Any

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from effects import Engine, IconView  # noqa: E402
from effects.replay import run_drill  # noqa: E402

PLAYLIST = os.path.join(ROOT, "drills", "playlist.json")


def load_playlist() -> list[dict[str, Any]]:
    with open(PLAYLIST, encoding="utf-8") as fh:
        return json.load(fh)["drills"]


def load_drill(entry: dict[str, Any]) -> dict[str, Any]:
    path = os.path.join(ROOT, entry["input"])
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def play(entry: dict[str, Any]) -> Engine:
    drill = load_drill(entry)
    print(f"\n=== {drill['id']} — {drill['title']} ===")
    print(f"种子 {drill['seed']} | 相机顺序 {' -> '.join(drill['camera_order'])}")
    print(f"说明: {drill['note']}")
    print("观察点:")
    for item in drill["observe"]:
        print(f"  - {item}")

    state = {"view": None, "seen": 0}

    def on_rejection(eng, idx, cmd, exc):
        print(f"  [拒绝] 第 {idx} 条命令 {cmd['name']}@{cmd['target']}/"
              f"{cmd['source']}: {exc}")
        print(f"          关系链 {len(eng.last_terminated_chain)} 层已记录；"
              f"继续回放后续命令。")

    def on_command(eng, idx, cmd) -> None:
        if state["view"] is None:
            state["view"] = IconView(eng)
        if cmd["op"] == "camera":
            print(f"  [相机] {cmd['mark']}")
            return
        label = {
            "apply": f"apply {cmd.get('name')}@{cmd.get('target')}"
                     f"<-{cmd.get('source')}",
            "advance": f"advance {cmd.get('steps', 1)} 拍",
            "destroy_source": f"destroy_source {cmd.get('source')}",
            "dispel": f"dispel iid={cmd.get('iid')}",
            "dispel_all": f"dispel_all {cmd.get('target')}",
            "pause": "pause",
            "resume": "resume",
            "define_transform": f"define_transform {cmd.get('from_name')}"
                                f"->{cmd.get('to_name')}",
            "clear_transform": f"clear_transform {cmd.get('from_name')}",
        }.get(cmd["op"], cmd["op"])
        print(f"  命令 #{idx:02d} {label}")

        for ev in eng.events[state["seen"]:]:
            if ev["type"] != "hp":
                continue
            reason = ev.get("reason") or {}
            name = reason.get("name", "?")
            tick_no = reason.get("tick_no", "?")
            signed = f"{ev['delta']:+d}"
            print(f"      [{ev['time']}] {signed} → {ev['target']} "
                  f"{ev['before']}->{ev['after']} ({name} 第 {tick_no} 跳)")
        state["seen"] = len(eng.events)

        for entity in sorted(eng.hp):
            print("    " + state["view"].render(entity))

    eng = run_drill(drill["entities"], drill["commands"],
                    on_command=on_command, on_rejection=on_rejection)
    print(f"  结束 t={eng.time} hp={eng.hp}")
    if drill.get("rejection"):
        rec = drill["rejection"]
        print(f"  反例证据: {rec['error']}")
        print(f"  终止关系链长度 {len(rec['chain'])}（上限 {rec['limit']}）")
        print(f"  拒绝后剩余命令已继续执行至 t={eng.time}")
    return eng


def main(argv: list[str]) -> None:
    entries = load_playlist()
    if "--list" in argv:
        for e in entries:
            print(f"{e['id']}: {e['title']}  [{e['input']}]")
        return
    chosen = [e for e in entries if e["id"] in argv] or entries
    for entry in chosen:
        play(entry)


if __name__ == "__main__":
    main(sys.argv[1:])
