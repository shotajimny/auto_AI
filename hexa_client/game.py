"""Pure rules and validation for the 37th Kosen Procon's Hexa Udon.

Rules: 香川大会 募集要項 pp. 8–12; 問い合わせ回答1 Q1, Q6–17;
問い合わせ回答2 Q22, Q25–27, Q47–61; フォーマット pp. 1–3.
Coordinates use even rows shifted right. Time/fuel costs belong to the
DEPARTURE cell. A move completes after its duration; until then an agent
occupies its original cell and may collect/refuel there. Fuel is checked
and consumed on completion, before simultaneous arrivals and refueling.
The latter also agrees with probes against the supplied official server.

This module deliberately has no HTTP, filesystem, clock, or third-party
dependencies. It predicts the local team's collection, not an official
score or other teams' future traffic.
"""

from copy import deepcopy


PLAIN, ROAD, MOUNTAIN, POND = range(4)
PATROL, SUPPLY = range(2)
DIRECTION_NAMES = ("左上", "右上", "右", "右下", "左下", "左")


def _int(value, label, minimum=None, maximum=None):
    if type(value) is not int:
        raise ValueError(f"{label} は整数で指定してください。")
    if minimum is not None and value < minimum:
        raise ValueError(f"{label} は {minimum} 以上で指定してください。")
    if maximum is not None and value > maximum:
        raise ValueError(f"{label} は {maximum} 以下で指定してください。")
    return value


def _list(value, label):
    if not isinstance(value, list):
        raise ValueError(f"{label} は配列で指定してください。")
    return value


def normalize_setting(raw):
    """Accept official /setting JSON or a supplied {problem, teams} file.

    Small maps and fewer than three agents are accepted for reproducible
    tests/practice. Terrain, positions, dimensions and schedules are still
    checked. No supplied authentication tokens are retained in the result.
    """
    if not isinstance(raw, dict):
        raise ValueError("試合設定は JSON オブジェクトで指定してください。")
    problem = raw.get("problem", raw)
    if not isinstance(problem, dict):
        raise ValueError("problem はオブジェクトで指定してください。")
    board = problem.get("map", problem)
    if not isinstance(board, dict):
        raise ValueError("map はオブジェクトで指定してください。")
    width = _int(board.get("width"), "map.width", 1, 32)
    height = _int(board.get("height"), "map.height", 1, 32)
    cells = _list(board.get("cells"), "map.cells")
    if len(cells) != height:
        raise ValueError("map.cells の行数が height と一致しません。")
    for row in cells:
        if len(_list(row, "map.cells の行")) != width:
            raise ValueError("map.cells の列数が width と一致しません。")
        for cell in row:
            _int(cell, "セルの地形", 0, 3)
    steps = _list(problem.get("daySteps"), "daySteps")
    if not steps or len(steps) > 100:
        raise ValueError("daySteps は 1〜100 日分で指定してください。")
    steps = [_int(n, "daySteps", 1, 10000) for n in steps]
    seconds = problem.get("daySeconds", [60] * len(steps))
    # The early format PDF has a scalar sample; the final OpenAPI uses an array.
    if type(seconds) is int:
        seconds = [seconds] * len(steps)
    seconds = _list(seconds, "daySeconds")
    if len(seconds) != len(steps):
        raise ValueError("daySeconds と daySteps の日数が一致しません。")
    seconds = [_int(n, "daySeconds", 1) for n in seconds]
    agents = _list(problem.get("agents", problem.get("agentStarts")), "agents")
    if not 1 <= len(agents) <= 8:
        raise ValueError("agents は 1〜8 台分で指定してください。")
    for pos in agents:
        _int(pos, "エージェント初期位置", 0, width * height - 1)
        if cells[pos // width][pos % width] == POND:
            raise ValueError("エージェント初期位置に池は指定できません。")
    if len(set(agents)) != len(agents):
        raise ValueError("エージェント初期位置が重複しています。")
    spots = _list(problem.get("spots", []), "spots")
    clean_spots, occupied = [], set()
    for spot in spots:
        if not isinstance(spot, dict):
            raise ValueError("spots の各要素はオブジェクトで指定してください。")
        pos = _int(spot.get("pos"), "スポット位置", 0, width * height - 1)
        if pos in occupied or cells[pos // width][pos % width] == POND:
            raise ValueError("スポット位置が重複しているか、池が指定されています。")
        occupied.add(pos)
        clean_spots.append({"pos": pos,
                            "brand": _int(spot.get("brand"), "系列"),
                            "stocks": _int(spot.get("stocks"), "最大在庫", 1)})
    teams = raw.get("teams", [])
    players = problem.get("players", len(teams) or 1)
    setting = {
        "startsAt": _int(problem.get("startsAt", 0), "startsAt", 0),
        "daySeconds": seconds, "daySteps": steps,
        "map": {"height": height, "width": width, "cells": deepcopy(cells)},
        "spots": clean_spots, "agents": list(agents),
        "fuelLimits": _int(problem.get("fuelLimits"), "fuelLimits", 1),
        "players": _int(players, "players", 1),
        "busyThreshold": _int(problem.get("busyThreshold", 2), "busyThreshold", 1),
        "jammedThreshold": _int(problem.get("jammedThreshold", 4), "jammedThreshold", 1),
    }
    return setting


def neighbor(pos, direction, width, height):
    """Return a neighboring row-major cell, or None at the map boundary."""
    if type(direction) is not int or direction not in range(6):
        raise ValueError("移動方向は 0〜5 で指定してください。")
    if type(pos) is not int or not 0 <= pos < width * height:
        raise ValueError("セル位置がマップ外です。")
    y, x = divmod(pos, width)
    right_shift = 1 if y % 2 == 0 else 0
    dx, dy = ((right_shift - 1, -1), (right_shift, -1), (1, 0),
              (right_shift, 1), (right_shift - 1, 1), (-1, 0))[direction]
    x, y = x + dx, y + dy
    return y * width + x if 0 <= x < width and 0 <= y < height else None


def initial_state(setting, kinds=None):
    setting = normalize_setting(setting)
    kinds = [PATROL] * len(setting["agents"]) if kinds is None else kinds
    if (not isinstance(kinds, list) or len(kinds) != len(setting["agents"])
            or any(type(k) is not int or k not in (PATROL, SUPPLY) for k in kinds)):
        raise ValueError("種別はエージェント数と同じ長さの 0 / 1 の配列で指定してください。")
    return {"day": 0, "endsAt": setting["startsAt"] + setting["daySeconds"][0],
            "agents": [{"kind": k, "pos": p, "fuel": setting["fuelLimits"]}
                       for p, k in zip(setting["agents"], kinds)],
            "others": [], "traffics": [{"pos": y * setting["map"]["width"] + x,
                                          "status": 0}
                                         for y, row in enumerate(setting["map"]["cells"])
                                         for x, cell in enumerate(row) if cell == ROAD]}


def normalize_state(setting, state):
    """Validate a day-start state while preserving no external extra keys."""
    if not isinstance(state, dict):
        raise ValueError("試合状態は JSON オブジェクトで指定してください。")
    board = setting["map"]
    width, height = board["width"], board["height"]
    day = _int(state.get("day"), "day", 0, len(setting["daySteps"]) - 1)
    def clean_agents(items, label):
        result = []
        for agent in _list(items, label):
            if not isinstance(agent, dict):
                raise ValueError(f"{label} の各要素はオブジェクトで指定してください。")
            kind = _int(agent.get("kind"), "kind", 0, 1)
            pos = _int(agent.get("pos"), "pos", 0, width * height - 1)
            fuel = _int(agent.get("fuel"), "fuel", 0)
            if kind == PATROL and fuel > setting["fuelLimits"]:
                raise ValueError("巡回車の fuel が燃料上限を超えています。")
            if board["cells"][pos // width][pos % width] == POND:
                raise ValueError("エージェントが池に配置されています。")
            result.append({"kind": kind, "pos": pos, "fuel": fuel})
        return result
    agents = clean_agents(state.get("agents"), "agents")
    if len(agents) != len(setting["agents"]):
        raise ValueError("試合状態のエージェント数が試合設定と一致しません。")
    traffics, seen = [], set()
    for traffic in _list(state.get("traffics", []), "traffics"):
        if not isinstance(traffic, dict):
            raise ValueError("traffics の各要素はオブジェクトで指定してください。")
        pos = _int(traffic.get("pos"), "交通情報の pos", 0, width * height - 1)
        if pos in seen or board["cells"][pos // width][pos % width] != ROAD:
            raise ValueError("交通情報の位置が重複しているか、道路以外です。")
        seen.add(pos)
        traffics.append({"pos": pos, "status": _int(traffic.get("status"), "status", 0, 2)})
    missing = [y * width + x for y, row in enumerate(board["cells"])
               for x, cell in enumerate(row) if cell == ROAD and y * width + x not in seen]
    if missing and day != 0:
        raise ValueError("当日の交通情報に一部の道路が含まれていません。状態を再取得してください。")
    traffics.extend({"pos": pos, "status": 0} for pos in missing)
    others = []
    for other in _list(state.get("others", []), "others"):
        if not isinstance(other, dict):
            raise ValueError("others の各要素はオブジェクトで指定してください。")
        others.append({"id": _int(other.get("id"), "他チームの id"),
                       "agents": clean_agents(other.get("agents"), "他チームの agents")})
    return {"day": day, "endsAt": _int(state.get("endsAt", 0), "endsAt", 0),
            "agents": agents, "others": others,
            "traffics": sorted(traffics, key=lambda item: item["pos"])}


def _costs(setting, state):
    traffic = {item["pos"]: item["status"] for item in state["traffics"]}
    width = setting["map"]["width"]
    costs = []
    for y, row in enumerate(setting["map"]["cells"]):
        for x, terrain in enumerate(row):
            if terrain == PLAIN:
                costs.append((2, 1))
            elif terrain == MOUNTAIN:
                costs.append((3, 2))
            elif terrain == ROAD:
                costs.append(((1, 2, 4)[traffic.get(y * width + x, 0)], 2))
            else:
                costs.append((0, 0))
    return costs


def move_cost(setting, state, pos):
    """Return (duration, fuel) for DEPARTING pos. Ponds are impassable."""
    _int(pos, "pos", 0, setting["map"]["width"] * setting["map"]["height"] - 1)
    duration, fuel = _costs(setting, state)[pos]
    if duration == 0:
        raise ValueError("池からは移動できません。")
    return duration, fuel


def simulate(setting, state, actions, known_brands=(), include_trace=True):
    """Validate the WHOLE team's answer and predict each reflection phase.

    An invalid answer has valid=False and no partial outcome is committed.
    All agents must explicitly cover exactly today's duration. Refueling
    uses only own supply agents; stocks are local to the team and reset
    each day. Traffic counts describe this team only.
    """
    setting = normalize_setting(setting)
    state = normalize_state(setting, state)
    total = setting["daySteps"][state["day"]]
    width, height = setting["map"]["width"], setting["map"]["height"]
    costs = _costs(setting, state)
    agents = deepcopy(state["agents"])
    known = {_int(b, "既知の系列") for b in known_brands}
    stocks = {s["pos"]: s["stocks"] for s in setting["spots"]}
    spots = {s["pos"]: s for s in setting["spots"]}
    events, errors = [{} for _ in agents], []

    def invalid():
        return {"valid": False, "errors": errors, "steps": total,
                "agents": deepcopy(state["agents"]), "collections": [],
                "stocks": [{"pos": p, "stocks": n} for p, n in stocks.items()],
                "score": {"brands": sorted(known), "brandCount": len(known),
                          "dailyBrands": [], "dailyBrandCount": 0,
                          "newBrands": [], "bowls": 0},
                "timeline": [], "trafficCounts": []}

    if not isinstance(actions, list) or len(actions) != len(agents):
        errors.append(f"全 {len(agents)} 台分の行動列を配列で指定してください。")
        return invalid()
    for index, (agent, row) in enumerate(zip(agents, actions)):
        if not isinstance(row, list):
            errors.append(f"車両 {index + 1}: 行動列は配列で指定してください。")
            continue
        elapsed, pos = 0, agent["pos"]
        for command_index, action in enumerate(row):
            prefix = f"車両 {index + 1} / 命令 {command_index + 1}"
            if type(action) is not int or action > 5:
                errors.append(f"{prefix}: 0〜5 の移動、または負の整数の待機を指定してください。")
                break
            if action < 0:
                elapsed -= action
            else:
                destination = neighbor(pos, action, width, height)
                if destination is None or costs[destination][0] == 0:
                    errors.append(f"{prefix}: {DIRECTION_NAMES[action]}の移動先が池またはマップ外です。")
                    break
                duration, fuel = costs[pos]
                elapsed += duration
                events[index][elapsed] = (destination, fuel)
                pos = destination
            if elapsed > total:
                errors.append(f"{prefix}: 合計 {elapsed} ステップで当日の {total} ステップを超えています。")
                break
        else:
            if elapsed != total:
                errors.append(f"車両 {index + 1}: 合計 {elapsed} ステップです。{total} ステップに合わせて末尾に待機を指定してください。")
    if errors:
        return invalid()

    collected = [set() for _ in agents]
    collections, timeline, daily = [], [], set()
    traffic_counts = {p: 0 for p, (duration, _) in enumerate(costs)
                      if duration and setting["map"]["cells"][p // width][p % width] == ROAD}
    if include_trace:
        timeline.append({"step": 0, "agents": deepcopy(agents), "collections": []})
    for step in range(1, total + 1):
        # Fuel consumption of ALL arrivals precedes ANY refueling this tick.
        for index, agent in enumerate(agents):
            if step in events[index] and agent["kind"] == PATROL:
                fuel = events[index][step][1]
                if agent["fuel"] < fuel:
                    errors.append(f"車両 {index + 1} / ステップ {step}: 移動完了時の燃料が不足しています（残量 {agent['fuel']} / 必要 {fuel}）。")
                else:
                    agent["fuel"] -= fuel
        if errors:
            return invalid()
        for index, agent in enumerate(agents):
            if step in events[index]:
                agent["pos"] = events[index][step][0]
        gained = []
        # List order is the official stock tie-break among simultaneous arrivals.
        for index, agent in enumerate(agents):
            pos = agent["pos"]
            if (agent["kind"] == PATROL and pos in spots
                    and pos not in collected[index] and stocks[pos] > 0):
                stocks[pos] -= 1
                collected[index].add(pos)
                brand = spots[pos]["brand"]
                daily.add(brand)
                event = {"step": step, "agent": index, "pos": pos, "brand": brand}
                collections.append(event)
                gained.append(event)
        supply_positions = {a["pos"] for a in agents if a["kind"] == SUPPLY}
        for agent in agents:
            if agent["kind"] == PATROL and agent["pos"] in supply_positions:
                agent["fuel"] = setting["fuelLimits"]
        # Q27's prose says pre-move occupancy, but the official supplement's
        # complete example and a supplied-server probe both use POST-move
        # positions. Follow the independently verified server behavior.
        for pos in (agent["pos"] for agent in agents):
            if pos in traffic_counts:
                traffic_counts[pos] += 1
        if include_trace:
            timeline.append({"step": step, "agents": deepcopy(agents), "collections": gained})
    return {"valid": True, "errors": [], "steps": total, "agents": agents,
            "collections": collections,
            "stocks": [{"pos": p, "stocks": n} for p, n in stocks.items()],
            "score": {"brands": sorted(known | daily), "brandCount": len(known | daily),
                      "dailyBrands": sorted(daily), "dailyBrandCount": len(daily),
                      "newBrands": sorted(daily - known), "bowls": len(collections)},
            "timeline": timeline,
            "trafficCounts": [{"pos": p, "count": n} for p, n in traffic_counts.items()]}
