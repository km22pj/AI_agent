"""AI routine adjustment: the AI reviews the saved week and proposes only the changes that are needed.

The AI works in a small loop and picks a tool on each turn:
  - get_history  : earlier weeks' plans, completion per day and feedback
  - check_plan   : run the server checks on a draft (nothing is saved)
  - submit       : final proposal; saved only if every check passes
Failed checks go back to the AI so it can fix the draft. The loop is limited (MAX_TURNS,
MAX_FAILED_SUBMITS); when it runs out, or the AI call fails, nothing is changed.

The tool choice is done with structured JSON output (an "action" field), so the same
Gemini / Anthropic clients and their response validation are used.
"""
from __future__ import annotations

import json
import re
from datetime import date, timedelta

from .ai import INTENSITIES, UNSAFE_PATTERNS, WEEKDAY_NAMES, WORKOUT_SCHEMA, AIError, _load_kg

MAX_TURNS = 6
MAX_FAILED_SUBMITS = 3
MAX_HISTORY_WEEKS = 4
LOAD_STEP_MAX = 1.10        # never more than +10 % load vs the previous plan (top of ACSM's 2-10 %)
PAIN_RE = re.compile(r"통증|아파|아프|아픔|다쳤|다침|부상|결려|결림|쑤셔|저려|삐었")
LOAD_GEAR = {"덤벨", "케틀벨", "헬스장"}
DECISIONS = ["keep", "increase", "decrease", "reschedule", "mixed"]
SCOPES = ["day", "this_week", "ongoing", "none"]
KINDS = ["too_easy", "too_hard", "time_limit", "schedule", "pain", "other", "none"]

SESSION_SCHEMA = WORKOUT_SCHEMA["properties"]["sessions"]["items"]
AGENT_SCHEMA = {
    "type": "object",
    "properties": {
        "action": {"type": "string", "enum": ["get_history", "check_plan", "submit"]},
        "history_weeks": {"type": "integer"},
        "decision": {"type": "string", "enum": DECISIONS},
        "based_on_feedback_ids": {"type": "array", "items": {"type": "integer"}},
        "based_on_records": {"type": "boolean"},
        "changes": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "weekday": {"type": "string", "enum": WEEKDAY_NAMES},
                    "type": {"type": "string", "enum": ["replace", "add", "rest"]},
                    "session": SESSION_SCHEMA,
                },
                "required": ["weekday", "type", "session"],
                "additionalProperties": False,
            },
        },
        "feedback_notes": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "feedback_id": {"type": "integer"},
                    "scope": {"type": "string", "enum": SCOPES},
                    "kind": {"type": "string", "enum": KINDS},
                    "applies_from": {"type": "string"},
                    "applies_to": {"type": "string"},
                    "max_minutes": {"type": "integer"},
                },
                "required": ["feedback_id", "scope", "kind", "applies_from", "applies_to", "max_minutes"],
                "additionalProperties": False,
            },
        },
        "summary": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["action", "history_weeks", "decision", "based_on_feedback_ids", "based_on_records", "changes", "feedback_notes", "summary"],
    "additionalProperties": False,
}

AGENT_SYSTEM = """You are the routine adjustment agent of a Korean health app. Write every human-readable string in Korean.

You receive the user's latest settings, the saved routine for one Monday-to-Sunday week with what has been completed, the user's workout feedback, and recent results. Decide whether the routine should be kept, made harder, made easier, or rescheduled, and propose ONLY the changes that are needed.

Each turn, answer with exactly one action:
- get_history: earlier weeks (history_weeks 1-4) when you need more past records. Other fields: empty / defaults.
- check_plan: run the server checks on a draft without saving.
- submit: your final proposal. It is saved only if every server check passes; otherwise you get the errors and must fix them.

changes: only the days you change. Days you leave out stay exactly as they are.
  - replace: a new session for that day (the whole session, every field).
  - add: a session on a training day that has none.
  - rest: no workout that day (e.g. the user is busy). session is ignored for rest; still fill it with a minimal valid session.
Rules:
- Never change days before editable_from (past days). For today, keep every exercise marked done exactly as it is, at the same position; you may change only the remaining ones.
- Review the whole week, but do not regenerate everything and do not change only the next day by habit: change exactly the days the evidence calls for. If information is missing or unclear, keep the existing plan for that part (decision keep, no change).
- Each feedback describes how the workout of its date felt (or a plan for coming days). Difficulty feedback without pain: "too easy" -> usually a small increase on the remaining training days (one intensity level, or loads +5-10 %, or +1-2 reps); "too hard" / could not finish -> a small decrease. Keep the plan only if the data clearly argues against a change, and say why.
- Feedback marked "피드백 없음" or days without feedback: use only the records (done / not done). Never guess why something was not done.
- Temporary feedback ("내일만 20분", "이번 주만 바빠요", "약속 때문에 쉬었어요") applies only to that period. In feedback_notes give scope "day" or "this_week" with applies_from / applies_to (YYYY-MM-DD) and max_minutes if a time limit was stated (otherwise 0). Ongoing difficulty feedback ("너무 쉬워요", "늘 힘들어요") has scope "ongoing" and is also considered in later weeks.
- Do not apply the same feedback twice: feedback with intensity_already_applied=true has already been reflected; keep that level and do not increase/decrease again for it. based_on_feedback_ids lists the feedback your decision relies on; based_on_records=true if it relies on completion records.
- Never increase load or intensity when there is recent pain. Change intensity by at most one level, and load by at most 10 %, compared with the current plan.
- Respect minutes_per_session, temporary time limits, the user's rest weekdays and available equipment.
- summary: 1-4 short lines saying what you changed and why (or why you kept the plan). Shown to the user as-is.
- mode "new_week": the week was copied from last week; adapt it to last week's results and feedback. Temporary limits from last week no longer apply; days listed in restored_after_temporary_feedback are already back to their normal version, do not shorten them again for last week's temporary feedback."""


# ---------------------------------------------------------------- feedback helpers

def parse_time_limits(text: str, feedback_date: date) -> list[dict]:
    """Deterministic reading of '내일은 20분만 가능해요', '오늘은 15분', '이번 주는 20분만' -> [{date, max_minutes}]."""
    out: list[dict] = []
    for m in re.finditer(r"(오늘|내일|모레|이번\s*주)[^0-9]{0,12}?(\d{1,3})\s*분", text or ""):
        word, minutes = re.sub(r"\s+", "", m.group(1)), int(m.group(2))
        if not 5 <= minutes <= 240:
            continue
        if word == "오늘":
            days = [feedback_date]
        elif word == "내일":
            days = [feedback_date + timedelta(days=1)]
        elif word == "모레":
            days = [feedback_date + timedelta(days=2)]
        else:  # rest of this week
            days = [feedback_date + timedelta(days=i) for i in range(7 - feedback_date.weekday())]
        out += [{"date": d.isoformat(), "max_minutes": minutes} for d in days]
    return out


def has_pain(text: str) -> bool:
    return bool(PAIN_RE.search(text or ""))


# ---------------------------------------------------------------- checks

def clean_session(raw, weekday: int, body_weight: float | None) -> tuple[dict | None, list[str]]:
    label = f"{WEEKDAY_NAMES[weekday]}요일"
    if not isinstance(raw, dict):
        return None, [f"{label}: session 형식이 올바르지 않아요."]
    errors = []
    intensity = raw.get("intensity")
    if intensity not in INTENSITIES:
        errors.append(f"{label}: intensity는 {', '.join(INTENSITIES)} 중 하나여야 해요.")
    minutes = raw.get("minutes")
    if isinstance(minutes, bool) or not isinstance(minutes, (int, float)) or not 5 <= minutes <= 240:
        errors.append(f"{label}: minutes는 5~240 사이 숫자여야 해요.")
    title = raw.get("title") if isinstance(raw.get("title"), str) else ""
    if not title.strip():
        errors.append(f"{label}: title이 비어 있어요.")
    exercises = []
    raw_ex = raw.get("exercises")
    if not isinstance(raw_ex, list) or not raw_ex:
        errors.append(f"{label}: exercises가 비어 있어요.")
        raw_ex = []
    for ex in raw_ex[:10]:
        if not isinstance(ex, dict) or not isinstance(ex.get("name"), str) or not ex["name"].strip():
            errors.append(f"{label}: 운동 이름이 없는 항목이 있어요.")
            continue
        name, tip = ex["name"].strip()[:60], str(ex.get("tip") or "")[:160]
        if any(p in name or p in tip for p in UNSAFE_PATTERNS):
            errors.append(f"{label}: '{name}'은(는) 벌칙·보상 운동 같은 안전하지 않은 표현이 있어 쓸 수 없어요.")
            continue
        weight, dropped = _load_kg(ex.get("weight_kg"), body_weight)
        if dropped:
            errors.append(f"{label}: '{name}'의 중량이 안전 범위(0~몸무게의 1.5배, 최대 150kg)를 벗어났어요.")
        exercises.append({"name": name, "amount": str(ex.get("amount") or "")[:40], "weight_kg": weight,
                          "weight_note": str(ex.get("weight_note") or "")[:60], "tip": tip})
    if errors:
        return None, errors
    return {"weekday": weekday, "title": title.strip()[:40], "focus": str(raw.get("focus") or "")[:60],
            "minutes": int(minutes), "intensity": intensity, "exercises": exercises}, []


def _iso(value) -> date | None:
    try:
        return date.fromisoformat(value)
    except (TypeError, ValueError):
        return None


def check_proposal(base: dict, proposal: dict, ctx: dict) -> tuple[dict, list[str], set[int]]:
    """Builds the candidate week (base plan + proposed changes) and checks every server rule.

    ctx: week_dates (7 ISO dates), first_editable (0-7), user_rest_days, training_days, minutes_limit,
         temp_limits {iso: minutes}, locked {weekday: {index: exercise}}, weighted_ok, pain_recent,
         feedback_ids, consumed_feedback, records_new, body_weight, base_loads {name: kg}, base_max_intensity.
    Returns (candidate plan, errors, changed weekdays)."""
    errors: list[str] = []
    sessions = {s["weekday"]: s for s in base.get("sessions", []) if s.get("weekday") is not None}
    base_sessions = dict(sessions)
    rest_over = {int(k) for k in (base.get("rest_overrides") or {})}
    temp_limits = dict(ctx["temp_limits"])
    changed: set[int] = set()
    label = lambda wd: f"{WEEKDAY_NAMES[wd]}요일"  # noqa: E731

    # feedback interpretation from the AI (its own time limits are enforced too)
    for note in proposal.get("feedback_notes") or []:
        if not isinstance(note, dict) or note.get("feedback_id") not in ctx["feedback_ids"]:
            errors.append("feedback_notes에 없는 피드백 id가 있어요.")
            continue
        if note.get("scope") not in SCOPES or note.get("kind") not in KINDS:
            errors.append("feedback_notes의 scope/kind 값이 올바르지 않아요.")
            continue
        minutes = note.get("max_minutes") or 0
        if minutes:
            start, end = _iso(note.get("applies_from")), _iso(note.get("applies_to"))
            if not start or not end or end < start or not 5 <= minutes <= 240:
                errors.append("시간 제한이 있는 feedback_notes에는 올바른 applies_from/applies_to와 5~240분 max_minutes가 필요해요.")
                continue
            for iso in ctx["week_dates"]:
                if start.isoformat() <= iso <= end.isoformat():
                    temp_limits[iso] = min(temp_limits.get(iso, minutes), minutes)

    seen: set[int] = set()
    for ch in proposal.get("changes") or []:
        wd = WEEKDAY_NAMES.index(ch["weekday"]) if isinstance(ch, dict) and ch.get("weekday") in WEEKDAY_NAMES else None
        if wd is None:
            errors.append("changes의 weekday가 올바르지 않아요.")
            continue
        if wd in seen:
            errors.append(f"{label(wd)}을 두 번 바꾸려고 했어요. 하루에 하나의 변경만 쓰세요.")
            continue
        seen.add(wd)
        if wd < ctx["first_editable"]:
            errors.append(f"{label(wd)}({ctx['week_dates'][wd]})은 이미 지난 날이라 바꿀 수 없어요. 과거 계획과 기록은 그대로 둬야 해요.")
            continue
        kind = ch.get("type")
        if kind == "rest":
            if ctx["locked"].get(wd):
                errors.append(f"{label(wd)}은 이미 완료한 운동이 있어 쉬는 날로 바꿀 수 없어요.")
                continue
            sessions.pop(wd, None)
            rest_over.add(wd)
            changed.add(wd)
        elif kind in ("replace", "add"):
            if wd in ctx["user_rest_days"]:
                errors.append(f"{label(wd)}은 사용자가 정한 휴식 요일이라 운동을 넣을 수 없어요.")
                continue
            session, errs = clean_session(ch.get("session"), wd, ctx["body_weight"])
            errors += errs
            if session:
                sessions[wd] = session
                rest_over.discard(wd)
                changed.add(wd)
        else:
            errors.append(f"{label(wd)}: type은 replace, add, rest 중 하나여야 해요.")

    # exercises already done today stay exactly where they are
    for wd, locked in ctx["locked"].items():
        s = sessions.get(wd)
        for idx, ex in locked.items():
            cur = s["exercises"][idx] if s and idx < len(s["exercises"]) else None
            if not cur or any(cur.get(k) != ex.get(k) for k in ("name", "amount", "weight_kg")):
                errors.append(f"{label(wd)}의 이미 완료한 운동 '{ex['name']}'({idx + 1}번째)은 같은 자리에 그대로 두어야 해요.")

    # final state of every editable day
    for wd in range(ctx["first_editable"], 7):
        s, iso = sessions.get(wd), ctx["week_dates"][wd]
        if s and wd in ctx["user_rest_days"] and not ctx["locked"].get(wd):  # work already done that day is a record
            errors.append(f"{label(wd)}은 사용자가 정한 휴식 요일인데 운동이 있어요. type rest로 바꿔 주세요.")
        if not s and wd in ctx["training_days"] and wd not in rest_over:
            errors.append(f"{label(wd)}은 운동 요일인데 운동이 없어요. 운동을 넣거나 type rest로 쉬는 날임을 표시해 주세요.")
        if not s:
            continue
        if not (wd in ctx["locked"] and wd not in changed):  # an untouched, partly done day is left as it is
            if ctx["minutes_limit"] and s["minutes"] > ctx["minutes_limit"]:
                errors.append(f"{label(wd)}: {s['minutes']}분은 1회 가능 시간 {ctx['minutes_limit']}분을 넘어요.")
            if iso in temp_limits and s["minutes"] > temp_limits[iso]:
                errors.append(f"{label(wd)}({iso})은 피드백에 따라 {temp_limits[iso]}분 이하여야 해요 (지금 {s['minutes']}분).")
        if wd in changed:
            # without weight equipment, no new or heavier loads (an exercise already in the plan may stay as it is)
            new_loads = [ex["name"] for ex in s["exercises"] if ex.get("weight_kg")
                         and not (ctx["base_loads"].get(ex["name"]) and ex["weight_kg"] <= ctx["base_loads"][ex["name"]])]
            if new_loads and not ctx["weighted_ok"]:
                errors.append(f"{label(wd)}: 사용자에게 덤벨·케틀벨·헬스장 같은 중량 장비가 없어요. '{new_loads[0]}' 중량을 0으로 하세요.")
            b = base_sessions.get(wd)
            base_level = INTENSITIES.index(b["intensity"]) if b and b.get("intensity") in INTENSITIES else ctx["base_max_intensity"]
            level = INTENSITIES.index(s["intensity"])
            if base_level is not None:
                if level > base_level + 1:
                    errors.append(f"{label(wd)}: 강도는 한 번에 한 단계까지만 올릴 수 있어요.")
                if ctx["pain_recent"] and level > base_level:
                    errors.append(f"{label(wd)}: 최근 통증 피드백이 있어 강도를 올릴 수 없어요.")
            for ex in s["exercises"]:
                before = ctx["base_loads"].get(ex["name"])
                if before and ex.get("weight_kg"):
                    cap = max(before + 0.5, round(before * LOAD_STEP_MAX * 2) / 2)
                    if ex["weight_kg"] > cap:
                        errors.append(f"{label(wd)}: '{ex['name']}' {before}kg → {ex['weight_kg']}kg는 10%를 넘는 증량이에요 (최대 {cap}kg).")
                    if ctx["pain_recent"] and ex["weight_kg"] > before:
                        errors.append(f"{label(wd)}: 최근 통증 피드백이 있어 '{ex['name']}' 중량을 올릴 수 없어요.")

    decision = proposal.get("decision")
    if decision not in DECISIONS:
        errors.append(f"decision은 {', '.join(DECISIONS)} 중 하나여야 해요.")
    fb_ids = set(proposal.get("based_on_feedback_ids") or [])
    if fb_ids - ctx["feedback_ids"]:
        errors.append("based_on_feedback_ids에 없는 피드백 id가 있어요.")
    if decision in ("increase", "decrease"):
        fresh = fb_ids - ctx["consumed_feedback"]
        if not fresh and not (proposal.get("based_on_records") and ctx["records_new"]):
            errors.append("이미 반영한 피드백·기록만으로 같은 증감을 다시 할 수 없어요. keep으로 두거나 새 근거를 쓰세요.")
        if decision == "increase" and ctx["pain_recent"]:
            errors.append("최근 통증 피드백이 있어 increase를 할 수 없어요.")
    if changed and not [x for x in proposal.get("summary") or [] if isinstance(x, str) and x.strip()]:
        errors.append("무엇을 왜 바꿨는지 summary에 적어 주세요.")

    candidate = {**base, "sessions": [sessions[k] for k in sorted(sessions)], "rest_overrides": {str(k): True for k in sorted(rest_over)}}
    return candidate, errors, changed


# ---------------------------------------------------------------- loop

def _compact(proposal: dict) -> str:
    """The AI's own draft, shown back to it in the next turn."""
    keep = {k: proposal.get(k) for k in ("action", "decision", "based_on_feedback_ids", "based_on_records", "changes", "feedback_notes", "summary")}
    return json.dumps(keep, ensure_ascii=False)[:6000]


def run_agent(ai, context: dict, check, history) -> dict:
    """check(proposal) -> (candidate, errors, changed); history(weeks) -> dict. Raises AIError when it gives up."""
    steps: list[str] = []
    failed = 0
    tools_used: list[str] = []
    for turn in range(1, MAX_TURNS + 1):
        content = [{"type": "text", "text": "상황(JSON):\n" + json.dumps(context, ensure_ascii=False)}]
        content += [{"type": "text", "text": s} for s in steps]
        data, model = ai.ask_json(AGENT_SYSTEM, content, AGENT_SCHEMA, "agent")
        if not isinstance(data, dict):
            raise AIError("AI 응답 형식이 올바르지 않아 기존 계획을 유지했어요.", True, "ai_invalid")
        action = data.get("action")
        tools_used.append(action if isinstance(action, str) else "?")
        if action == "get_history":
            weeks = data.get("history_weeks") if isinstance(data.get("history_weeks"), int) else 1
            weeks = max(1, min(MAX_HISTORY_WEEKS, weeks))
            steps.append(f"[{turn}단계] get_history(weeks={weeks}) 결과:\n" + json.dumps(history(weeks), ensure_ascii=False))
            continue
        if action not in ("check_plan", "submit"):
            steps.append(f"[{turn}단계] 알 수 없는 action이에요. get_history, check_plan, submit 중 하나를 쓰세요.")
            continue
        candidate, errors, changed = check(data)
        if action == "check_plan":
            result = "검사 통과 (아직 저장 안 됨). 이대로 submit 하면 저장돼요." if not errors else "검사 실패:\n- " + "\n- ".join(errors)
            steps.append(f"[{turn}단계] check_plan 변경안:\n{_compact(data)}\n결과: {result}")
            continue
        if not errors:
            return {"proposal": data, "candidate": candidate, "changed": changed, "model": model,
                    "turns": turn, "failed_submits": failed, "tools": tools_used}
        failed += 1
        if failed >= MAX_FAILED_SUBMITS:
            raise AIError(f"AI 변경안이 서버 검사를 {failed}번 통과하지 못해 기존 계획을 유지했어요. (마지막 이유: {errors[0]})", True, "agent_check_failed")
        steps.append(f"[{turn}단계] submit 거절 — 저장하지 않았어요. 변경안:\n{_compact(data)}\n검사 실패:\n- " + "\n- ".join(errors) + "\n고친 변경안을 다시 submit 하세요.")
    raise AIError("AI가 정해진 단계 안에 변경안을 내지 못해 기존 계획을 유지했어요.", True, "agent_turn_limit")
