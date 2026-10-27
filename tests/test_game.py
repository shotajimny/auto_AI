import copy
import unittest

from hexa_client.game import (initial_state, move_cost, neighbor,
                              normalize_setting, normalize_state, simulate)


def fixture(cells=None, steps=8, starts=None, spots=None, fuel=12, days=2):
    cells = cells or [[0, 0, 0, 0], [0, 0, 0, 0]]
    return normalize_setting({"problem": {
        "width": len(cells[0]), "height": len(cells), "cells": cells,
        "agentStarts": starts if starts is not None else [0],
        "spots": spots or [], "fuelLimits": fuel,
        "daySteps": [steps] * days, "daySeconds": [60] * days,
        "busyThreshold": 2, "jammedThreshold": 4,
    }, "teams": [{"token": "secret"}]})


class CoordinatesTests(unittest.TestCase):
    def test_even_rows_shift_right(self):
        self.assertEqual([neighbor(1, d, 4, 3) for d in range(6)],
                         [None, None, 2, 6, 5, 0])
        self.assertEqual([neighbor(5, d, 4, 3) for d in range(6)],
                         [0, 1, 6, 9, 8, 4])

    def test_edges_never_wrap(self):
        self.assertIsNone(neighbor(3, 2, 4, 3))
        self.assertIsNone(neighbor(4, 5, 4, 3))
        self.assertIsNone(neighbor(8, 4, 4, 3))

    def test_adjacency_is_symmetric(self):
        for pos in range(24):
            for direction in range(6):
                target = neighbor(pos, direction, 6, 4)
                if target is not None:
                    self.assertEqual(neighbor(target, (direction + 3) % 6, 6, 4), pos)


class SettingsTests(unittest.TestCase):
    def test_server_file_converts_without_leaking_tokens(self):
        setting = fixture()
        self.assertEqual(setting["agents"], [0])
        self.assertEqual(setting["players"], 1)
        self.assertNotIn("secret", str(setting))
        self.assertEqual(normalize_setting(setting), setting)

    def test_early_scalar_seconds_is_normalized(self):
        setting = fixture()
        setting["daySeconds"] = 5
        self.assertEqual(normalize_setting(setting)["daySeconds"], [5, 5])

    def test_bad_dimensions_and_pond_positions_fail(self):
        for patch in ({"width": 4, "height": 2, "cells": [[0]]},
                      {"width": 1, "height": 1, "cells": [[3]]}):
            setting = fixture()
            setting["map"] = patch
            with self.assertRaises(ValueError):
                normalize_setting(setting)

    def test_bad_state_and_missing_later_traffic_fail(self):
        setting = fixture(cells=[[1, 0, 0]])
        state = initial_state(setting)
        state["day"] = 1
        state["traffics"] = []
        with self.assertRaises(ValueError):
            normalize_state(setting, state)

    def test_boolean_agent_kind_is_not_an_integer_kind(self):
        with self.assertRaises(ValueError):
            initial_state(fixture(), [True])

    def test_thresholds_follow_supplied_values_without_extra_order_constraint(self):
        setting = fixture()
        setting["busyThreshold"] = setting["jammedThreshold"] = 2
        self.assertEqual(normalize_setting(setting)["busyThreshold"], 2)


class SimulationTests(unittest.TestCase):
    def test_costs_use_departure_not_destination(self):
        setting = fixture(cells=[[0, 2, 1]], steps=6)
        state = initial_state(setting)
        report = simulate(setting, state, [[2, 2, -1]])
        self.assertTrue(report["valid"], report["errors"])
        self.assertEqual(report["agents"][0]["pos"], 2)
        self.assertEqual(report["agents"][0]["fuel"], 9)
        self.assertEqual(report["timeline"][1]["agents"][0]["pos"], 0)
        self.assertEqual(report["timeline"][2]["agents"][0]["pos"], 1)
        self.assertEqual(report["timeline"][4]["agents"][0]["fuel"], 11)
        self.assertEqual(report["timeline"][5]["agents"][0]["fuel"], 9)

    def test_all_three_road_statuses(self):
        setting = fixture(cells=[[1, 0]], steps=4)
        state = initial_state(setting)
        for status, duration in enumerate((1, 2, 4)):
            state["traffics"] = [{"pos": 0, "status": status}]
            self.assertEqual(move_cost(setting, state, 0), (duration, 2))
            row = [2] + ([-(4 - duration)] if duration < 4 else [])
            report = simulate(setting, state, [row])
            self.assertTrue(report["valid"], report["errors"])
            self.assertEqual(report["trafficCounts"], [{"pos": 0, "count": duration - 1}])

    def test_wait_is_free_and_acquires_once_per_day(self):
        setting = fixture(spots=[{"pos": 0, "brand": 7, "stocks": 2}])
        state = initial_state(setting)
        state["agents"][0]["fuel"] = 0
        report = simulate(setting, state, [[-8]])
        self.assertTrue(report["valid"])
        self.assertEqual(report["score"]["bowls"], 1)
        self.assertEqual(report["collections"][0]["step"], 1)
        self.assertEqual(report["timeline"][0]["collections"], [])
        self.assertEqual(report["agents"][0]["fuel"], 0)
        state["day"] = 1
        self.assertEqual(simulate(setting, state, [[-8]])["score"]["bowls"], 1)

    def test_collects_origin_while_slow_move_is_pending(self):
        setting = fixture(steps=2, spots=[{"pos": 0, "brand": 7, "stocks": 1}])
        report = simulate(setting, initial_state(setting), [[2]])
        self.assertEqual(report["collections"], [{"step": 1, "agent": 0, "pos": 0, "brand": 7}])

    def test_stock_is_team_local_and_lower_index_wins_simultaneously(self):
        setting = fixture(steps=2, starts=[0, 2],
                          spots=[{"pos": 1, "brand": 3, "stocks": 1}])
        state = initial_state(setting)
        state["others"] = [{"id": 5, "agents": [{"kind": 0, "pos": 1, "fuel": 12}]}]
        report = simulate(setting, state, [[2], [5]])
        self.assertEqual(report["score"]["bowls"], 1)
        self.assertEqual(report["collections"][0]["agent"], 0)
        self.assertEqual(report["stocks"], [{"pos": 1, "stocks": 0}])

    def test_exact_step_total_is_required_for_every_agent(self):
        setting = fixture(starts=[0, 3])
        for actions in ([[2], [-8]], [[-9], [-8]], [[-8]], [[-8], [2, -7]]):
            report = simulate(setting, initial_state(setting), actions)
            self.assertFalse(report["valid"])
            self.assertEqual(report["collections"], [])
            self.assertEqual(report["agents"], initial_state(setting)["agents"])

    def test_bad_commands_and_boundaries_reject_whole_answer(self):
        setting = fixture(cells=[[0, 3, 0]], steps=2)
        for row in ([2], [5], [6], [True], [1.0], ["2"], [None], [-10000000000]):
            report = simulate(setting, initial_state(setting), [row])
            self.assertFalse(report["valid"], row)

    def test_supply_consumes_no_fuel_and_never_collects(self):
        setting = fixture(steps=2, fuel=1,
                          spots=[{"pos": 1, "brand": 4, "stocks": 1}])
        state = initial_state(setting, [1])
        state["agents"][0]["fuel"] = 0
        report = simulate(setting, state, [[2]])
        self.assertTrue(report["valid"])
        self.assertEqual(report["agents"][0]["fuel"], 0)
        self.assertEqual(report["score"]["bowls"], 0)

    def test_same_final_step_refuels_after_all_arrivals(self):
        setting = fixture(steps=2, starts=[0, 2], fuel=5)
        state = initial_state(setting, [0, 1])
        state["agents"][0]["fuel"] = 1
        report = simulate(setting, state, [[2], [5]])
        self.assertTrue(report["valid"])
        self.assertEqual(report["agents"][0]["fuel"], 5)

    def test_mid_transit_refill_can_fund_previously_unaffordable_move(self):
        # Regression established against the supplied official server: patrol
        # leaves mountain1 (3 steps/2 fuel) with only1; supply arrives origin
        # at t2, refills it, then its move completes and consumes2 at t3.
        setting = fixture(cells=[[0, 2, 0]], starts=[0, 2], steps=3, fuel=2)
        state = initial_state(setting, [0, 1])
        state["agents"][0].update(pos=1, fuel=1)
        report = simulate(setting, state, [[2], [5, -1]])
        self.assertTrue(report["valid"], report["errors"])
        self.assertEqual(report["timeline"][1]["agents"][0]["fuel"], 1)
        self.assertEqual(report["timeline"][2]["agents"][0]["fuel"], 2)
        self.assertEqual(report["agents"][0], {"kind": 0, "pos": 2, "fuel": 0})

    def test_same_tick_destination_refill_cannot_pay_insufficient_fuel(self):
        setting = fixture(cells=[[0, 2, 0]], starts=[0, 2], steps=3, fuel=2)
        state = initial_state(setting, [0, 1])
        state["agents"][0].update(pos=1, fuel=1)
        report = simulate(setting, state, [[2], [-3]])
        self.assertFalse(report["valid"])
        self.assertIn("ステップ 3", report["errors"][0])

    def test_step_zero_does_not_refill_but_intermediate_wait_does(self):
        setting = fixture(starts=[0, 2], steps=2, fuel=3)
        state = initial_state(setting, [0, 1])
        state["agents"][0]["fuel"] = 0
        state["agents"][1]["pos"] = 0
        report = simulate(setting, state, [[2], [-2]])
        self.assertTrue(report["valid"])
        self.assertEqual(report["timeline"][0]["agents"][0]["fuel"], 0)
        self.assertEqual(report["timeline"][1]["agents"][0]["fuel"], 3)
        self.assertEqual(report["agents"][0]["fuel"], 2)

    def test_full_official_supplement_timing_example(self):
        setting = fixture(cells=[[1, 1, 0, 0, 0, 0, 0, 0]],
                          starts=list(range(8)), steps=6, fuel=3,
                          spots=[{"pos": 2, "brand": 8, "stocks": 4}])
        state = initial_state(setting, [0] * 7 + [1])
        for agent, pos in zip(state["agents"], [0, 0, 0, 0, 2, 3, 3, 2]):
            agent["pos"] = pos
        state["traffics"] = [{"pos": 0, "status": 0}, {"pos": 1, "status": 1}]
        actions = [[2, 2, 2, -1], [-1, 2, 2, 2], [-2, 2, 2, -1],
                   [-3, 2, -2], [5, 5, -2], [5, 5, -2], [-2, 5, 5], [5, 5, -2]]
        report = simulate(setting, state, actions)
        self.assertTrue(report["valid"], report["errors"])
        self.assertEqual([(a["pos"], a["fuel"]) for a in report["agents"]],
                         [(3, 0), (3, 0), (2, 1), (1, 1), (0, 3), (1, 1), (1, 1), (0, 3)])
        self.assertEqual([(e["step"], e["agent"]) for e in report["collections"]],
                         [(1, 4), (2, 5), (3, 0), (4, 1)])
        self.assertEqual(report["trafficCounts"], [{"pos": 0, "count": 12}, {"pos": 1, "count": 17}])

    def test_score_distinguishes_match_and_day_diversity(self):
        setting = fixture(steps=4, spots=[{"pos": 0, "brand": 2, "stocks": 1},
                                                     {"pos": 1, "brand": 3, "stocks": 1}])
        report = simulate(setting, initial_state(setting), [[2, -2]], known_brands=[2, 5])
        self.assertEqual(report["score"], {"brands": [2, 3, 5], "brandCount": 3,
                                         "dailyBrands": [2, 3], "dailyBrandCount": 2,
                                         "newBrands": [3], "bowls": 2})

    def test_inputs_are_never_mutated_even_on_invalid_answer(self):
        setting = fixture()
        state = initial_state(setting)
        original = copy.deepcopy((setting, state))
        simulate(setting, state, [[2, -6]])
        simulate(setting, state, [[5, -6]])
        self.assertEqual((setting, state), original)
        self.assertEqual(simulate(setting, state, [[-8]], include_trace=False)["timeline"], [])


if __name__ == "__main__":
    unittest.main()
