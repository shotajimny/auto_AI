"""Deterministic, fuel-aware baseline planner; no claim of optimality.

Choose uncollected brands before today's new brands, then extra bowls.
Supply vehicles move toward useful spot hubs and wait there. Patrol paths
use Pareto labels (arrival time, remaining fuel), including planned supply
occupancy and in-transit refueling. Every returned answer is independently
checked by the rules engine and is padded to the exact day's duration.
"""

import heapq
import time as clock
from collections import defaultdict

from .game import (PATROL, SUPPLY, _costs, initial_state, neighbor,
                   normalize_setting, normalize_state, simulate)


def _graph(setting, state):
    width, height = setting["map"]["width"], setting["map"]["height"]
    costs = _costs(setting, state)
    graph = [[] for _ in costs]
    for pos, (duration, _) in enumerate(costs):
        if not duration:
            continue
        for direction in range(6):
            target = neighbor(pos, direction, width, height)
            if target is not None and costs[target][0]:
                graph[pos].append((direction, target))
    return costs, graph


def _shortest(start, graph, costs):
    distances, previous, queue = {start: 0}, {}, [(0, start)]
    while queue:
        elapsed, pos = heapq.heappop(queue)
        if elapsed != distances[pos]:
            continue
        for direction, target in graph[pos]:
            candidate = elapsed + costs[pos][0]
            if candidate < distances.get(target, float("inf")):
                distances[target] = candidate
                previous[target] = (pos, direction)
                heapq.heappush(queue, (candidate, target))
    return distances, previous


def _route(previous, start, target):
    result = []
    while target != start:
        origin, direction = previous[target]
        result.append(direction)
        target = origin
    return list(reversed(result))


def choose_agent_kinds(setting):
    """Suggest one central supply vehicle, with all remaining agents patrols.

    This is an explicit suggestion; callers should submit it only in the
    pre-game selection phase. It never changes existing kinds automatically.
    """
    setting = normalize_setting(setting)
    kinds = [PATROL] * len(setting["agents"])
    if len(kinds) < 3 or not setting["spots"]:
        return kinds
    costs, graph = _graph(setting, initial_state(setting))
    centrality = []
    for index, start in enumerate(setting["agents"]):
        distances, _ = _shortest(start, graph, costs)
        centrality.append((sum(distances.get(s["pos"], 100000)
                               for s in setting["spots"]), index))
    kinds[min(centrality)[1]] = SUPPLY
    return kinds


def _pad(actions, elapsed, total):
    if elapsed < total:
        actions.append(elapsed - total)
    return actions


def _supply_plans(setting, state, costs, graph, total):
    plans = [[-total] for _ in state["agents"]]
    patrols = [a for a in state["agents"] if a["kind"] == PATROL]
    # Distances FROM each candidate agent are a useful inexpensive hub heuristic.
    patrol_distances = [_shortest(a["pos"], graph, costs)[0] for a in patrols]
    chosen = set()
    for index, agent in enumerate(state["agents"]):
        if agent["kind"] != SUPPLY:
            continue
        distances, previous = _shortest(agent["pos"], graph, costs)
        candidates = [s["pos"] for s in setting["spots"]
                      if distances.get(s["pos"], total + 1) < total]
        if not patrols or not candidates:
            continue
        hub = min(candidates, key=lambda pos: (
            pos in chosen,
            sum(d.get(pos, total * 8) for d in patrol_distances)
            + distances[pos] * 0.6,
            distances[pos], pos))
        chosen.add(hub)
        plans[index] = _pad(_route(previous, agent["pos"], hub), distances[hub], total)
    # Patrol wait plans are valid even at zero fuel. This also supplies exact
    # own-team positions throughout slow departures of the supply vehicles.
    report = simulate(setting, state, plans)
    positions = [set(a["pos"] for a in frame["agents"] if a["kind"] == SUPPLY)
                 for frame in report["timeline"]]
    return plans, positions


def _transition(pos, target, start, fuel, duration, needed, supply, limit):
    """Fuel after a move, or None. Refills before arrival can fund the move."""
    end = start + duration
    if any(pos in supply[step] for step in range(start + 1, end)):
        fuel = limit
    if fuel < needed:
        return None
    fuel -= needed
    return limit if target in supply[end] else fuel


def _paths_to_spots(start, start_time, fuel, targets, costs, graph, supply,
                    limit, total, deadline=None, cancelled=None):
    """Return feasible shortest-time paths to requested cells.

    An earlier label with at least as much fuel dominates a later label:
    waiting is always possible. Useful waits are generated at the next
    refueling opportunity; redundant no-refill waits need not be expanded.
    A bounded search keeps the baseline responsive even on large maps.
    """
    start_node = (start_time, start, fuel)
    queue = [start_node]
    labels = defaultdict(list)
    labels[start].append((start_time, fuel))
    previous = {}
    found = {}
    expansions = 0

    def add(origin, node, command):
        time, pos, remaining = node
        if any(t <= time and f >= remaining for t, f in labels[pos]):
            return
        labels[pos] = [(t, f) for t, f in labels[pos]
                       if not (time <= t and remaining >= f)]
        labels[pos].append((time, remaining))
        previous[node] = (origin, command)
        heapq.heappush(queue, node)

    while queue and expansions < 16000:
        if expansions % 64 == 0 and ((deadline is not None and clock.monotonic() >= deadline)
                                    or (cancelled and cancelled())):
            break
        node = heapq.heappop(queue)
        time, pos, remaining = node
        if (time, remaining) not in labels[pos]:
            continue
        expansions += 1
        if pos in targets and time > start_time and pos not in found:
            commands, cursor = [], node
            while cursor != start_node:
                cursor, command = previous[cursor]
                commands.append(command)
            found[pos] = (time, remaining, list(reversed(commands)))
            if len(found) == len(targets):
                break
        if time >= total:
            continue
        duration, needed = costs[pos]
        if time + duration <= total:
            for direction, target in graph[pos]:
                after = _transition(pos, target, time, remaining, duration,
                                    needed, supply, limit)
                if after is not None:
                    add(node, (time + duration, target, after), direction)
        if remaining < limit:
            for future in range(time + 1, total + 1):
                if pos in supply[future]:
                    add(node, (future, pos, limit), time - future)
                    break
    return found


def plan_actions(setting, state, known_brands=(), *, order=None, strategy="novel",
                 deadline=None, cancelled=None):
    """Return the official list-of-agent-action-lists JSON-compatible shape."""
    setting = normalize_setting(setting)
    state = normalize_state(setting, state)
    total = setting["daySteps"][state["day"]]
    limit = setting["fuelLimits"]
    costs, graph = _graph(setting, state)
    plans, supply = _supply_plans(setting, state, costs, graph, total)
    spots = {s["pos"]: s for s in setting["spots"]}
    stock = {s["pos"]: s["stocks"] for s in setting["spots"]}
    match_brands, daily_brands = set(known_brands), set()

    indices = list(range(len(state["agents"]))) if order is None else order
    for index in indices:
        agent = state["agents"][index]
        if agent["kind"] == SUPPLY:
            continue
        if (deadline is not None and clock.monotonic() >= deadline) or (cancelled and cancelled()):
            break
        pos, fuel, time, commands = agent["pos"], agent["fuel"], 0, []
        visited = set()

        def collect(position):
            if position in spots and position not in visited and stock[position] > 0:
                visited.add(position)
                stock[position] -= 1
                brand = spots[position]["brand"]
                match_brands.add(brand)
                daily_brands.add(brand)

        def apply(command):
            nonlocal pos, fuel, time
            commands.append(command)
            if command < 0:
                for tick in range(time + 1, time - command + 1):
                    collect(pos)
                    if pos in supply[tick]:
                        fuel = limit
                time -= command
                return
            target = next(p for d, p in graph[pos] if d == command)
            duration, needed = costs[pos]
            for tick in range(time + 1, time + duration):
                collect(pos)
                if pos in supply[tick]:
                    fuel = limit
            fuel -= needed
            time += duration
            pos = target
            collect(pos)
            if pos in supply[time]:
                fuel = limit

        # The zero-th step has no reflection: wait one real step when already
        # on a shop, including when an empty patrol needs an initial refill.
        if (pos in spots and stock[pos] > 0) or (fuel < limit and pos in supply[1]):
            apply(-1)
        while time < total:
            if (deadline is not None and clock.monotonic() >= deadline) or (cancelled and cancelled()):
                break
            targets = {p for p in spots if p not in visited and stock[p] > 0}
            if not targets:
                break
            candidates = _paths_to_spots(pos, time, fuel, targets, costs, graph,
                                        supply, limit, total, deadline, cancelled)
            if not candidates:
                break
            def priority(item):
                target, (arrival, remaining, route) = item
                brand = spots[target]["brand"]
                if strategy == "near":
                    return (-arrival, brand not in match_brands, brand not in daily_brands,
                            remaining, -target)
                if strategy == "daily":
                    return (brand not in daily_brands, -arrival, brand not in match_brands,
                            remaining, -target)
                return (brand not in match_brands, brand not in daily_brands,
                        -arrival, remaining, -target)
            target, (_, _, route) = max(candidates.items(), key=priority)
            for command in route:
                apply(command)
            if pos != target:
                break  # Defensive; the independent simulator will reject it.
        # End a depleted route at a supply rendezvous when time permits. This
        # uses otherwise idle time and keeps later days from becoming immobile.
        if (time < total and fuel < limit and any(supply[time + 1:])
                and (deadline is None or clock.monotonic() < deadline)
                and not (cancelled and cancelled())):
            rendezvous = set().union(*supply[time + 1:])
            candidates = _paths_to_spots(pos, time, fuel, rendezvous, costs, graph,
                                        supply, limit, total, deadline, cancelled)
            refill_routes = [(arrival, target, route) for target, (arrival, _, route)
                             in candidates.items()
                             if any(target in supply[t] for t in range(arrival, total + 1))]
            if refill_routes:
                _, _, route = min(refill_routes)
                for command in route:
                    apply(command)
        plans[index] = _pad(commands, time, total)

    # Stock contention can change rewards but cannot invalidate movement.
    # Never return a partly valid team answer if a future planner change breaks
    # its assumptions. Waiting is always a legal, exactly sized safe answer.
    report = simulate(setting, state, plans, known_brands, include_trace=False)
    if not report["valid"]:
        return [[-total] for _ in state["agents"]]
    return plans


def solve(setting, state, known_brands=(), time_budget=0.8, cancelled=None):
    """Compare independently simulated candidates within a wall-clock budget.

    Official score priorities dominate candidate selection. The final fuel
    reserve breaks otherwise equal scores to keep later days mobile. This
    bounded multi-start heuristic does not promise the global optimum.
    """
    setting = normalize_setting(setting)
    state = normalize_state(setting, state)
    if isinstance(time_budget, bool) or not isinstance(time_budget, (int, float)):
        raise ValueError("探索時間は秒数で指定してください。")
    if not 0 <= time_budget <= 5:
        raise ValueError("探索時間は0〜5秒で指定してください。")
    started = clock.monotonic()
    deadline = started + time_budget
    total = setting["daySteps"][state["day"]]
    best_actions = [[-total] for _ in state["agents"]]
    best_report = simulate(setting, state, best_actions, known_brands, include_trace=False)

    def rank(report):
        score = report["score"]
        return (score["brandCount"], score["dailyBrandCount"], score["bowls"],
                sum(a["fuel"] for a in report["agents"] if a["kind"] == PATROL))

    best_rank = rank(best_report)
    count = len(state["agents"])
    orders = [list(range(count)), list(reversed(range(count)))]
    orders.extend(list(range(i, count)) + list(range(i)) for i in range(1, count))
    candidates, seen = 0, set()
    for strategy in ("novel", "near", "daily"):
        for order in orders:
            if clock.monotonic() >= deadline or (cancelled and cancelled()):
                break
            signature = (strategy, tuple(order))
            if signature in seen:
                continue
            seen.add(signature)
            actions = plan_actions(setting, state, known_brands, order=order,
                                   strategy=strategy, deadline=deadline, cancelled=cancelled)
            report = simulate(setting, state, actions, known_brands, include_trace=False)
            candidates += 1
            if report["valid"] and rank(report) > best_rank:
                best_actions, best_report, best_rank = actions, report, rank(report)
    return {"actions": best_actions, "validation": best_report,
            "candidates": candidates, "elapsedMs": round((clock.monotonic() - started) * 1000),
            "strategy": "車両の割り当て順と訪問優先度を変えて比較する時間制限付き探索"}


def plan(setting, state, known_brands=()):
    actions = plan_actions(setting, state, known_brands)
    return {"actions": actions,
            "simulation": simulate(setting, state, actions, known_brands),
            "strategy": "未収集系列を優先する燃料制約付き最短路の貪欲法",
            "warnings": ["最適解を保証しません。補給車はスポットの近くで待機する基本戦略です。"]}
