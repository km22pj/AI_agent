"""Input validation, user settings, and nutrition math (all computed in code)."""
from __future__ import annotations

import math
import re
from datetime import date, timedelta


class ValidationError(Exception):
    """Bad user input. The message is shown to the user as-is."""


# ---------------------------------------------------------------- primitives

DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
TIME_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
CLIENT_ID_RE = re.compile(r"^[A-Za-z0-9_-]{8,64}$")


def parse_date(value, field: str = "날짜") -> str:
    if not isinstance(value, str) or not DATE_RE.match(value):
        raise ValidationError(f"{field} 형식이 올바르지 않아요.")
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise ValidationError(f"{field}가 올바른 날짜가 아니에요.") from exc
    if not (2000 <= parsed.year <= 2100):
        raise ValidationError(f"{field} 범위를 확인해 주세요.")
    return value


def parse_time(value) -> str | None:
    if value in (None, ""):
        return None
    if not isinstance(value, str) or not TIME_RE.match(value):
        raise ValidationError("시간 형식이 올바르지 않아요. (예: 08:30)")
    return value


def parse_client_id(value) -> str:
    if not isinstance(value, str) or not CLIENT_ID_RE.match(value):
        raise ValidationError("요청 식별값이 올바르지 않아요. 페이지를 새로고침해 주세요.")
    return value


def text(value, limit: int, field: str, required: bool = False) -> str:
    if value is None:
        value = ""
    if not isinstance(value, str):
        raise ValidationError(f"{field} 값이 올바르지 않아요.")
    value = value.strip()
    if required and not value:
        raise ValidationError(f"{field}을(를) 입력해 주세요.")
    if len(value) > limit:
        raise ValidationError(f"{field}은(는) {limit}자 이내로 입력해 주세요.")
    return value


def optional_number(value, low: float, high: float, field: str):
    if value in (None, ""):
        return None
    if isinstance(value, bool):
        raise ValidationError(f"{field} 값이 올바르지 않아요.")
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValidationError(f"{field}에는 숫자를 입력해 주세요.") from exc
    if not math.isfinite(number) or number < low or number > high:
        raise ValidationError(f"{field}은(는) {low:g}~{high:g} 사이로 입력해 주세요.")
    return round(number, 1)


def string_list(value, max_items: int, limit: int, field: str) -> list[str]:
    if value in (None, ""):
        return []
    if isinstance(value, str):
        value = [v for v in re.split(r"[,\n]", value)]
    if not isinstance(value, list):
        raise ValidationError(f"{field} 값이 올바르지 않아요.")
    out: list[str] = []
    for entry in value:
        entry = text(entry, limit, field)
        if entry and entry not in out:
            out.append(entry)
    if len(out) > max_items:
        raise ValidationError(f"{field}은(는) {max_items}개까지 입력할 수 있어요.")
    return out


# ---------------------------------------------------------------- settings

GOAL_TYPES = ["", "체중 감량", "근육 증가", "체력 향상", "건강 유지", "식습관 개선", "기타"]
EXPERIENCE = ["", "처음이에요", "6개월 미만", "6개월~2년", "2년 이상"]
EQUIPMENT = ["맨몸", "요가 매트", "덤벨", "밴드", "케틀벨", "헬스장", "걷기·달리기 공간", "자전거"]


def default_settings() -> dict:
    return {
        "goal_type": "",
        "goal_note": "",
        "experience": "",
        "equipment": [],
        "equipment_note": "",
        "workouts_per_week": None,
        "minutes_per_session": None,
        "food_preferences": "",
        "allergies": [],
        "avoid_foods": [],
        "targets": {"kcal": None, "carbs_g": None, "protein_g": None, "fat_g": None},
        "targets_confirmed_at": None,
        "timezone": "",
        "notifications": True,
        # Current body status, entered by the user (used for workout routine suggestions).
        "body": {"height_cm": None, "weight_kg": None, "skeletal_muscle_kg": None, "condition_note": ""},
        "body_updated_at": None,
    }


def default_body() -> dict:
    return dict(default_settings()["body"])


def normalize_body(raw, previous: dict) -> dict:
    if not isinstance(raw, dict):
        raise ValidationError("몸 상태 값이 올바르지 않아요.")
    out = {**default_body(), **(previous or {})}
    if "height_cm" in raw:
        out["height_cm"] = optional_number(raw["height_cm"], 100, 230, "키(cm)")
    if "weight_kg" in raw:
        out["weight_kg"] = optional_number(raw["weight_kg"], 25, 300, "몸무게(kg)")
    if "skeletal_muscle_kg" in raw:
        out["skeletal_muscle_kg"] = optional_number(raw["skeletal_muscle_kg"], 5, 90, "골격근량(kg)")
    if "condition_note" in raw:
        out["condition_note"] = text(raw["condition_note"], 200, "운동 시 주의할 점")
    if out["skeletal_muscle_kg"] is not None and out["weight_kg"] is not None and out["skeletal_muscle_kg"] >= out["weight_kg"]:
        raise ValidationError("골격근량은 몸무게보다 작아야 해요. 값을 다시 확인해 주세요.")
    return out


def body_metrics(settings: dict) -> dict:
    """The body values plus numbers the app calculates from them (not AI estimates)."""
    body = {**default_body(), **(settings.get("body") or {})}
    h, w, m = body["height_cm"], body["weight_kg"], body["skeletal_muscle_kg"]
    return {
        "height_cm": h,
        "weight_kg": w,
        "skeletal_muscle_kg": m,
        "condition_note": body["condition_note"],
        "bmi": round(w / (h / 100) ** 2, 1) if h and w else None,
        "muscle_ratio_pct": round(m / w * 100, 1) if m and w else None,
        "updated_at": settings.get("body_updated_at"),
        "missing": [label for key, label in (("height_cm", "키"), ("weight_kg", "몸무게"), ("skeletal_muscle_kg", "골격근량")) if body[key] is None],
    }


def normalize_settings(raw: dict, previous: dict, now: str) -> dict:
    if not isinstance(raw, dict):
        raise ValidationError("설정 값이 올바르지 않아요.")
    merged = {**default_settings(), **previous}
    out = dict(merged)
    if "goal_type" in raw:
        if raw["goal_type"] not in GOAL_TYPES:
            raise ValidationError("목표 종류를 다시 선택해 주세요.")
        out["goal_type"] = raw["goal_type"]
    if "goal_note" in raw:
        out["goal_note"] = text(raw["goal_note"], 200, "목표 메모")
    if "experience" in raw:
        if raw["experience"] not in EXPERIENCE:
            raise ValidationError("운동 경험을 다시 선택해 주세요.")
        out["experience"] = raw["experience"]
    if "equipment" in raw:
        items = string_list(raw["equipment"], len(EQUIPMENT), 30, "장비")
        if any(item not in EQUIPMENT for item in items):
            raise ValidationError("장비 목록을 다시 선택해 주세요.")
        out["equipment"] = items
    if "equipment_note" in raw:
        out["equipment_note"] = text(raw["equipment_note"], 120, "기타 장비")
    if "workouts_per_week" in raw:
        value = optional_number(raw["workouts_per_week"], 0, 14, "주당 운동 횟수")
        out["workouts_per_week"] = None if value is None else int(value)
    if "minutes_per_session" in raw:
        value = optional_number(raw["minutes_per_session"], 5, 240, "1회 운동 시간(분)")
        out["minutes_per_session"] = None if value is None else int(value)
    if "food_preferences" in raw:
        out["food_preferences"] = text(raw["food_preferences"], 300, "식사 선호")
    if "allergies" in raw:
        out["allergies"] = string_list(raw["allergies"], 20, 40, "알레르기")
    if "avoid_foods" in raw:
        out["avoid_foods"] = string_list(raw["avoid_foods"], 30, 40, "피하고 싶은 음식")
    if "targets" in raw:
        targets_raw = raw["targets"] or {}
        if not isinstance(targets_raw, dict):
            raise ValidationError("영양 목표 값이 올바르지 않아요.")
        targets = {
            "kcal": optional_number(targets_raw.get("kcal"), 1000, 6000, "열량 목표(kcal)"),
            "carbs_g": optional_number(targets_raw.get("carbs_g"), 20, 900, "탄수화물 목표(g)"),
            "protein_g": optional_number(targets_raw.get("protein_g"), 20, 400, "단백질 목표(g)"),
            "fat_g": optional_number(targets_raw.get("fat_g"), 10, 300, "지방 목표(g)"),
        }
        if targets != merged["targets"]:
            out["targets_confirmed_at"] = now if any(v is not None for v in targets.values()) else None
        out["targets"] = targets
    if "timezone" in raw:
        tz = text(raw["timezone"], 64, "시간대")
        if tz and not re.match(r"^[A-Za-z_]+(/[A-Za-z0-9_+\-]+){0,2}$", tz):
            raise ValidationError("시간대 값이 올바르지 않아요.")
        out["timezone"] = tz
    if "notifications" in raw:
        out["notifications"] = bool(raw["notifications"])
    if "body" in raw:
        previous_body = {**default_body(), **(merged.get("body") or {})}
        body = normalize_body(raw["body"], previous_body)
        if body != previous_body:
            out["body_updated_at"] = now
        out["body"] = body
    return out


def workout_profile(settings: dict) -> dict:
    """What the AI sees for a workout routine (no account identifiers)."""
    metrics = body_metrics(settings)
    return {
        "goal": settings.get("goal_type") or None,
        "goal_note": settings.get("goal_note") or None,
        "exercise_experience": settings.get("experience") or None,
        "equipment": settings.get("equipment") or [],
        "equipment_note": settings.get("equipment_note") or None,
        "workouts_per_week": settings.get("workouts_per_week"),
        "minutes_per_session": settings.get("minutes_per_session"),
        "body": {
            "height_cm": metrics["height_cm"],
            "weight_kg": metrics["weight_kg"],
            "skeletal_muscle_kg": metrics["skeletal_muscle_kg"],
            "bmi_calculated": metrics["bmi"],
            "skeletal_muscle_ratio_pct_calculated": metrics["muscle_ratio_pct"],
        },
        "cautions_from_user": metrics["condition_note"] or None,
    }


def ai_profile(settings: dict) -> dict:
    """The part of the settings that the AI sees (no account identifiers)."""
    return {
        "goal": settings.get("goal_type") or None,
        "goal_note": settings.get("goal_note") or None,
        "exercise_experience": settings.get("experience") or None,
        "equipment": settings.get("equipment") or [],
        "workouts_per_week": settings.get("workouts_per_week"),
        "minutes_per_session": settings.get("minutes_per_session"),
        "food_preferences": settings.get("food_preferences") or None,
        "allergies": settings.get("allergies") or [],
        "avoid_foods": settings.get("avoid_foods") or [],
        "nutrition_targets": settings.get("targets"),
    }


# ---------------------------------------------------------------- meals

MEAL_TYPES = {"breakfast": "아침", "lunch": "점심", "dinner": "저녁", "snack": "간식"}
NUTRIENTS = [("kcal", "열량", "kcal"), ("carbs_g", "탄수화물", "g"), ("protein_g", "단백질", "g"), ("fat_g", "지방", "g")]
COUNTED_STATUSES = {"ai", "user"}


def parse_items(value) -> list[dict]:
    if not isinstance(value, list) or not value:
        raise ValidationError("음식을 한 가지 이상 입력해 주세요.")
    if len(value) > 12:
        raise ValidationError("한 끼에 음식은 12개까지 기록할 수 있어요.")
    items = []
    for raw in value:
        if not isinstance(raw, dict):
            raise ValidationError("음식 정보가 올바르지 않아요.")
        items.append({
            "name": text(raw.get("name"), 80, "음식 이름", required=True),
            "amount": text(raw.get("amount"), 80, "섭취량"),
            "cooking": text(raw.get("cooking"), 40, "조리법"),
        })
    return items


def parse_manual_nutrition(value) -> dict | None:
    """User-entered totals. Fields left blank stay unknown (None), never 0."""
    if value in (None, {}):
        return None
    if not isinstance(value, dict):
        raise ValidationError("영양 정보 값이 올바르지 않아요.")
    total = {
        "kcal": optional_number(value.get("kcal"), 0, 6000, "열량"),
        "carbs_g": optional_number(value.get("carbs_g"), 0, 900, "탄수화물"),
        "protein_g": optional_number(value.get("protein_g"), 0, 500, "단백질"),
        "fat_g": optional_number(value.get("fat_g"), 0, 400, "지방"),
    }
    if all(v is None for v in total.values()):
        return None
    return {"source": "user", "total": total, "items": None}


def total_from_items(items: list[dict]) -> dict:
    return {key: round(sum(item[key] for item in items), 1) for key, _, _ in NUTRIENTS}


def meal_signature(meal_items: list[dict]) -> list:
    return [[i["name"], i.get("amount", ""), i.get("cooking", "")] for i in meal_items]


def day_totals(meals: list[dict]) -> dict:
    counted = [m for m in meals if m["nutrition_status"] in COUNTED_STATUSES and m.get("nutrition")]
    uncounted = [m for m in meals if m not in counted]
    totals: dict[str, dict] = {}
    for key, _, _ in NUTRIENTS:
        values = [m["nutrition"]["total"].get(key) for m in counted]
        known = [v for v in values if v is not None]
        totals[key] = {
            "value": round(sum(known), 1) if known else None,
            "included": len(known),
            "missing": len(meals) - len(known),
        }
    return {"totals": totals, "counted": len(counted), "uncounted": len(uncounted), "meal_count": len(meals)}


def evaluate_day(meals: list[dict], settings: dict, complete: bool) -> dict:
    """Deterministic, data-grounded evaluation of one day's records."""
    agg = day_totals(meals)
    targets = settings.get("targets") or {}
    has_targets = any(targets.get(key) is not None for key, _, _ in NUTRIENTS)
    progress = {}
    for key, label, unit in NUTRIENTS:
        target = targets.get(key)
        value = agg["totals"][key]["value"]
        progress[key] = {
            "label": label,
            "unit": unit,
            "value": value,
            "target": target,
            "pct": round(value / target * 100) if (target and value is not None) else None,
        }

    messages: list[dict] = []
    questions: list[dict] = []
    incomplete_data = agg["uncounted"] > 0 or any(agg["totals"][k]["missing"] for k, _, _ in NUTRIENTS)

    if not meals:
        return {**agg, "progress": progress, "has_targets": has_targets, "complete": complete,
                "incomplete_data": False, "state": "empty", "messages": [], "questions": []}

    if agg["uncounted"]:
        messages.append({"level": "info", "text": f"분석되지 않은 기록 {agg['uncounted']}개는 합계에 포함되지 않았어요. 합계가 불완전해요."})
    if not has_targets:
        messages.append({"level": "info", "text": "영양 목표가 설정되지 않아 달성률과 부족·과다 평가는 하지 않아요."})
        questions.append({"id": "set_targets", "text": "하루 영양 목표를 정하면 기록과 비교해 드릴 수 있어요. 지금 설정할까요?"})

    if agg["counted"] == 0:
        state = "unanalyzed"
    elif not complete:
        state = "partial"
        messages.append({"level": "info", "text": f"지금까지 분석된 {agg['counted']}끼 기준이에요. 하루 전체 평가는 이 날의 식사를 모두 기록했다고 표시한 뒤 할게요."})
        questions.append({"id": "mark_complete", "text": "이 날의 식사를 모두 기록했나요?"})
        for key, label, unit in NUTRIENTS:
            p = progress[key]
            if p["target"] is None or p["value"] is None:
                continue
            remaining = p["target"] - p["value"]
            if remaining > 0:
                messages.append({"level": "info", "text": f"{label}은(는) 목표까지 약 {round(remaining):,}{unit} 남았어요. (현재 {round(p['value']):,}/{round(p['target']):,}{unit})"})
            else:
                messages.append({"level": "info", "text": f"{label}은(는) 지금까지 목표의 {p['pct']}%예요."})
    elif incomplete_data:
        state = "complete_unanalyzed"
        messages.append({"level": "info", "text": "분석되지 않은 기록이 있어 하루 전체 평가는 보류했어요. 분석을 마치면 평가할 수 있어요."})
    else:
        state = "complete"
        for key, label, unit in NUTRIENTS:
            p = progress[key]
            if p["target"] is None or p["value"] is None:
                continue
            numbers = f"({round(p['value']):,}/{round(p['target']):,}{unit}, {p['pct']}%)"
            if p["pct"] < 80:
                messages.append({"level": "low", "text": f"{label}이(가) 목표보다 부족했어요 {numbers}"})
            elif p["pct"] > 120:
                messages.append({"level": "high", "text": f"{label}이(가) 목표보다 많았어요 {numbers}"})
            else:
                messages.append({"level": "ok", "text": f"{label}은(는) 목표 범위에 가까워요 {numbers}"})

    return {**agg, "progress": progress, "has_targets": has_targets, "complete": complete,
            "incomplete_data": incomplete_data, "state": state, "messages": messages, "questions": questions}


def date_range(start: str, end: str, max_days: int = 400) -> list[str]:
    a, b = date.fromisoformat(start), date.fromisoformat(end)
    if b < a:
        raise ValidationError("기간의 시작일이 종료일보다 늦어요.")
    if (b - a).days > max_days:
        raise ValidationError(f"기간은 {max_days}일 이내로 선택해 주세요.")
    return [(a + timedelta(days=i)).isoformat() for i in range((b - a).days + 1)]
