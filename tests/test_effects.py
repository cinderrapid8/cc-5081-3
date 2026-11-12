import json
import os
import tempfile
import unittest

from effects import Engine, EnginePausedError, StackPolicy, SPECS
from effects import IconView, replay, save_scenario, run_commands
from effects import kernel as kernel_mod


class BaseCase(unittest.TestCase):
    def make_engine(self, hp=None):
        eng = Engine()
        for eid, val in (hp or {"hero": 100, "enemy": 60}).items():
            eng.register_entity(eid, val)
        return eng


class TestStackPolicy(BaseCase):
    def test_independent_sources_independent_instances(self):
        eng = self.make_engine()
        i1 = eng.apply_effect("poison", "hero", "spider1")
        i2 = eng.apply_effect("poison", "hero", "spider2")
        self.assertNotEqual(i1.iid, i2.iid)
        self.assertEqual(len(eng.find("hero", "poison")), 2)

        eng.advance(3)
        # 同一时刻两跳，按 iid 排序；每跳 -4。
        ticks = [e for e in eng.events if e["type"] == "tick"]
        self.assertEqual([t["iid"] for t in ticks], [i1.iid, i2.iid])
        self.assertEqual(eng.hp["hero"], 92)

        eng.advance(7)  # 到 t=10：每实例在 3/6/9 各一跳，t=10 到期
        self.assertEqual(eng.hp["hero"], 100 - 6 * 4)
        self.assertEqual(eng.find("hero", "poison"), [])
        removes = [e for e in eng.events if e["type"] == "remove"]
        self.assertEqual({e["cause"] for e in removes}, {"expire"})

    def test_same_source_restacks_potency(self):
        eng = self.make_engine()
        inst = eng.apply_effect("poison", "hero", "spider1")
        eng.apply_effect("poison", "hero", "spider1")
        self.assertEqual(len(eng.find("hero", "poison")), 1)
        self.assertEqual(inst.amount, 8)
        eng.advance(3)
        self.assertEqual(eng.hp["hero"], 92)

    def test_source_exit_removes_only_owned_stack_instances(self):
        eng = self.make_engine()
        i1 = eng.apply_effect("poison", "hero", "spider1")
        i2 = eng.apply_effect("poison", "hero", "spider2")
        removed = eng.destroy_source("spider2")
        self.assertEqual(removed, [i2.iid])
        self.assertEqual([i.iid for i in eng.find("hero", "poison")], [i1.iid])
        # 被提前解除的实例 ticks_done 记录在 remove 事件中，可追溯。
        ev = [e for e in eng.events if e["type"] == "remove"][0]
        self.assertEqual(ev["iid"], i2.iid)
        self.assertEqual(ev["cause"], "source_exit")


class TestRefreshPolicy(BaseCase):
    def _wounded(self, eng, hp=40):
        eng.hp["hero"] = hp

    def test_no_tick_at_endpoint_and_natural_expiry(self):
        eng = self.make_engine()
        self._wounded(eng)
        inst = eng.apply_effect("regen", "hero", "priest", duration=6)
        eng.advance(6)
        tick_times = [e["time"] for e in eng.ticks_of(inst.iid)]
        self.assertEqual(tick_times, [3])          # 周期点 6 与到期重合，不跳
        rm = [e for e in eng.events if e["type"] == "remove" and e["iid"] == inst.iid]
        self.assertEqual(rm[0]["cause"], "expire")

    def test_critical_refresh_at_last_remaining_turn(self):
        eng = self.make_engine()
        self._wounded(eng)
        inst = eng.apply_effect("regen", "hero", "priest", duration=6)
        eng.advance(5)                              # 剩余 1，原本下一刻到期
        self.assertEqual(inst.remaining, 1)
        eng.apply_effect("regen", "hero", "priest2", duration=6)
        self.assertEqual(inst.remaining, 6)        # 持续期刷新，实例 iid 不变
        self.assertEqual(inst.ticks_done, 0)       # 周期计数归零重新累计
        # 若刷新失败，t=6 就该到期；这里实例必须存活。刷新后周期从零累计，
        # 下一跳在刷新后的第 3 个有效拍（t=8）。
        eng.advance(4)
        self.assertIn(inst.iid, eng.instances)
        tick_times = [e["time"] for e in eng.ticks_of(inst.iid)]
        self.assertEqual(tick_times, [3, 8])

    def test_refresh_event_reports_remaining_before_after(self):
        eng = self.make_engine()
        eng.apply_effect("regen", "hero", "priest", duration=6)
        eng.advance(4)
        eng.apply_effect("regen", "hero", "priest")
        ev = [e for e in eng.events if e["type"] == "refresh"][0]
        self.assertEqual((ev["remaining_before"], ev["remaining_after"]), (2, 6))

    def test_source_exit_kept_until_natural_expiry(self):
        eng = self.make_engine()
        self._wounded(eng)
        inst = eng.apply_effect("regen", "hero", "priest", duration=6)
        eng.destroy_source("priest")
        self.assertIn(inst.iid, eng.instances)     # REFRESH：来源退出不撤销
        kept = [e for e in eng.events if e["type"] == "source_exit_kept"]
        self.assertEqual(kept[0]["iid"], inst.iid)
        eng.advance(6)
        self.assertNotIn(inst.iid, eng.instances)  # 自然到期


class TestReplacePolicy(BaseCase):
    def test_replacement_swaps_instance(self):
        eng = self.make_engine()
        old = eng.apply_effect("rage", "hero", "orc", duration=10)
        eng.advance(2)
        new = eng.apply_effect("rage", "hero", "orc", duration=10)
        self.assertNotEqual(old.iid, new.iid)
        self.assertEqual([i.iid for i in eng.find("hero", "rage")], [new.iid])
        rm = [e for e in eng.events if e["type"] == "remove"]
        self.assertEqual(rm[0]["cause"], "replaced")
        eng.advance(10)                             # 新实例从 t=2 起活 10 拍
        self.assertEqual(eng.find("hero", "rage"), [])

    def test_source_exit_removes_replace_instance(self):
        eng = self.make_engine()
        inst = eng.apply_effect("freeze", "enemy", "mage")
        eng.destroy_source("mage")
        self.assertNotIn(inst.iid, eng.instances)


class TestShortEffect(BaseCase):
    def test_duration_shorter_than_period_never_ticks(self):
        eng = self.make_engine()
        inst = eng.apply_effect("burn", "hero", "imp", duration=1)  # 周期 2
        eng.advance(1)
        self.assertEqual(eng.ticks_of(inst.iid), [])
        self.assertNotIn(inst.iid, eng.instances)
        self.assertEqual(eng.hp["hero"], 100)      # 没有任何跳伤


class TestPauseResume(BaseCase):
    def test_paused_clock_emits_no_catchup_damage(self):
        live = self.make_engine()
        paused = self.make_engine()
        for eng in (live, paused):
            eng.apply_effect("burn", "hero", "imp", duration=8)  # 周期 2

        live.advance(8)
        paused.advance(2)
        paused.pause()
        with self.assertRaises(EnginePausedError):
            paused.advance(3)                      # 暂停区间战斗时间不动
        paused.resume()
        paused.advance(6)

        self.assertEqual(paused.time, live.time, 8)
        self.assertEqual(paused.hp["hero"], live.hp["hero"])     # 均为 100-9
        self.assertEqual(paused.hp["hero"], 91)
        # 暂停引擎在暂停期间没有任何 tick 事件。
        pause_t = [e["time"] for e in paused.events if e["type"] == "pause"][0]
        resume_t = [e["time"] for e in paused.events if e["type"] == "resume"][0]
        mid_ticks = [e for e in paused.events
                     if e["type"] == "tick" and pause_t <= e["time"] < resume_t]
        self.assertEqual(mid_ticks, [])


class TestTickExpiryOrdering(BaseCase):
    def test_ticks_before_expiry_at_same_time_and_endpoint_suppressed(self):
        eng = self.make_engine()
        poison = eng.apply_effect("poison", "hero", "spider", duration=6)  # 周期 3
        burn = eng.apply_effect("burn", "hero", "imp", duration=8)         # 周期 2
        eng.advance(6)
        at_six = [e for e in eng.events if e["time"] == 6 and e["type"] in ("tick", "remove")]
        self.assertEqual([e["type"] for e in at_six], ["tick", "remove"])
        self.assertEqual(at_six[0]["iid"], burn.iid)
        self.assertEqual(at_six[1]["iid"], poison.iid)
        # poison 在到期点 t=6 不产生第三跳，只有 t3 一跳。
        self.assertEqual(len(eng.ticks_of(poison.iid)), 1)
        self.assertEqual(eng.ticks_of(poison.iid)[0]["time"], 3)
        # burn 在 t2/4/6 各一跳，t8 到期不跳。
        self.assertEqual([e["time"] for e in eng.ticks_of(burn.iid)], [2, 4, 6])


class TestSuppressAndTransform(BaseCase):
    def test_freeze_suppresses_burn_and_release_restores_remaining(self):
        eng = self.make_engine()
        burn = eng.apply_effect("burn", "hero", "imp")           # dur 8, period 2
        freeze = eng.apply_effect("freeze", "hero", "mage")      # dur 5
        self.assertEqual(burn.suppressed_by, freeze.iid)
        remaining_at_freeze = burn.remaining
        eng.advance(5)                                            # freeze 到期
        self.assertNotIn(freeze.iid, eng.instances)
        self.assertIsNone(burn.suppressed_by)
        self.assertEqual(burn.elapsed, 0)                        # 挂起期完全冻结
        self.assertEqual(burn.remaining, remaining_at_freeze)
        # 挂起的 5 拍不补发，恢复后周期从零累计，下一跳在 t7。
        self.assertEqual(eng.ticks_of(burn.iid), [])
        eng.advance(2)
        self.assertEqual([e["time"] for e in eng.ticks_of(burn.iid)], [7])
        self.assertEqual(eng.hp["hero"], 97)                     # 仅一跳 -3

    def test_suppressed_effect_skips_whole_paused_window(self):
        eng = self.make_engine()
        burn = eng.apply_effect("burn", "hero", "imp", duration=8)
        eng.apply_effect("freeze", "hero", "mage", duration=5)
        eng.advance(5)
        # 挂起窗口内 burn 没有任何 tick，也没有累计 elapsed。
        self.assertEqual(eng.ticks_of(burn.iid), [])
        self.assertEqual(burn.ticks_done, 0)

    def test_dispelling_suppressor_releases_effect(self):
        eng = self.make_engine()
        burn = eng.apply_effect("burn", "hero", "imp")
        freeze = eng.apply_effect("freeze", "hero", "mage")
        self.assertTrue(eng.dispel_instance(freeze.iid))
        self.assertIsNone(burn.suppressed_by)
        resume_ev = [e for e in eng.events if e["type"] == "resume"]
        self.assertEqual(resume_ev[0]["iid"], burn.iid)

    def test_transform_keeps_traceable_iid_both_directions(self):
        eng = self.make_engine()
        curse = eng.apply_effect("curse", "hero", "witch")
        iid = curse.iid
        eng.apply_effect("hex", "hero", "warlock")              # curse -> hex
        self.assertIn(iid, eng.instances)
        self.assertEqual(eng.instances[iid].name, "hex")
        ev = [e for e in eng.events if e["type"] == "transform"][0]
        self.assertEqual((ev["from_name"], ev["to_name"]), ("curse", "hex"))

        eng.apply_effect("curse", "hero", "witch2")            # hex -> curse
        self.assertEqual(eng.instances[iid].name, "curse")
        self.assertEqual(len(eng.instances), 1)                  # 始终是同一个实例


class TestDispelTargetsInstances(BaseCase):
    def test_dispel_instance_and_mass_dispel(self):
        eng = self.make_engine()
        p1 = eng.apply_effect("poison", "hero", "spider1")
        p2 = eng.apply_effect("poison", "hero", "spider2")
        p3 = eng.apply_effect("poison", "enemy", "spider1")
        self.assertTrue(eng.dispel_instance(p1.iid))
        self.assertFalse(eng.dispel_instance(99999))
        removed = eng.dispel_all("hero", {"poison"})
        self.assertEqual(removed, [p2.iid])
        self.assertIn(p3.iid, eng.instances)                    # 别的目标不动
        ev = [e for e in eng.events if e["type"] == "dispel_all"][0]
        self.assertEqual(ev["removed"], [p2.iid])

    def test_mass_dispel_without_name_filter(self):
        eng = self.make_engine()
        p1 = eng.apply_effect("poison", "hero", "spider1")
        b1 = eng.apply_effect("burn", "hero", "imp")
        removed = eng.dispel_all("hero")
        self.assertEqual(sorted(removed), sorted([p1.iid, b1.iid]))
        self.assertEqual(eng.find("hero"), [])


class TestSourceRebuild(BaseCase):
    def test_stack_source_rebuild_creates_new_traceable_instance(self):
        eng = self.make_engine()
        first = eng.apply_effect("poison", "hero", "spider")
        eng.destroy_source("spider")
        self.assertNotIn(first.iid, eng.instances)
        second = eng.apply_effect("poison", "hero", "spider")  # 同名来源重建
        self.assertNotEqual(first.iid, second.iid)
        self.assertEqual(second.source, "spider")

    def test_refresh_reapply_after_exit_retags_same_instance(self):
        eng = self.make_engine()
        inst = eng.apply_effect("regen", "hero", "priest")
        eng.destroy_source("priest")
        eng.apply_effect("regen", "hero", "priest")            # 来源重建并刷新
        self.assertIn(inst.iid, eng.instances)
        self.assertEqual(inst.source, "priest")
        eng.advance(6)
        self.assertNotIn(inst.iid, eng.instances)


class TestReactionChainLimit(BaseCase):
    def test_infinite_mutual_trigger_terminated_with_relation_chain(self):
        eng = self.make_engine()
        eng.apply_effect("burn", "hero", "imp")
        saved = dict(kernel_mod.TRANSFORM_MAP)
        kernel_mod.TRANSFORM_MAP["burn"] = "burn"               # 自互触发白盒场景
        try:
            with self.assertRaises(RecursionError) as ctx:
                eng.apply_effect("burn", "hero", "imp2")
        finally:
            kernel_mod.TRANSFORM_MAP.clear()
            kernel_mod.TRANSFORM_MAP.update(saved)
        self.assertIn("反应链超过", str(ctx.exception))
        term = [e for e in eng.events if e["type"] == "reaction_chain_terminated"]
        self.assertEqual(len(term), 1)
        self.assertEqual(term[0]["limit"], kernel_mod.MAX_REACTION_DEPTH)
        self.assertEqual(len(eng.last_terminated_chain), kernel_mod.MAX_REACTION_DEPTH)
        self.assertTrue(all(rel.startswith("transform:") for rel in
                            eng.last_terminated_chain))


class TestReplayDeterminism(BaseCase):
    def _scenario(self):
        entities = {"hero": 100, "enemy": 60}
        commands = [
            {"op": "apply", "name": "poison", "target": "hero", "source": "s1"},
            {"op": "apply", "name": "burn", "target": "hero", "source": "s2"},
            {"op": "advance", "steps": 2},
            {"op": "pause"},
            {"op": "resume"},
            {"op": "apply", "name": "freeze", "target": "hero", "source": "mage"},
            {"op": "advance", "steps": 5},
            {"op": "dispel", "iid": 1},
            {"op": "apply", "name": "regen", "target": "enemy", "source": "priest"},
            {"op": "destroy_source", "source": "s2"},
            {"op": "dispel_all", "target": "hero", "names": ["freeze"]},
            {"op": "advance", "steps": 3},
        ]
        return entities, commands

    def test_replay_from_empty_matches_bit_for_bit(self):
        entities, commands = self._scenario()
        eng1 = run_commands(entities, commands)
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "scenario.json")
            save_scenario(path, entities, commands)
            with open(path, encoding="utf-8") as fh:
                payload = json.load(fh)
            self.assertNotIn("random", json.dumps(payload).lower())
            eng2 = replay(path)
        self.assertEqual(eng1.snapshot(), eng2.snapshot())
        self.assertEqual(eng1.events, eng2.events)
        # 触发编号全局严格递增且无重复。
        tids = [e["tid"] for e in eng2.events]
        self.assertEqual(tids, list(range(1, len(tids) + 1)))

    def test_paused_state_survives_replay(self):
        entities = {"hero": 50}
        commands = [
            {"op": "apply", "name": "burn", "target": "hero", "source": "s"},
            {"op": "pause"},
        ]
        eng = run_commands(entities, commands)
        self.assertTrue(eng.paused)
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "p.json")
            save_scenario(path, entities, commands)
            again = replay(path)
        self.assertTrue(again.paused)
        with self.assertRaises(EnginePausedError):
            again.advance(1)
        again.resume()
        again.advance(2)
        self.assertEqual(again.hp["hero"], 47)


class TestIconView(BaseCase):
    def test_icon_stacks_and_remaining_read_from_kernel(self):
        eng = self.make_engine()
        p1 = eng.apply_effect("poison", "hero", "s1")
        p2 = eng.apply_effect("poison", "hero", "s2")
        view = IconView(eng)
        icons = {i.name: i for i in view.icons("hero")}
        self.assertEqual(icons["poison"].stacks, 2)
        self.assertEqual(icons["poison"].iids, (p1.iid, p2.iid))
        eng.advance(3)
        icons = {i.name: i for i in view.icons("hero")}
        self.assertEqual(icons["poison"].remaining, (7, 7))
        eng.dispel_instance(p1.iid)
        icons = {i.name: i for i in view.icons("hero")}
        self.assertEqual(icons["poison"].stacks, 1)
        rendered = view.render("hero")
        self.assertIn("poisonx1", rendered)
        self.assertIn("hp=92", rendered)                          # 两跳各 -4 后解一层

    def test_suppressed_icon_flag(self):
        eng = self.make_engine()
        eng.apply_effect("burn", "hero", "imp")
        eng.apply_effect("freeze", "hero", "mage")
        view = IconView(eng)
        icons = {i.name: i for i in view.icons("hero")}
        self.assertTrue(icons["burn"].suppressed[0])
        self.assertFalse(icons["freeze"].suppressed[0])


class TestLifeDeltaAccounting(BaseCase):
    def test_hp_events_reconstruct_every_change(self):
        eng = self.make_engine()
        eng.apply_effect("poison", "hero", "s1", duration=4)     # 仅 t3 一跳
        eng.advance(4)
        deltas = eng.hp_deltas("hero")
        self.assertEqual([(d["before"], d["after"], d["delta"]) for d in deltas],
                         [(100, 96, -4)])
        # 生命下限钳制。
        eng.apply_effect("bleed", "hero", "s1", duration=5)      # period4 -> t4? 仅 1 跳 -5
        eng.hp["hero"] = 3
        eng.advance(4)
        self.assertEqual(eng.hp["hero"], 0)


if __name__ == "__main__":
    unittest.main()
