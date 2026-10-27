"""A synthetic, redistributable practice map; never contains real tokens."""


def demo_config():
    width, height = 12, 10
    cells = [[0 for _ in range(width)] for _ in range(height)]
    for y in range(height):
        for x in range(width):
            if y in (2, 6) or x in (3, 8):
                cells[y][x] = 1
            elif (x, y) in ((5, 3), (6, 3), (5, 4), (6, 4), (1, 7), (2, 7)):
                cells[y][x] = 3
            elif (x + 2 * y) % 11 == 0:
                cells[y][x] = 2
    spots = [{"brand": brand, "pos": y * width + x, "stocks": stocks}
             for x, y, brand, stocks in [
                 (2, 1, 0, 2), (7, 1, 1, 2), (10, 2, 2, 3),
                 (1, 4, 3, 2), (8, 4, 4, 1), (3, 6, 0, 3),
                 (10, 7, 1, 2), (5, 8, 5, 2), (8, 9, 2, 2)]]
    for spot in spots:
        cells[spot["pos"] // width][spot["pos"] % width] = 0
    return {"problem": {
        "width": width, "height": height, "cells": cells,
        "spots": spots, "agentStarts": [13, 16, 19, 22],
        "fuelLimits": 36, "daySteps": [36, 48, 60],
        "daySeconds": [60, 60, 60], "busyThreshold": 2, "jammedThreshold": 4,
    }, "teams": [{"name": "練習チーム", "token": "demo-only"}]}
