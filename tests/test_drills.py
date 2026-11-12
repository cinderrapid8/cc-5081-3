"""演练素材工程测试。

覆盖仓库要求的几条硬契约：
- 相同种子恢复相同输入序列；
- 期望生命曲线来自独立计算，且与运行器一致；
- 反例的非法触发环被拒绝，同时正常演练继续；
- 清空输出目录后可从源素材完整重建；
- 缺少引用时定位到具体演练与素材。
"""

import json
import os
import shutil
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from effects.replay import run_drill  # noqa: E402
from tools import curve, generate_drills as gen, verify_drills as ver  # noqa: E402


class TestDeterministicInputs(unittest.TestCase):
    def test_same_seed_restores_identical_command_sequence(self):
        for drill in gen.load_sources():
            a = gen.seeded_commands(drill)
            b = gen.seeded_commands(drill)
            self.assertEqual(a, b, drill["id"])
            self.assertNotIn("random", json.dumps(a).lower())

    def test_no_random_token_leaks_into_kernel_input(self):
        for drill in gen.load_sources():
            commands = gen.seeded_commands(drill)
            json.dumps(commands)
            for cmd in commands:
                self.assertNotIn("random", cmd)


class TestIndependentCurve(unittest.TestCase):
    def test_curve_is_computed_outside_kernel_state(self):
        """计算器不读取也不写入主引擎 hp。"""
        drill = next(d for d in gen.load_sources()
                     if d["id"] == "fast_stacking")
        calc = curve.CurveCalculator(drill["entities"])
        for cmd in gen.seeded_commands(drill):
            curve.feed(calc, cmd, {})
        self.assertEqual(calc.curve("hero")[-1], 84)
        # 计算器自身的 hp 与曲线一致，且它不是内核实例。
        self.assertEqual(calc.hp["hero"], 84)
        self.assertFalse(hasattr(calc, "events"))

    def test_expected_curve_matches_replay_for_all_drills(self):
        playlist = _playlist()
        for entry in playlist["drills"]:
            with open(os.path.join(ROOT, entry["input"]), encoding="utf-8") as fh:
                drill = json.load(fh)
            issues = ver.verify_drill(entry["id"], drill, entry)
            self.assertEqual(issues, [], f"{entry['id']}: {issues}")


def _playlist():
    if not os.path.exists(gen.PLAYLIST):
        ver.rebuild(verbose=False)
    with open(gen.PLAYLIST, encoding="utf-8") as fh:
        return json.load(fh)


def _snapshot_outputs() -> dict[str, str]:
    out = {}
    for name in sorted(os.listdir(gen.DRILL_OUT)):
        if name.endswith(".json"):
            out[name] = gen._sha256(os.path.join(gen.DRILL_OUT, name))
    out["playlist.json"] = gen._sha256(gen.PLAYLIST)
    return out


class TestIllegalCycleRejectedWhilePlaybackContinues(unittest.TestCase):
    def test_rejection_leaves_evidence_and_later_commands_run(self):
        drill = next(d for d in gen.load_sources()
                     if d["id"] == "illegal_trigger_cycle")
        commands = gen.seeded_commands(drill)
        rejections = []

        def on_rejection(eng, idx, cmd, exc):
            rejections.append((idx, cmd, exc))

        eng = run_drill(drill["entities"], commands,
                        on_rejection=on_rejection)
        self.assertEqual(len(rejections), 1)
        _, cmd, exc = rejections[0]
        self.assertEqual(cmd["name"], "burn")
        self.assertTrue(eng.last_terminated_chain)
        # 拒绝后正常演练必须继续：hex 已申请，时间已推进。
        self.assertIn("hex", [i.name for i in eng.instances.values()])
        self.assertEqual(eng.time, 4)
        term = [e for e in eng.events if e["type"] == "reaction_chain_terminated"]
        self.assertEqual(len(term), 1)
        self.assertEqual(term[0]["limit"], curve.MAX_REACTION_DEPTH)

    def test_undeclared_rejection_still_raises(self):
        entities = {"hero": 100}
        commands = [
            {"op": "apply", "name": "burn", "target": "hero", "source": "a"},
            {"op": "define_transform", "from_name": "burn", "to_name": "burn"},
            {"op": "apply", "name": "burn", "target": "hero", "source": "b"},
        ]
        with self.assertRaises(RecursionError):
            run_drill(entities, commands)

    def test_declared_rejection_requires_following_advance(self):
        entities = {"hero": 100}
        commands = [
            {"op": "apply", "name": "burn", "target": "hero", "source": "a"},
            {"op": "define_transform", "from_name": "burn", "to_name": "burn"},
            {"op": "apply", "name": "burn", "target": "hero", "source": "b",
             "expect": "reject_reaction_chain"},
        ]
        drill = {"id": "no_continue", "entities": entities,
                 "commands": commands, "expected_hp": {"hero": [100]},
                 "rejection": {"chain": [], "limit": 8, "evidence": [{}]},
                 "frames": []}
        issues = ver.verify_drill("no_continue", drill,
                                  {"time_horizon": 0})
        self.assertTrue(any("播放继续" in i for i in issues), issues)


class TestRebuildAndMissingReferences(unittest.TestCase):
    def test_rebuild_from_empty_output_is_complete(self):
        problems = ver.rebuild(verbose=False)
        self.assertEqual(problems if problems else [], [])
        with open(gen.PLAYLIST, encoding="utf-8") as fh:
            playlist = json.load(fh)
        self.assertEqual(len(playlist["drills"]),
                         len(gen.load_sources()))

    def test_rebuild_is_byte_identical(self):
        gen.build_all(verbose=False)
        first = _snapshot_outputs()
        gen.build_all(verbose=False)
        second = _snapshot_outputs()
        self.assertEqual(first, second)

    def test_missing_reference_is_located(self):
        with open(gen.PLAYLIST, encoding="utf-8") as fh:
            playlist = json.load(fh)
        broken = json.loads(json.dumps(playlist))
        broken["drills"][0]["input"] = "drills/out/does_not_exist.json"
        problems = ver.locate_missing_reference(broken)
        self.assertTrue(problems)
        self.assertIn(broken["drills"][0]["id"], problems[0])
        self.assertIn("does_not_exist.json", problems[0])

    def test_tampered_effect_config_is_located(self):
        with open(gen.PLAYLIST, encoding="utf-8") as fh:
            playlist = json.load(fh)
        entry = next(e for e in playlist["drills"]
                     if e["id"] == "low_frequency_high_intensity")
        with open(os.path.join(ROOT, entry["input"]), encoding="utf-8") as fh:
            drill = json.load(fh)
        tampered = json.loads(json.dumps(drill))
        for cmd in tampered["commands"]:
            if cmd["op"] == "apply":
                cmd["amount"] = 7
                break
        issues = ver.verify_drill(entry["id"], tampered, entry)
        self.assertTrue(any("apply 命令" in i for i in issues), issues)


if __name__ == "__main__":
    unittest.main()
