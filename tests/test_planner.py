import copy
import random
import unittest

from hexa_client.demo import demo_config
from hexa_client.game import initial_state, normalize_setting, simulate
from hexa_client.planner import choose_agent_kinds, plan, plan_actions


def setting(cells, starts, spots, steps=20, fuel=12):
    return normalize_setting({"problem": {
        "width": len(cells[0]), "height": len(cells), "cells": cells,
        "agentStarts": starts, "spots": spots, "fuelLimits": fuel,
        "daySteps": [steps, steps], "daySeconds": [60, 60],
    }})


class PlannerTests(unittest.TestCase):
    def test_visits_multiple_brands(self):
        s = setting([[0] * 5], [0], [{"pos": 2, "brand": 3, "stocks": 1},
                                             {"pos": 4, "brand": 8, "stocks": 1}])
        state = initial_state(s)
        actions = plan_actions(s, state)
        report = simulate(s, state, actions)
        self.assertTrue(report["valid"], report["errors"])
        self.assertEqual(report["score"]["dailyBrands"], [3, 8])

    def test_global_novel_brand_precedes_nearer_known_brand(self):
        s = setting([[0] * 7], [3], [{"pos": 2, "brand": 1, "stocks": 1},
                                             {"pos": 6, "brand": 2, "stocks": 1}], steps=6)
        report = simulate(s, initial_state(s), plan_actions(s, initial_state(s), [1]), [1])
        self.assertEqual(report["score"]["newBrands"], [2])

    def test_empty_fuel_still_collects_current_spot(self):
        s = setting([[0, 0]], [0], [{"pos": 0, "brand": 9, "stocks": 1}], fuel=1)
        state = initial_state(s)
        state["agents"][0]["fuel"] = 0
        report = simulate(s, state, plan_actions(s, state))
        self.assertTrue(report["valid"])
        self.assertEqual(report["score"]["bowls"], 1)

    def test_no_spots_and_no_fuel_produce_safe_waits(self):
        s = setting([[0] * 3], [0, 2], [], fuel=1)
        state = initial_state(s)
        for a in state["agents"]:
            a["fuel"] = 0
        self.assertEqual(plan_actions(s, state), [[-20], [-20]])

    def test_obstacles_and_unreachable_spots(self):
        s = setting([[0, 3, 0], [0, 3, 0]], [0],
                    [{"pos": 2, "brand": 1, "stocks": 1}])
        report = simulate(s, initial_state(s), plan_actions(s, initial_state(s)))
        self.assertTrue(report["valid"])
        self.assertEqual(report["score"]["bowls"], 0)

    def test_multi_agent_supply_plan_and_input_purity(self):
        s = normalize_setting(demo_config())
        state = initial_state(s, [0, 0, 0, 1])
        before = copy.deepcopy((s, state))
        actions = plan_actions(s, state)
        self.assertEqual(actions, plan_actions(s, state))
        report = simulate(s, state, actions)
        self.assertTrue(report["valid"], report["errors"])
        self.assertEqual(report["score"]["dailyBrandCount"], 6)
        self.assertEqual((s, state), before)

    def test_all_supply_agents_remain_legal(self):
        s = normalize_setting(demo_config())
        state = initial_state(s, [1] * 4)
        report = simulate(s, state, plan_actions(s, state))
        self.assertTrue(report["valid"])
        self.assertEqual(report["score"]["bowls"], 0)

    def test_kind_suggestion_keeps_patrols_and_one_supply(self):
        s = normalize_setting(demo_config())
        kinds = choose_agent_kinds(s)
        self.assertEqual(kinds.count(1), 1)
        self.assertEqual(kinds.count(0), 3)

    def test_report_wrapper_uses_official_answer_shape(self):
        s = normalize_setting(demo_config())
        result = plan(s, initial_state(s))
        self.assertIsInstance(result["actions"], list)
        self.assertTrue(result["simulation"]["valid"])
        self.assertTrue(result["warnings"])

    def test_two_day_replanning_uses_fuel_and_positions(self):
        s = normalize_setting(demo_config())
        state = initial_state(s, [0, 0, 0, 1])
        first = simulate(s, state, plan_actions(s, state))
        state["day"], state["agents"] = 1, first["agents"]
        second = simulate(s, state, plan_actions(s, state, first["score"]["dailyBrands"]))
        self.assertTrue(second["valid"], second["errors"])

    def test_generated_heterogeneous_maps_always_return_valid_plans(self):
        rng = random.Random(37)
        for _ in range(12):
            cells = [[rng.choice([0, 0, 0, 1, 2, 3]) for _ in range(8)] for _ in range(8)]
            for p in [0, 7, 56, 63, 9, 22, 41, 54]:
                cells[p // 8][p % 8] = 0
            s = setting(cells, [0, 7, 56, 63],
                        [{"pos": p, "brand": i % 3, "stocks": 1 + i % 2}
                         for i, p in enumerate([9, 22, 41, 54])],
                        steps=rng.randint(16, 48), fuel=rng.randint(2, 20))
            state = initial_state(s, [0, 0, 0, 1])
            for traffic in state["traffics"]:
                traffic["status"] = rng.randrange(3)
            for agent in state["agents"]:
                if agent["kind"] == 0:
                    agent["fuel"] = rng.randint(0, s["fuelLimits"])
            report = simulate(s, state, plan_actions(s, state))
            self.assertTrue(report["valid"], report["errors"])


if __name__ == "__main__":
    unittest.main()
