"""Weekly routine adjustment from how much of last week's routine was completed.

Deterministic (no AI): the server changes the saved routine once per week.
- Load increase: +5 % — inside the 2-10 % increase that the ACSM 2009 position stand
  ("Progression Models in Resistance Training for Healthy Adults") recommends when the current
  workload can be done for 1-2 reps over the target. APP INTERPRETATION: completing every
  exercise of the week is used as the sign that the current workload is manageable.
- Thresholds (< 50 % done -> easier, 100 % done -> harder) and the size of the easier step
  (one intensity level down, one set fewer, ~20 % fewer reps/time, -10 % load) are APP RULES,
  not values from a guideline.
"""
from __future__ import annotations

import copy
import re
from datetime import date, timedelta

INTENSITIES = ["가볍게", "보통", "조금 힘들게"]
LOW_RATE = 0.5
LOAD_UP = 1.05
LOAD_DOWN = 0.90


def week_start(day: date) -> date:
    return day - timedelta(days=day.weekday())


def exercise_key(index: int, name: str) -> str:
    return f"{index}:{name}"[:100]


def review_week(plan: dict, plan_created: date, checks: set[tuple[str, str]], week_monday: date) -> dict | None:
    """Completion of `plan` during the week starting `week_monday` (only days on/after the plan was made).
    checks: {(date_iso, item_key)}. None when nothing was planned in that window."""
    by_day = {s.get("weekday"): s for s in plan.get("sessions", []) if s.get("weekday") is not None}
    planned = done = 0
    for offset in range(7):
        day = week_monday + timedelta(days=offset)
        if day < plan_created or offset not in by_day:
            continue
        keys = [exercise_key(i, ex["name"]) for i, ex in enumerate(by_day[offset]["exercises"])]
        planned += len(keys)
        done += sum(1 for k in keys if (day.isoformat(), k) in checks)
    if not planned:
        return None
    rate = done / planned
    level = "decrease" if rate < LOW_RATE else "increase" if done == planned else "keep"
    return {"week_start": week_monday.isoformat(), "planned": planned, "done": done, "rate": round(rate, 2), "level": level}


def _round_half(value: float) -> float:
    return round(value * 2) / 2


def _scale_amount(amount: str, level: str, has_load: bool) -> str:
    """'3세트 × 10회' / '30초' / '5분' -> adjusted text."""
    def sets(m):
        n = int(m.group(1))
        return f"{max(1, n - 1) if level == 'decrease' else n}세트"

    def reps(m):
        n = int(m.group(1))
        if level == "decrease":
            n = max(5, round(n * 0.8)) if n > 5 else n
        elif level == "increase" and not has_load:
            n = min(20, n + 2)
        return f"{n}회"

    def seconds(m):
        n = int(m.group(1))
        n = max(10, round(n * 0.8)) if level == "decrease" else min(90, n + 10) if level == "increase" else n
        return f"{n}초"

    def minutes(m):
        n = int(m.group(1))
        return f"{max(3, round(n * 0.8)) if level == 'decrease' else n}분"

    out = re.sub(r"(\d+)\s*세트", sets, amount)
    out = re.sub(r"(\d+)\s*회", reps, out)
    out = re.sub(r"(\d+)\s*초", seconds, out)
    return re.sub(r"(\d+)\s*분", minutes, out)


def cap_against_previous(plan: dict, previous: dict, level: str) -> int:
    """Keep an AI-made routine within one step of the previous one (in place). Returns how many values were capped.
    Loads: at most +10 % (the top of the ACSM 2-10 % range); for 'decrease' no heavier than before.
    Intensity: at most one level above the previous highest; for 'decrease' at most one level below it."""
    prev_loads = {ex["name"]: ex["weight_kg"] for s in previous.get("sessions", []) for ex in s.get("exercises", []) if ex.get("weight_kg")}
    prev_top = max((INTENSITIES.index(s["intensity"]) for s in previous.get("sessions", []) if s.get("intensity") in INTENSITIES), default=None)
    capped = 0
    for session in plan.get("sessions", []):
        if prev_top is not None and session.get("intensity") in INTENSITIES:
            limit = max(0, prev_top - 1) if level == "decrease" else min(len(INTENSITIES) - 1, prev_top + 1)
            if INTENSITIES.index(session["intensity"]) > limit:
                session["intensity"] = INTENSITIES[limit]
                capped += 1
        for ex in session.get("exercises", []):
            before = prev_loads.get(ex["name"])
            if before and ex.get("weight_kg"):
                limit = before if level == "decrease" else _round_half(before * 1.10)
                if ex["weight_kg"] > limit:
                    ex["weight_kg"] = limit
                    capped += 1
    return capped


def adjust_plan(plan: dict, level: str) -> tuple[dict, list[str]]:
    """Returns (new plan, human-readable changes). 'keep' returns the plan unchanged."""
    if level == "keep":
        return plan, []
    new = copy.deepcopy(plan)
    step = -1 if level == "decrease" else 1
    loads_changed = reps_up = 0
    for session in new.get("sessions", []):
        idx = INTENSITIES.index(session["intensity"]) if session.get("intensity") in INTENSITIES else 0
        session["intensity"] = INTENSITIES[max(0, min(len(INTENSITIES) - 1, idx + step))]
        if level == "decrease":
            session["minutes"] = max(10, round(session.get("minutes", 30) * 0.85))
        for ex in session.get("exercises", []):
            weight = ex.get("weight_kg")
            has_load = bool(weight)
            if has_load:
                target = _round_half(weight * (LOAD_DOWN if level == "decrease" else LOAD_UP))
                if level == "increase" and target == weight:
                    # +5 % rounds to the same dumbbell: progress with one more rep instead
                    ex["amount"] = re.sub(r"(\d+)\s*회", lambda m: f"{min(20, int(m.group(1)) + 1)}회", ex.get("amount", ""))
                    reps_up += 1
                elif target > 0:
                    ex["weight_kg"] = target
                    loads_changed += 1
            ex["amount"] = _scale_amount(ex.get("amount", ""), level, has_load)
    if level == "decrease":
        changes = ["강도를 한 단계 낮췄어요", "세트 1개, 횟수·시간 약 20%를 줄였어요", "운동 시간을 약 15% 줄였어요"]
        if loads_changed:
            changes.append("중량을 약 10% 낮췄어요")
    else:
        changes = ["강도를 한 단계 높였어요", "중량 없는 동작은 횟수 +2회, 시간 +10초"]
        if loads_changed:
            changes.append("중량을 약 5% 높였어요 (ACSM 권고 2~10% 범위)")
        if reps_up:
            changes.append("중량을 올리기 어려운 동작은 횟수를 1회 늘렸어요")
    return new, changes
