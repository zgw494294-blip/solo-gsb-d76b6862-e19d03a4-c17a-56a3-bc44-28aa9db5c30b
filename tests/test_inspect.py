"""压印点位复核引擎测试（含与独立朴素实现的随机对照）。"""

import random

import pytest

from app.braille import ValidationError, typeset
from app.inspect import (
    _match_at,
    _theory_dots,
    inspect,
    parse_points,
)


# ---------------------------------------------------------------------------
# 独立朴素参照：逐位移、逐理论点直接贪心配对（仅用于小输入对照）
# ---------------------------------------------------------------------------

def _ref_match(theory, measured, dx, dy):
    """在位移 (dx, dy) 下按规则贪心配对，返回 (匹配数, 距离平方和, 缺失下标, 多余行号)。"""
    used = [False] * len(measured)
    count = 0
    dist_sum = 0
    missing = []
    for ti, t in enumerate(theory):
        best = None
        for i, (x, y, line) in enumerate(measured):
            if used[i]:
                continue
            d2 = (x + dx - t[0]) ** 2 + (y + dy - t[1]) ** 2
            if d2 <= 16 and (
                best is None or d2 < best[0] or (d2 == best[0] and line < best[1])
            ):
                best = (d2, line, i)
        if best is None:
            missing.append(ti)
        else:
            used[best[2]] = True
            count += 1
            dist_sum += best[0]
    extras = [measured[i][2] for i in range(len(measured)) if not used[i]]
    return count, dist_sum, missing, extras


def _ref_search(theory, measured):
    """遍历全部位移择优，返回 (dx, dy, 匹配数, 距离平方和, 缺失, 多余)。"""
    best_key = None
    best = None
    for dx in range(-30, 31):
        for dy in range(-30, 31):
            count, dist_sum, missing, extras = _ref_match(theory, measured, dx, dy)
            key = (-count, dist_sum, dx * dx + dy * dy, dx, dy)
            if best_key is None or key < best_key:
                best_key = key
                best = (dx, dy, count, dist_sum, missing, extras)
    return best


def _check_against_reference(text, width, points):
    """inspect 的位移/匹配/缺失/多余须与朴素参照一致。"""
    r = inspect(text, width, points)
    theory = _theory_dots(typeset(text, width))
    measured = parse_points(points)
    dx, dy, count, dist_sum, missing, extras = _ref_search(theory, measured)
    assert (r["dx"], r["dy"]) == (dx / 10, dy / 10)
    assert r["matched_count"] == count
    assert r["distance2_sum"] == dist_sum / 100
    assert r["missing"] == [
        {"row": theory[i][2], "cell": theory[i][3], "dot": theory[i][4]}
        for i in missing
    ]
    assert r["extras"] == sorted(extras)
    assert r["perfect"] == (not missing and not extras)
    return r


# ---------------------------------------------------------------------------
# 理论点阵
# ---------------------------------------------------------------------------

def test_theory_dots_sorted_and_complete():
    # a＝点1；b＝点1,2；理论点按 (行, 格, 点号) 升序
    assert _theory_dots(typeset("ab", 10)) == [
        (0, 0, 0, 0, 1),
        (60, 0, 0, 1, 1),
        (60, 25, 0, 1, 2),
    ]


# ---------------------------------------------------------------------------
# 坐标解析
# ---------------------------------------------------------------------------

def test_parse_points_ok_and_line_numbers():
    pts = parse_points("0.0,0.0\n12.5,3.4\n6.0,2.5")
    assert pts == [(0, 0, 0), (125, 34, 1), (60, 25, 2)]


def test_parse_points_empty_text_is_zero_points():
    assert parse_points("") == []
    assert parse_points("\n") == []  # 结尾单个换行不产生空行


def test_parse_points_rejects_blank_middle_line():
    with pytest.raises(ValidationError) as ei:
        parse_points("1.0,2.0\n\n3.0,4.0")
    err = ei.value.errors[0]
    assert err["code"] == "empty_point_line"
    assert (err["line"], err["col"]) == (2, 1)


def test_parse_points_missing_comma_located():
    with pytest.raises(ValidationError) as ei:
        parse_points("1.0 2.0")
    err = ei.value.errors[0]
    assert err["code"] == "bad_point"
    assert (err["line"], err["col"]) == (1, 8)


def test_parse_points_extra_comma_located():
    with pytest.raises(ValidationError) as ei:
        parse_points("1.0,2.0,")
    err = ei.value.errors[0]
    assert (err["line"], err["col"]) == (1, 8)


@pytest.mark.parametrize("src,line,col", [
    ("1.05,2.0", 1, 4),     # 两位小数
    ("1.,2.0", 1, 3),       # 缺小数位
    ("1.0,2", 1, 6),        # y 缺小数点
    ("1.0,x", 1, 5),        # y 非数字
    ("+1.0,2.0", 1, 1),     # 正号不允许（负号可以）
    ("1.0 ,2.0", 1, 4),     # 空格不允许
    (",2.0", 1, 1),         # x 为空
    ("-1.0,2.0", 1, 1),     # 负号允许（不报错）
])
def test_parse_points_bad_decimal_located(src, line, col):
    if src == "-1.0,2.0":
        # 负号允许（不报错）
        assert parse_points(src) == [(-10, 20, 0)]
        return
    with pytest.raises(ValidationError) as ei:
        parse_points(src)
    err = ei.value.errors[0]
    assert err["code"] == "bad_point"
    assert (err["line"], err["col"]) == (line, col)


def test_parse_points_too_many_lines():
    with pytest.raises(ValidationError) as ei:
        parse_points("\n".join(["1.0,1.0"] * 401))
    err = ei.value.errors[0]
    assert err["code"] == "too_many_points"
    assert err["line"] == 401


def test_parse_points_collects_multiple_errors():
    with pytest.raises(ValidationError) as ei:
        parse_points("bad\n1.0,2.0\n3.0")
    assert [e["line"] for e in ei.value.errors] == [1, 3]


# ---------------------------------------------------------------------------
# 配对规则（直接驱动 _match_at，固定理论点与实测点）
# ---------------------------------------------------------------------------

def _T(x, y):
    return (x, y, 0, 0, 1)  # 理论点坐标（0.1 毫米），行格点号占位


def test_match_radius_boundary():
    theory = [_T(0, 0)]
    # 恰好 0.4 毫米（平方 16）可配对
    count, _, missing, extras, _ = _match_at(theory, [(4, 0, 0)], 0, 0)
    assert (count, missing, extras) == (1, [], [])
    # 0.5 毫米超出阈值
    count, _, missing, extras, _ = _match_at(theory, [(5, 0, 0)], 0, 0)
    assert (count, missing, extras) == (0, [0], [0])


def test_match_nearest_then_earliest_input_order():
    theory = [_T(0, 0)]
    measured = [(3, 0, 0), (0, 3, 1), (1, 0, 2)]  # 前两个同距，第三个最近
    count, dist_sum, _, extras, matches = _match_at(theory, measured, 0, 0)
    assert count == 1 and dist_sum == 1
    assert matches[0]["measured_line"] == 2       # 最近者优先
    assert sorted(extras) == [0, 1]

    # 去掉最近者：同距取输入行号较早者（行 0）
    count, dist_sum, _, extras, matches = _match_at(theory, measured[:2], 0, 0)
    assert matches[0]["measured_line"] == 0
    assert extras == [1]


def test_match_one_to_one_and_theory_order():
    # 两个理论点竞争同一实测点：理论点按顺序处理，先到先得
    theory = [_T(0, 0), _T(4, 0)]
    measured = [(2, 0, 0)]
    count, _, missing, extras, matches = _match_at(theory, measured, 0, 0)
    assert count == 1
    assert matches[0]["theory"]["dot"] == 1 and missing == [1]
    assert extras == []


def test_match_negative_coordinates_buckets():
    theory = [_T(-10, -10)]
    measured = [(-11, -9, 0)]
    count, dist_sum, missing, extras, _ = _match_at(theory, measured, 0, 0)
    assert (count, dist_sum, missing, extras) == (1, 2, [], [])


# ---------------------------------------------------------------------------
# 位移择优
# ---------------------------------------------------------------------------

def test_perfect_match_zero_shift():
    r = inspect("ab", 10, "0.0,0.0\n6.0,0.0\n6.0,2.5")
    assert r["perfect"] is True
    assert (r["dx"], r["dy"]) == (0.0, 0.0)
    assert r["matched_count"] == 3
    assert r["distance2_sum"] == 0.0
    assert r["missing"] == [] and r["extras"] == []


def test_recovers_overall_shift():
    # 实测点整体平移 (+0.5, +0.3)：最优位移 (-0.5, -0.3)，零距离
    r = inspect("ab", 10, "0.5,0.3\n6.5,0.3\n6.5,2.8")
    assert r["perfect"] is True
    assert (r["dx"], r["dy"]) == (-0.5, -0.3)
    assert r["matched_count"] == 3


def test_missing_and_extra_reported():
    # 漏掉 b 的点 2，多出第 3 行 (9.9, 9.9)
    r = inspect("ab", 10, "0.0,0.0\n6.0,0.0\n9.9,9.9")
    assert r["perfect"] is False
    assert r["matched_count"] == 2
    assert r["missing"] == [{"row": 0, "cell": 1, "dot": 2}]
    assert r["extras"] == [2]  # 零基输入行号
    assert r["missing_count"] == 1 and r["extra_count"] == 1


def test_empty_measured_all_missing():
    r = inspect("ab", 10, "")
    assert r["matched_count"] == 0
    assert r["missing_count"] == 3
    assert r["extra_count"] == 0
    assert (r["dx"], r["dy"]) == (0.0, 0.0)  # 全零匹配时位移归原点


def test_shift_tie_prefers_smaller_distance_sum():
    # 理论点 (0,0)；实测 (1.0, 0.0)：位移 (-1.0, 0) 零距离优于 (-0.9, 0) 等
    r = inspect("a", 10, "1.0,0.0")
    assert (r["dx"], r["dy"]) == (-1.0, 0.0)
    assert r["distance2_sum"] == 0.0


def test_shift_tie_prefers_smaller_norm_then_dx():
    # 实测 (0.4, 0.0)：位移 (-0.4,0) 与 (0.4,0) 均零距离，范数并列，取 dx 小者
    r = inspect("a", 10, "0.4,0.0")
    assert (r["dx"], r["dy"]) == (-0.4, 0.0)


def test_shift_tie_prefers_smaller_dy():
    r = inspect("a", 10, "0.0,1.0")
    assert (r["dx"], r["dy"]) == (0.0, -1.0)


def test_shift_clamped_to_three_mm():
    # 实测点距理论点 3.5 毫米：±3 毫米位移后仍剩 0.5 毫米，超过 0.4 阈值，无法配对
    r = inspect("a", 10, "3.5,0.0")
    assert r["matched_count"] == 0
    assert r["missing_count"] == 1
    assert r["extras"] == [0]


def test_result_embeds_typeset_and_points():
    r = inspect("Hi 7", 10, "0.0,0.0")
    assert r["width"] == 10
    assert r["typeset"]["braille_text"] == typeset("Hi 7", 10)["braille_text"]
    assert r["theory_count"] == len(_theory_dots(r["typeset"]))
    assert r["measured_count"] == 1
    assert r["measured_points"] == [{"line": 0, "x": 0.0, "y": 0.0}]
    assert len(r["matches"]) == r["matched_count"]


# ---------------------------------------------------------------------------
# 输入校验
# ---------------------------------------------------------------------------

def test_text_limit_is_100():
    r = inspect("a" * 100, 10, "")
    assert r["theory_count"] > 0
    with pytest.raises(ValidationError) as ei:
        inspect("a" * 101, 10, "")
    err = ei.value.errors[0]
    assert err["code"] == "too_long"
    assert err["source"] == "text"
    assert "100" in err["message"]


def test_text_errors_carry_source_and_position():
    with pytest.raises(ValidationError) as ei:
        inspect("ab\tc", 10, "")
    err = ei.value.errors[0]
    assert err["code"] == "illegal_char"
    assert err["source"] == "text"
    assert (err["line"], err["col"]) == (1, 3)


def test_width_error_has_no_source():
    with pytest.raises(ValidationError) as ei:
        inspect("a", 41, "")
    assert [e["code"] for e in ei.value.errors] == ["bad_width"]
    assert "source" not in ei.value.errors[0]


def test_text_and_points_errors_collected_together():
    with pytest.raises(ValidationError) as ei:
        inspect("ab\tc", 10, "bad")
    sources = sorted(e["source"] for e in ei.value.errors)
    assert sources == ["points", "text"]


# ---------------------------------------------------------------------------
# 与独立朴素实现的随机对照
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("text,width,points", [
    ("ab", 10, "0.0,0.0\n6.0,0.0\n6.0,2.5"),
    ("ab", 10, "0.5,0.3\n6.5,0.3\n6.5,2.8"),
    ("Hi 7", 10, "0.0,0.0\n2.5,0.0\n6.0,0.0\n8.5,0.0\n12.0,0.0\n15.0,0.0\n18.0,0.0"),
    ("a", 10, ""),
    ("a", 10, "0.3,0.0\n0.0,0.3\n0.4,0.4"),
    ("q", 10, "0.0,0.0\n2.5,0.0\n0.0,2.5\n2.5,2.5\n0.0,5.0\n9.9,9.9"),
    ("ab\ncd", 10, "0.2,0.1\n6.1,0.0\n6.0,2.4\n0.0,10.1\n6.0,10.0\n6.0,12.5"),
])
def test_fixed_cases_match_reference(text, width, points):
    _check_against_reference(text, width, points)


def test_random_small_cases_match_reference():
    random.seed(20260927)
    for _ in range(6):
        text = " ".join(
            "".join(random.choice("abq") for _ in range(random.randint(1, 3)))
            for _ in range(random.randint(1, 3))
        )
        n = random.randint(0, 8)
        pts = []
        for _ in range(n):
            x = random.randint(-5, 125) / 10
            y = random.randint(-5, 105) / 10
            pts.append(f"{x:.1f},{y:.1f}")
        _check_against_reference(text, 10, "\n".join(pts))
