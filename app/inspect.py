"""压印点位复核：把实测凸点坐标与排版理论点阵对齐复核。

复用 braille 模块的校验与排版得到理论凸点（坐标约定与 avoid 一致：
零基行格 (r, c) 中，点 1～3 为 (6c, 10r+2.5k)，点 4～6 为 (6c+2.5, 10r+2.5k)，
k=0～2，单位毫米，只计凸点）；把 0～400 行实测点整体平移 (dx, dy)
（各 −3～3 毫米，步长 0.1 毫米）后，与距离不超过 0.4 毫米的理论点配对：

- 理论点按 (行, 格, 点号) 升序依次处理；
- 每个理论点在尚未配对的实测点中取距离最近者，同距取输入行号较早者，
  配对后该实测点退出（一对一）；
- 全部位移遍历后，依次按
  匹配点数最多 → 匹配距离平方和最小 → dx²+dy² 最小 → dx 最小 → dy 最小
  择优；返回位移、缺失理论点（零基行/格/点号）与多余实测点（输入行号）。

全程以 0.1 毫米为单位做整数运算。实测点按 0.5 毫米网格分桶，
桶内以「下一个未配对项」链表删除，使每次匹配的候选扫描均摊线性。
"""

from __future__ import annotations

from .braille import MAX_LENGTH, ValidationError, typeset, validate

# 原文长度上限（压印复核视图）
INSPECT_MAX_LENGTH = 100

# 实测点行数上限
MAX_POINT_LINES = 400

# 位移范围 ±3 毫米、步长 0.1 毫米（以 0.1 毫米为单位的整数）
SHIFT_MIN = -30
SHIFT_MAX = 30

# 配对距离阈值 0.4 毫米（0.1 毫米整数），距离平方 ≤ 4² 即配对
MATCH_RADIUS = 4

# 理论凸点：(X, Y, 行, 格, 点号)，坐标单位 0.1 毫米
Dot = tuple[int, int, int, int, int]


# ---------------------------------------------------------------------------
# 理论点阵（与 avoid._dots_of 同口径）
# ---------------------------------------------------------------------------

def _theory_dots(result: dict) -> list[Dot]:
    """从排版结果提取全部理论凸点，按 (行, 格, 点号) 升序排列。"""
    dots: list[Dot] = []
    for r, line in enumerate(result["lines"]):
        for c, cell in enumerate(line["cells"]):
            for d in cell["dots"]:
                if d <= 3:
                    dots.append((60 * c, 100 * r + 25 * (d - 1), r, c, d))
                else:
                    dots.append((60 * c + 25, 100 * r + 25 * (d - 4), r, c, d))
    dots.sort(key=lambda p: (p[2], p[3], p[4]))
    return dots


# ---------------------------------------------------------------------------
# 实测坐标解析：每行 x,y（毫米，限一位小数）
# ---------------------------------------------------------------------------

def _decimal_error(s: str) -> tuple[int, str] | None:
    """校验一位小数字符串（可带负号、整数位不限位数），返回 (出错列, 原因)；合法返回 None。"""
    if s == "":
        return 0, "不能为空"
    i = 0
    if s[i] == "-":
        i += 1
    if i >= len(s) or not s[i].isdigit():
        return i, "（负号后）须以数字开头"
    while i < len(s) and s[i].isdigit():
        i += 1
    if i >= len(s) or s[i] != ".":
        return i, "小数点后须恰好一位数字"
    i += 1
    if i >= len(s) or not s[i].isdigit():
        return i, "小数点后须恰好一位数字"
    i += 1
    if i < len(s):
        return i, "一位小数之后不能再有字符"
    return None


def parse_points(raw: str) -> list[tuple[int, int, int]]:
    """解析实测坐标文本，返回 [(X, Y, 零基输入行号), ...]（坐标单位 0.1 毫米）。

    POSIX 惯例：结尾单个换行不产生空行；其余任何空行、非 "x,y" 形式、
    坐标不是一位小数的行均按 (1 基行号, 1 基列号) 定位报错，
    错误累计后统一抛出 ValidationError。
    """
    points: list[tuple[int, int, int]] = []
    errors: list[dict] = []

    normalized = raw.replace("\r\n", "\n").replace("\r", "\n")
    if normalized.endswith("\n"):
        normalized = normalized[:-1]
    lines = normalized.split("\n") if normalized != "" else []

    for idx, line in enumerate(lines):
        lineno = idx + 1
        if line == "":
            errors.append({
                "code": "empty_point_line",
                "source": "points",
                "line": lineno,
                "col": 1,
                "message": f"实测坐标第 {lineno} 行为空：每行须为 “x,y”（一位小数），不允许空行。",
            })
            continue

        comma = line.find(",")
        if comma == -1:
            errors.append({
                "code": "bad_point",
                "source": "points",
                "line": lineno,
                "col": len(line) + 1,
                "message": f"实测坐标第 {lineno} 行缺少英文逗号：每行须为 “x,y”，单位毫米。",
            })
            continue
        if line.find(",", comma + 1) != -1:
            col = line.find(",", comma + 1) + 1
            errors.append({
                "code": "bad_point",
                "source": "points",
                "line": lineno,
                "col": col,
                "message": f"实测坐标第 {lineno} 行第 {col} 列多了一个逗号：每行只能有一个英文逗号。",
            })
            continue

        xs, ys = line[:comma], line[comma + 1:]
        bad = False
        for part, name, base in ((xs, "x", 1), (ys, "y", comma + 2)):
            err = _decimal_error(part)
            if err is not None:
                off, reason = err
                errors.append({
                    "code": "bad_point",
                    "source": "points",
                    "line": lineno,
                    "col": base + off,
                    "message": f"实测坐标第 {lineno} 行的 {name} 坐标非法：{reason}"
                               f"（须为一位小数，单位毫米，可带负号）。",
                })
                bad = True
        if not bad:
            points.append((round(float(xs) * 10), round(float(ys) * 10), idx))

    if len(lines) > MAX_POINT_LINES:
        errors.append({
            "code": "too_many_points",
            "source": "points",
            "line": MAX_POINT_LINES + 1,
            "col": 1,
            "message": f"实测坐标共 {len(lines)} 行，超过 {MAX_POINT_LINES} 行上限"
                       f"（第 {MAX_POINT_LINES + 1} 行起须删除）。",
        })

    if errors:
        raise ValidationError(errors)
    return points


# ---------------------------------------------------------------------------
# 位移搜索与配对
# ---------------------------------------------------------------------------

def _match_at(
    theory: list[Dot],
    measured: list[tuple[int, int, int]],
    dx: int,
    dy: int,
) -> tuple[int, int, list[int], list[int], list[dict]]:
    """在位移 (dx, dy) 下做一对一配对。

    实测点平移后按 0.5 毫米网格分桶，桶内按输入顺序排列并以
    「下一个未配对项」数组做链表删除；理论点按 (行,格,点号) 顺序，
    在半径 4（0.1 毫米）覆盖的 3×3 个桶内扫描候选，取距离最近、
    同距输入行号最早的未配对实测点。

    返回 (匹配数, 匹配距离平方和, 缺失理论点下标, 多余实测点输入行号, 配对明细)。
    """
    # 建桶（坐标可能为负，用 floor 除法）
    buckets: dict[tuple[int, int], list[int]] = {}
    for i, (x, y, _line) in enumerate(measured):
        key = ((x + dx) // 5, (y + dy) // 5)
        buckets.setdefault(key, []).append(i)

    # 每个桶一条「下一个未配对项」链：nxt[node] 为后继节点下标，-1 结束
    nxt: dict[int, int] = {}
    for members in buckets.values():
        for j, node in enumerate(members):
            nxt[node] = members[j + 1] if j + 1 < len(members) else -1
    heads = {key: members[0] for key, members in buckets.items() if members}

    used = [False] * len(measured)
    r2max = MATCH_RADIUS * MATCH_RADIUS
    matches: list[dict] = []
    matched_count = 0
    dist_sum = 0
    missing: list[int] = []

    for ti, (tx, ty, _r, _c, _d) in enumerate(theory):
        best_i = -1
        best_d2 = r2max + 1
        bx, by = tx // 5, ty // 5
        for gx in (bx - 1, bx, bx + 1):
            for gy in (by - 1, by, by + 1):
                node = heads.get((gx, gy))
                while node is not None and node != -1:
                    mx, my, _line = measured[node]
                    ddx = mx + dx - tx
                    ddy = my + dy - ty
                    d2 = ddx * ddx + ddy * ddy
                    if d2 <= r2max and (
                        d2 < best_d2
                        or d2 == best_d2 and measured[node][2] < measured[best_i][2]
                    ):
                        best_d2 = d2
                        best_i = node
                    node = nxt[node]
        if best_i == -1:
            missing.append(ti)
        else:
            used[best_i] = True
            matched_count += 1
            dist_sum += best_d2
            td = theory[ti]
            mx, my, mline = measured[best_i]
            matches.append({
                "theory": {"row": td[2], "cell": td[3], "dot": td[4]},
                "measured_line": mline,
                "measured": {"x": mx / 10, "y": my / 10},
                "distance2": best_d2 / 100,
            })
            # 从所在桶的链表中摘除
            gkey = ((mx + dx) // 5, (my + dy) // 5)
            head = heads[gkey]
            if head == best_i:
                heads[gkey] = nxt[best_i]
            else:
                prev = head
                while nxt[prev] != best_i:
                    prev = nxt[prev]
                nxt[prev] = nxt[best_i]

    extras = [measured[i][2] for i in range(len(measured)) if not used[i]]
    return matched_count, dist_sum, missing, extras, matches


def _search(
    theory: list[Dot],
    measured: list[tuple[int, int, int]],
) -> tuple[int, int, int, int, list[int], list[int], list[dict]]:
    """遍历全部 61×61 个位移并择优，返回
    (dx, dy, 匹配数, 距离平方和, 缺失理论点下标, 多余实测点行号, 配对明细)。
    """
    best_key = None
    best = None
    for dx in range(SHIFT_MIN, SHIFT_MAX + 1):
        for dy in range(SHIFT_MIN, SHIFT_MAX + 1):
            count, dist_sum, missing, extras, matches = _match_at(theory, measured, dx, dy)
            key = (-count, dist_sum, dx * dx + dy * dy, dx, dy)
            if best_key is None or key < best_key:
                best_key = key
                best = (dx, dy, count, dist_sum, missing, extras, matches)
    return best


# ---------------------------------------------------------------------------
# 对外入口
# ---------------------------------------------------------------------------

def inspect(text: str, width, points_text: str) -> dict:
    """校验原文/行宽/实测坐标，排版后搜索最优整体平移并复核点位。"""
    errors: list[dict] = []
    try:
        validate(text, width)
    except ValidationError as exc:
        for err in exc.errors:
            if err["code"] == "too_long":
                # 本视图长度上限为 100（比排版视图的 500 更严），统一改写文案
                errors.append({
                    "code": "too_long",
                    "source": "text",
                    "pos": INSPECT_MAX_LENGTH,
                    "message": f"输入长度为 {len(text)} 个字符，超过 {INSPECT_MAX_LENGTH} 个字符上限。",
                })
            elif err["code"] == "bad_width":
                errors.append(err)
            else:
                errors.append({**err, "source": "text"})

    if len(text) <= MAX_LENGTH and len(text) > INSPECT_MAX_LENGTH:
        # validate 不认为超长（101～500），由本视图补报 100 字符上限
        errors.append({
            "code": "too_long",
            "source": "text",
            "pos": INSPECT_MAX_LENGTH,
            "message": f"输入长度为 {len(text)} 个字符，超过 {INSPECT_MAX_LENGTH} 个字符上限。",
        })

    # 无论原文是否合法都解析坐标，把两侧错误一并定位返回
    measured = _safe_parse(points_text, errors)

    if errors:
        raise ValidationError(errors)

    result = typeset(text, width)
    theory = _theory_dots(result)

    dx, dy, count, dist_sum, missing_idx, extras, matches = _search(theory, measured)

    missing = [
        {"row": theory[i][2], "cell": theory[i][3], "dot": theory[i][4]}
        for i in missing_idx
    ]
    matches.sort(key=lambda m: (m["theory"]["row"], m["theory"]["cell"], m["theory"]["dot"]))
    extras.sort()

    return {
        "width": width,
        "dx": dx / 10,
        "dy": dy / 10,
        "theory_count": len(theory),
        "measured_count": len(measured),
        "matched_count": count,
        "missing_count": len(missing),
        "extra_count": len(extras),
        "distance2_sum": dist_sum / 100,
        "perfect": not missing and not extras,
        "missing": missing,
        "extras": extras,
        "matches": matches,
        "measured_points": [
            {"line": line, "x": x / 10, "y": y / 10}
            for x, y, line in measured
        ],
        "typeset": result,
    }


def _safe_parse(points_text: str, errors: list[dict]) -> list:
    """原文已非法时仍尝试解析坐标以汇集错误；解析错误并入 errors。"""
    try:
        return parse_points(points_text)
    except ValidationError as exc:
        errors.extend(exc.errors)
        return []
