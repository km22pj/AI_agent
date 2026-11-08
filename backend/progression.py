"""Helpers for weekly routines: week boundaries, exercise keys, last-week completion and safety caps.

The old fixed rule (easier below 50 % done, harder at 100 %) was removed: the AI routine review
(backend/routine_agent.py) now decides keep / increase / decrease / reschedule, and the server checks it.
cap_against_previous still limits a regenerated routine to one step from the previous one:
loads at most +10 % (the top of the 2-10 % increase in the ACSM 2009 position stand) and intensity
at most one level.
"""
from __future__ import annotations

from datetime import date, timedelta

INTENSITIES = ["가볍게", "보통", "조금 힘들게"]


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
    # Only the facts: whether to keep, increase or decrease is decided by the AI review.
    return {"week_start": week_monday.isoformat(), "planned": planned, "done": done, "rate": round(done / planned, 2)}


def _round_half(value: float) -> float:
    return round(value * 2) / 2


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
