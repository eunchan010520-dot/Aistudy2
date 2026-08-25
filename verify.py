#!/usr/bin/env python3
"""
칼로리 기록 검수 스크립트 (6단계 검수)

사용법:
    python3 verify.py                # 오늘 날짜 검수
    python3 verify.py 2026-08-25     # 특정 날짜 검수
    python3 verify.py --month        # 이번 달 전체 요약

검수 항목:
  1. 누적 합계 오차   - CSV를 독립적으로 재합산
  2. 탄단지 정합성    - (탄x4 + 단x4 + 지x9) vs 기록된 kcal, ±10% 허용
  3. 중복 입력        - 같은 날/끼니/음식 중복 탐지
  4. 끼니 누락        - 아침/점심/저녁 중 비어 있는 끼니
  5. 표준량 일치      - foods_standard.csv의 표준 분량과 다른 경우 표시
  6. 목표 대비 판정   - profile.json의 target_kcal과 비교
"""

import csv
import json
import os
import sys
from collections import defaultdict
from datetime import date

BASE = os.path.dirname(os.path.abspath(__file__))
PROFILE = os.path.join(BASE, "profile.json")
STANDARD = os.path.join(BASE, "foods_standard.csv")

MEALS = ["아침", "점심", "저녁"]
MACRO_TOLERANCE = 0.10      # 탄단지 <-> kcal 허용 오차
PORTION_TOLERANCE = 0.20    # 표준 분량 대비 허용 편차
MACRO_CHECK_MIN_KCAL = 50   # 이 미만은 반올림 오차가 커서 정합성 검사 제외


def load_profile():
    with open(PROFILE, encoding="utf-8") as f:
        return json.load(f)


def load_standard():
    table = {}
    if not os.path.exists(STANDARD):
        return table
    with open(STANDARD, encoding="utf-8") as f:
        for row in csv.DictReader(f):
            table[row["food"].strip()] = row
    return table


def log_path(day):
    return os.path.join(BASE, f"log_{day[:7]}.csv")


def load_rows(day=None, month=None):
    target_month = month or (day[:7] if day else None)
    path = os.path.join(BASE, f"log_{target_month}.csv")
    if not os.path.exists(path):
        return [], path
    rows = []
    with open(path, encoding="utf-8") as f:
        for i, row in enumerate(csv.DictReader(f), start=2):
            if not row.get("date"):
                continue
            row["_line"] = i
            if day and row["date"].strip() != day:
                continue
            rows.append(row)
    return rows, path


def num(row, key):
    """숫자 파싱 실패를 조용히 넘기지 않고 None으로 표시."""
    raw = (row.get(key) or "").strip()
    if raw == "":
        return None
    try:
        return float(raw)
    except ValueError:
        return None


def verify_day(day, profile, standard):
    rows, path = load_rows(day=day)
    issues = []
    warnings = []

    if not rows:
        return {
            "day": day, "rows": [], "path": path, "total": 0,
            "macros": {"carb": 0, "protein": 0, "fat": 0},
            "issues": [], "warnings": [f"{day} 기록이 없습니다."],
            "verdict": "기록 없음",
        }

    # --- 1. 독립 재합산 ---
    total = 0.0
    macros = {"carb": 0.0, "protein": 0.0, "fat": 0.0}
    for r in rows:
        kcal = num(r, "kcal")
        if kcal is None:
            issues.append(f"L{r['_line']} [{r.get('food')}] kcal 값이 비었거나 숫자가 아님")
            continue
        total += kcal
        for key, col in (("carb", "carb_g"), ("protein", "protein_g"), ("fat", "fat_g")):
            v = num(r, col)
            if v is None:
                warnings.append(f"L{r['_line']} [{r.get('food')}] {col} 누락 — 영양소 합계에서 제외됨")
            else:
                macros[key] += v

        # --- 2. 탄단지 <-> kcal 정합성 ---
        # 주류는 알코올(7kcal/g)이 탄단지에 안 잡히므로 제외.
        # 저칼로리 항목은 반올림 오차가 비율을 과장하므로 제외.
        food_name = (r.get("food") or "").strip()
        std_row = standard.get(food_name)
        is_alcohol = bool(std_row) and std_row.get("category") == "주류"
        c, p, fat = num(r, "carb_g"), num(r, "protein_g"), num(r, "fat_g")
        if None not in (c, p, fat) and kcal >= MACRO_CHECK_MIN_KCAL and not is_alcohol:
            derived = c * 4 + p * 4 + fat * 9
            diff = abs(derived - kcal) / kcal
            if diff > MACRO_TOLERANCE:
                issues.append(
                    f"L{r['_line']} [{food_name}] 탄단지 환산 {derived:.0f}kcal "
                    f"vs 기록 {kcal:.0f}kcal (오차 {diff*100:.0f}%)"
                )

        # --- 5. 표준 분량 대조 ---
        std = standard.get((r.get("food") or "").strip())
        g = num(r, "portion_g")
        if std and g:
            std_g = float(std["portion_g"])
            mult = g / std_g
            nearest = max(1, round(mult))
            # 2인분, 3인분처럼 표준량의 정수배면 정상으로 간주
            if abs(mult - nearest) / nearest > PORTION_TOLERANCE:
                warnings.append(
                    f"L{r['_line']} [{r.get('food')}] {g:.0f}g — 표준 1인분 {std_g:.0f}g의 "
                    f"{mult:.1f}배로 애매함. 분량 재확인 필요"
                )

    # --- 3. 중복 탐지 ---
    seen = defaultdict(list)
    for r in rows:
        key = (r.get("meal", "").strip(), (r.get("food") or "").strip())
        seen[key].append(r["_line"])
    for (meal, food), lines in seen.items():
        if len(lines) > 1:
            warnings.append(f"중복 의심: {meal} / {food} — 라인 {lines}")

    # --- 4. 끼니 누락 ---
    logged = {r.get("meal", "").strip() for r in rows}
    missing = [m for m in MEALS if m not in logged]
    if missing:
        warnings.append(f"기록 없는 끼니: {', '.join(missing)}")

    # --- 6. 목표 대비 판정 ---
    target = profile["calculation"]["target_kcal"]
    band = profile["tolerance"]["daily_kcal_ok_band_pct"] / 100
    lo, hi = target * (1 - band), target * (1 + band)
    if total < lo:
        verdict = f"부족 ({target - total:.0f}kcal 남음)"
    elif total > hi:
        verdict = f"초과 ({total - target:.0f}kcal 초과)"
    else:
        verdict = "적정 범위"

    return {
        "day": day, "rows": rows, "path": path, "total": total,
        "macros": macros, "issues": issues, "warnings": warnings,
        "verdict": verdict, "target": target,
    }


def print_day(res, profile):
    print(f"\n{'='*56}")
    print(f"  검수 결과 — {res['day']}")
    print(f"{'='*56}")

    if not res["rows"]:
        print("  기록 없음")
        return

    print(f"\n[기록 {len(res['rows'])}건]")
    for r in res["rows"]:
        print(f"  {r.get('meal','?'):<4} {r.get('food','?'):<16} "
              f"{r.get('portion_g','?'):>5}g  {r.get('kcal','?'):>5}kcal  "
              f"({r.get('confidence','-')})")

    t = res["target"]
    mt = profile["macro_target"]
    m = res["macros"]
    print(f"\n[누적 vs 목표]")
    print(f"  칼로리   {res['total']:>7.0f} / {t:<7} kcal   ({res['total']/t*100:>5.1f}%)")
    print(f"  탄수화물 {m['carb']:>7.0f} / {mt['carb_g']:<7} g      ({m['carb']/mt['carb_g']*100:>5.1f}%)")
    print(f"  단백질   {m['protein']:>7.0f} / {mt['protein_g']:<7} g      ({m['protein']/mt['protein_g']*100:>5.1f}%)")
    print(f"  지방     {m['fat']:>7.0f} / {mt['fat_g']:<7} g      ({m['fat']/mt['fat_g']*100:>5.1f}%)")
    print(f"\n  판정: {res['verdict']}")

    print(f"\n[검수]")
    if res["issues"]:
        print("  ✗ 오류")
        for i in res["issues"]:
            print(f"    - {i}")
    if res["warnings"]:
        print("  ! 확인 필요")
        for w in res["warnings"]:
            print(f"    - {w}")
    if not res["issues"] and not res["warnings"]:
        print("  ✓ 계산 오차 없음. 이상 항목 없음.")
    print()


def print_month(profile):
    month = date.today().strftime("%Y-%m")
    rows, path = load_rows(month=month)
    if not rows:
        print(f"{month} 기록이 없습니다.")
        return
    by_day = defaultdict(float)
    for r in rows:
        k = num(r, "kcal")
        if k is not None:
            by_day[r["date"].strip()] += k
    t = profile["calculation"]["target_kcal"]
    print(f"\n  {month} 일별 누적\n  {'-'*40}")
    for d in sorted(by_day):
        v = by_day[d]
        bar = "█" * int(v / t * 20)
        print(f"  {d}  {v:>6.0f} kcal  {bar} {v/t*100:>5.1f}%")
    avg = sum(by_day.values()) / len(by_day)
    print(f"  {'-'*40}")
    print(f"  일평균 {avg:.0f} kcal / 목표 {t} kcal ({avg/t*100:.1f}%)\n")


def main():
    profile = load_profile()
    standard = load_standard()
    args = sys.argv[1:]

    if args and args[0] == "--month":
        print_month(profile)
        return

    day = args[0] if args else date.today().isoformat()
    res = verify_day(day, profile, standard)
    print_day(res, profile)

    if res["issues"]:
        sys.exit(1)


if __name__ == "__main__":
    main()
