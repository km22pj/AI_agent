"""AI food recognition, nutrition estimation and day coaching.

The provider is Google Gemini by default (Claude is still available via
HEALTHLINGO_AI_PROVIDER=anthropic). Requests are built in a provider-neutral
shape and each client converts them to its own API.

Every response is requested as structured JSON and then validated here again:
numbers must be finite and in a plausible range, item counts must line up, and
totals are always recomputed in code instead of trusting the model.
"""
from __future__ import annotations

import base64
import json
import logging
import math
import os
import re
import threading
import time

log = logging.getLogger("healthlingo.ai")

FALLBACK_BETA = "server-side-fallback-2026-07-01"


class AIError(Exception):
    def __init__(self, message: str, retryable: bool = True, code: str = "ai_error"):
        super().__init__(message)
        self.message = message
        self.retryable = retryable
        self.code = code


class AINotConfigured(AIError):
    def __init__(self, key_name: str = "GEMINI_API_KEY"):
        super().__init__(
            f"AI 분석이 아직 설정되지 않았어요. 서버에 {key_name}를 설정하면 사용할 수 있어요. 지금은 직접 입력으로 기록할 수 있어요.",
            retryable=False,
            code="ai_not_configured",
        )


# ---------------------------------------------------------------- JSON schemas

RECOGNITION_SCHEMA = {
    "type": "object",
    "properties": {
        "status": {"type": "string", "enum": ["recognized", "not_food", "unclear"]},
        "message": {"type": "string"},
        "items": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "estimated_amount": {"type": "string"},
                    "amount_certain": {"type": "boolean"},
                    "cooking_method": {"type": "string"},
                    "cooking_certain": {"type": "boolean"},
                    "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
                },
                "required": ["name", "estimated_amount", "amount_certain", "cooking_method", "cooking_certain", "confidence"],
                "additionalProperties": False,
            },
        },
        "questions": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["status", "message", "items", "questions"],
    "additionalProperties": False,
}

NUTRITION_SCHEMA = {
    "type": "object",
    "properties": {
        "items": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "kcal": {"type": "number"},
                    "carbs_g": {"type": "number"},
                    "protein_g": {"type": "number"},
                    "fat_g": {"type": "number"},
                },
                "required": ["name", "kcal", "carbs_g", "protein_g", "fat_g"],
                "additionalProperties": False,
            },
        },
        "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
        "assumptions": {"type": "array", "items": {"type": "string"}},
        "feedback": {"type": "string"},
        "questions": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["items", "confidence", "assumptions", "feedback", "questions"],
    "additionalProperties": False,
}

COACH_SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {"type": "string"},
        "observations": {"type": "array", "items": {"type": "string"}},
        "recommendations": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "food": {"type": "string"},
                    "reason": {"type": "string"},
                    "ingredients": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["food", "reason", "ingredients"],
                "additionalProperties": False,
            },
        },
        "questions": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["summary", "observations", "recommendations", "questions"],
    "additionalProperties": False,
}

INTENSITIES = ["가볍게", "보통", "조금 힘들게"]

WORKOUT_SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {"type": "string"},
        "sessions": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "weekday": {"type": "string", "enum": ["월", "화", "수", "목", "금", "토", "일"]},
                    "title": {"type": "string"},
                    "focus": {"type": "string"},
                    "minutes": {"type": "integer"},
                    "intensity": {"type": "string", "enum": INTENSITIES},
                    "exercises": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "name": {"type": "string"},
                                "amount": {"type": "string"},
                                "weight_kg": {"type": "number"},
                                "weight_note": {"type": "string"},
                                "tip": {"type": "string"},
                            },
                            "required": ["name", "amount", "weight_kg", "weight_note", "tip"],
                            "additionalProperties": False,
                        },
                    },
                },
                "required": ["weekday", "title", "focus", "minutes", "intensity", "exercises"],
                "additionalProperties": False,
            },
        },
        "cautions": {"type": "array", "items": {"type": "string"}},
        "questions": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["summary", "sessions", "cautions", "questions"],
    "additionalProperties": False,
}

# ---------------------------------------------------------------- prompts

RECOGNITION_SYSTEM = """You identify the foods in one meal photo for a Korean food-journal app.
Write every human-readable string in Korean.

- status: "recognized" if at least one food or drink is visible, "not_food" if the photo shows no food or drink, "unclear" if it is too blurry, dark or cropped to tell.
- message: one short sentence for the user explaining the result.
- items: each distinct food or drink separately, with a common Korean name. Empty unless status is "recognized".
- estimated_amount: a human-friendly guess such as "밥 1공기(약 200g)". Set amount_certain to false whenever the portion cannot be judged reliably from the photo (no size reference, hidden depth, shared dish).
- cooking_method: such as 구이, 튀김, 볶음, 찜, 생것. Set cooking_certain to false when it cannot be seen (hidden oil, sauces, dressing).
- questions: up to 4 short questions about facts that would change the nutrition estimate but cannot be seen, such as the amount actually eaten, sauces, oil, sugar in drinks.
Do not estimate nutrition and do not judge foods as good or bad."""

NUTRITION_SYSTEM = """You estimate nutrition for a meal that the user has already confirmed, for a Korean food-journal app.
Write every human-readable string in Korean.

- Return exactly one entry in items per confirmed food, in the same order, with kcal, carbs_g, protein_g and fat_g for the stated amount and cooking method, based on typical food composition data.
- When an amount or ingredient is vague, assume a typical single serving and state that assumption in assumptions.
- feedback: 1-3 friendly sentences about this meal only, connected to the user's goals and nutrition targets when they are given. The day context covers only the meals recorded so far; unless day_complete is true, do not state conclusions about the whole day's intake.
- questions: ask only when missing information would materially change the estimate (up to 3).
- Never suggest skipping meals, fasting, very low calorie intake, or exercise to burn off or make up for food. Do not label foods as good or bad."""

COACH_SYSTEM = """You give gentle, personalized meal coaching for one day in a Korean food-journal app.
Write every human-readable string in Korean.

You receive the user's profile (goals, food preferences, allergies, foods to avoid, nutrition targets), that day's meals with estimated nutrition (some may be unanalyzed and excluded from totals), totals computed by the app, and whether the user marked the day as fully recorded.

- Base every judgment on the provided totals and targets. Mention numbers when you make a judgment.
- If day_complete is false, talk about the meals recorded so far and the remaining meals; never state that the whole day was deficient or excessive.
- If a target is missing, do not invent one; suggest setting it instead.
- If some meals are unanalyzed, say the totals are incomplete.
- recommendations: 2-4 concrete foods for the next meal (or tomorrow if the day is complete), each with a reason tied to the data. List the main ingredients of each food in ingredients. Never include the user's allergens or foods they avoid, and respect their preferences.
- questions: ask for whatever information is missing to give better advice (up to 3).
- Never suggest skipping meals, fasting, very low calorie intake, or punishment/compensation exercise. Do not label foods as good or bad."""

WORKOUT_SYSTEM = """You suggest a gentle, personalized weekly workout routine in a Korean health-journal app.
Write every human-readable string in Korean.

You receive the user's current body values (height, weight, skeletal muscle mass if known, and BMI / skeletal-muscle ratio that the app calculated), goal, exercise experience, available equipment, how many times a week and how many minutes per session they can work out, and any cautions they wrote (pain, injuries).

- summary: 2-3 sentences explaining why this routine fits, tied to the provided numbers and goal. Do not diagnose, and do not label the body as good/bad or comment on appearance. BMI does not reflect muscle mass; say so if you use it.
- sessions: a Monday-to-Sunday week. Give exactly one session for each day in training_weekdays (weekday = that day, e.g. "월") and none for rest_weekdays. title: a short focus title such as "하체·코어".
- Balance the week: rotate muscle groups so the same main muscle group is not trained hard on two consecutive training days, mix strength, cardio and mobility, and put harder sessions after rest days.
- Each session's minutes must not exceed minutes_per_session if given (otherwise 30). Each session starts with a warm-up and ends with a cool-down/stretch inside its exercises.
- Only use the listed equipment (bodyweight is always allowed). Match difficulty to experience; for beginners or when experience is unknown, choose easy, low-impact movements.
- If the user wrote cautions, avoid movements that load those areas and mention it.
- amount: sets x reps, or minutes, e.g. "2세트 × 10회", "5분". tip: one short form or safety tip.
- weight_kg: a concrete starting weight in kg for every exercise that uses an external load (dumbbell, kettlebell, barbell, machine). Base it on body weight, skeletal muscle mass, experience and goal; start conservatively, choosing a weight the user can finish with 2-3 reps left. Use 0 for bodyweight moves, stretching, walking/cardio and bands. Only use loads with equipment the user has.
- weight_note: what the weight means or how to adjust it, e.g. "한 손 기준", "양손 합계", "맨몸", "밴드 약한 강도", "마지막 2회가 힘들면 적당해요".
- If progress_review (last week's completion) and previous_plan are given, judge yourself whether to keep, increase or decrease from the completion data: do not apply a fixed rule, and do not guess reasons for missed workouts. Change intensity by at most one level and loads by at most 10 %. Mention the decision in summary.
- cautions: 2-4 short safety notes (e.g. stop if there is pain, consult a professional for medical conditions).
- questions: always an empty list (the app does not show follow-up questions for routines).
- Never suggest punishment or compensation exercise (e.g. to burn off food), exercising through pain, extreme volume, or skipping meals."""

TARGET_EXPLAIN_SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {"type": "string"},
        "notes": {"type": "array", "items": {"type": "string"}},
        "caution": {"type": "string"},
    },
    "required": ["summary", "notes", "caution"],
    "additionalProperties": False,
}

TARGET_EXPLAIN_SYSTEM = """You explain daily nutrition targets that a Korean health app has ALREADY calculated with fixed formulas.
Write every human-readable string in Korean, in a gentle tone.

You receive the targets (kcal, carbohydrate, protein, fat in grams), the calculation steps (resting energy by the Mifflin-St Jeor equation, physical activity level for daily life, planned exercise energy, goal adjustment, protein per kg, macro shares), and the user's goal and free-text notes.

- Never change, recalculate or invent numbers. Only use numbers that appear in the input.
- summary: 2-3 sentences on how the targets follow from the user's settings.
- notes: up to 3 short practical notes on using the targets (they are estimates; adjust with how the body responds).
- caution: if the user's free-text notes or goal note mention pregnancy, breastfeeding, a disease, medication, an eating disorder or a doctor's diet instructions, say in one sentence that the general targets may not fit and a professional should be consulted. Otherwise an empty string.
- Never recommend fasting, skipping meals, or very low calorie intake."""

# ---------------------------------------------------------------- safety helpers

UNSAFE_PATTERNS = [
    "굶", "단식", "금식", "끼니를 거르", "식사를 거르", "식사를 건너", "끼니를 건너", "벌칙", "만회", "태워야", "태우세요",
    "태워 보", "칼로리를 태", "보상 운동", "죄책감", "1000kcal 미만", "1,000kcal 미만", "800kcal",
]


def _norm(text: str) -> str:
    return re.sub(r"\s+", "", str(text)).lower()


def strip_unsafe_sentences(text: str) -> tuple[str, bool]:
    """Remove sentences that push restriction or compensatory exercise."""
    if not text:
        return text, False
    parts = re.split(r"(?<=[.!?。])\s+|\n+", text)
    kept = [p for p in parts if not any(pat in p for pat in UNSAFE_PATTERNS)]
    return " ".join(p.strip() for p in kept if p.strip()), len(kept) != len(parts)


def filter_recommendations(recs: list[dict], blocked_terms: list[str]) -> tuple[list[dict], int]:
    blocked = [_norm(t) for t in blocked_terms if _norm(t)]
    kept: list[dict] = []
    removed = 0
    for rec in recs:
        haystack = _norm(rec.get("food", "") + " " + " ".join(rec.get("ingredients", [])) + " " + rec.get("reason", ""))
        unsafe = any(pat in rec.get("reason", "") or pat in rec.get("food", "") for pat in UNSAFE_PATTERNS)
        if unsafe or any(b in haystack for b in blocked):
            removed += 1
            continue
        kept.append(rec)
    return kept, removed


# ---------------------------------------------------------------- validation

def _invalid(detail: str) -> AIError:
    log.warning("AI response failed validation: %s", detail)
    return AIError("AI 응답 형식이 올바르지 않아 결과를 사용하지 않았어요. 다시 시도해 주세요.", retryable=True, code="ai_invalid")


def _clean_str(value, limit: int, field: str, allow_empty: bool = True) -> str:
    if not isinstance(value, str):
        raise _invalid(f"{field} is not a string")
    value = value.strip()
    if not value and not allow_empty:
        raise _invalid(f"{field} is empty")
    return value[:limit]


def _clean_str_list(value, max_items: int, limit: int, field: str) -> list[str]:
    if not isinstance(value, list):
        raise _invalid(f"{field} is not a list")
    out = []
    for entry in value[:max_items]:
        text = _clean_str(entry, limit, field)
        if text:
            out.append(text)
    return out


def _number(value, low: float, high: float, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise _invalid(f"{field} is not a number")
    value = float(value)
    if not math.isfinite(value) or value < low or value > high:
        raise _invalid(f"{field} out of range")
    return round(value, 1)


def validate_recognition(data) -> dict:
    if not isinstance(data, dict):
        raise _invalid("recognition is not an object")
    status = data.get("status")
    if status not in {"recognized", "not_food", "unclear"}:
        raise _invalid("bad status")
    items = []
    raw_items = data.get("items")
    if not isinstance(raw_items, list):
        raise _invalid("items not list")
    for raw in raw_items[:12]:
        if not isinstance(raw, dict):
            raise _invalid("item not object")
        if raw.get("confidence") not in {"high", "medium", "low"}:
            raise _invalid("bad confidence")
        if not isinstance(raw.get("amount_certain"), bool) or not isinstance(raw.get("cooking_certain"), bool):
            raise _invalid("certainty flags")
        items.append({
            "name": _clean_str(raw.get("name"), 80, "name", allow_empty=False),
            "amount": _clean_str(raw.get("estimated_amount"), 80, "estimated_amount"),
            "amount_certain": raw["amount_certain"],
            "cooking": _clean_str(raw.get("cooking_method"), 40, "cooking_method"),
            "cooking_certain": raw["cooking_certain"],
            "confidence": raw["confidence"],
        })
    if status == "recognized" and not items:
        status = "unclear"
    if status != "recognized":
        items = []
    return {
        "status": status,
        "message": _clean_str(data.get("message"), 200, "message"),
        "items": items,
        "questions": _clean_str_list(data.get("questions"), 4, 160, "questions"),
    }


def validate_nutrition(data, expected_items: list[dict]) -> dict:
    if not isinstance(data, dict):
        raise _invalid("nutrition not object")
    raw_items = data.get("items")
    if not isinstance(raw_items, list) or len(raw_items) != len(expected_items):
        raise _invalid("item count mismatch")
    items = []
    for idx, raw in enumerate(raw_items):
        if not isinstance(raw, dict):
            raise _invalid("item not object")
        entry = {
            "name": expected_items[idx]["name"],
            "kcal": _number(raw.get("kcal"), 0, 4000, "kcal"),
            "carbs_g": _number(raw.get("carbs_g"), 0, 600, "carbs_g"),
            "protein_g": _number(raw.get("protein_g"), 0, 400, "protein_g"),
            "fat_g": _number(raw.get("fat_g"), 0, 400, "fat_g"),
        }
        # Energy from macros should roughly agree with the stated kcal.
        macro_kcal = entry["carbs_g"] * 4 + entry["protein_g"] * 4 + entry["fat_g"] * 9
        if abs(macro_kcal - entry["kcal"]) > max(120.0, entry["kcal"] * 0.45):
            raise _invalid("kcal does not match macros")
        items.append(entry)
    if data.get("confidence") not in {"high", "medium", "low"}:
        raise _invalid("bad confidence")
    feedback, _ = strip_unsafe_sentences(_clean_str(data.get("feedback"), 600, "feedback"))
    return {
        "items": items,
        "confidence": data["confidence"],
        "assumptions": _clean_str_list(data.get("assumptions"), 6, 200, "assumptions"),
        "feedback": feedback,
        "questions": _clean_str_list(data.get("questions"), 3, 160, "questions"),
    }


def validate_coach(data, blocked_terms: list[str]) -> dict:
    if not isinstance(data, dict):
        raise _invalid("coach not object")
    recs_raw = data.get("recommendations")
    if not isinstance(recs_raw, list):
        raise _invalid("recommendations not list")
    recs = []
    for raw in recs_raw[:5]:
        if not isinstance(raw, dict):
            raise _invalid("rec not object")
        recs.append({
            "food": _clean_str(raw.get("food"), 80, "food", allow_empty=False),
            "reason": _clean_str(raw.get("reason"), 300, "reason", allow_empty=False),
            "ingredients": _clean_str_list(raw.get("ingredients"), 12, 40, "ingredients"),
        })
    recs, removed = filter_recommendations(recs, blocked_terms)
    summary, _ = strip_unsafe_sentences(_clean_str(data.get("summary"), 500, "summary"))
    observations = []
    for text in _clean_str_list(data.get("observations"), 5, 300, "observations"):
        cleaned, _ = strip_unsafe_sentences(text)
        if cleaned:
            observations.append(cleaned)
    return {
        "summary": summary,
        "observations": observations,
        "recommendations": recs,
        "filtered_recommendations": removed,
        "questions": _clean_str_list(data.get("questions"), 3, 160, "questions"),
    }


NUMBER_RE = re.compile(r"\d[\d,]*(?:\.\d+)?")


def _canon(number) -> str:
    """1850.0 / "1,850" / "1850" -> "1850";  1.40 -> "1.4"."""
    return "%g" % float(str(number).replace(",", ""))


def _numbers_in(value) -> set[str]:
    """All numbers in a nested structure, normalized with _canon (plus their rounded values)."""
    out: set[str] = set()
    if isinstance(value, dict):
        for v in value.values():
            out |= _numbers_in(v)
    elif isinstance(value, (list, tuple)):
        for v in value:
            out |= _numbers_in(v)
    elif isinstance(value, bool):
        pass
    elif isinstance(value, (int, float)):
        out |= {_canon(value), _canon(round(value))}
    elif isinstance(value, str):
        out |= {_canon(m) for m in NUMBER_RE.findall(value)}
    return out


def _drop_invented_numbers(text: str, allowed: set[str]) -> tuple[str, bool]:
    """Remove sentences that contain a number not present in the calculation input."""
    parts = re.split(r"(?<=[.!?。])\s+|\n+", text or "")
    kept = [p for p in parts if not ({_canon(m) for m in NUMBER_RE.findall(p)} - allowed)]
    return " ".join(p.strip() for p in kept if p.strip()), len(kept) != len(parts)


def validate_target_explanation(data, payload: dict) -> dict:
    if not isinstance(data, dict):
        raise _invalid("explanation not object")
    allowed = _numbers_in(payload) | {"1", "2", "3"}
    dropped = False
    summary, d = _drop_invented_numbers(strip_unsafe_sentences(_clean_str(data.get("summary"), 500, "summary"))[0], allowed)
    dropped |= d
    notes = []
    for text in _clean_str_list(data.get("notes"), 3, 200, "notes"):
        cleaned, d = _drop_invented_numbers(strip_unsafe_sentences(text)[0], allowed)
        dropped |= d
        if cleaned:
            notes.append(cleaned)
    caution, d = _drop_invented_numbers(_clean_str(data.get("caution"), 200, "caution"), allowed)
    dropped |= d
    return {"summary": summary, "notes": notes, "caution": caution, "dropped_sentences": dropped}


MAX_LOAD_KG = 150


def _load_kg(value, body_weight: float | None) -> tuple[float | None, bool]:
    """(weight or None for no external load, True if an unsafe/invalid weight was dropped)."""
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return None, value is not None
    if value <= 0:
        return None, value < 0
    limit = min(MAX_LOAD_KG, body_weight * 1.5) if body_weight else MAX_LOAD_KG
    if value > limit:
        return None, True
    return round(float(value) * 2) / 2, False  # nearest 0.5kg, like real plates and dumbbells


WEEKDAY_NAMES = ["월", "화", "수", "목", "금", "토", "일"]


def validate_workout(data, max_minutes: int | None = None, body_weight: float | None = None,
                     training_days: list[int] | None = None) -> dict:
    """training_days (Mon=0): when given, the plan must have exactly one session on each of them."""
    if not isinstance(data, dict):
        raise _invalid("workout not object")
    raw_sessions = data.get("sessions")
    if not isinstance(raw_sessions, list) or not raw_sessions:
        raise _invalid("sessions missing")
    sessions = []
    removed = 0
    loads_dropped = 0
    for raw in raw_sessions[:7]:
        if not isinstance(raw, dict):
            raise _invalid("session not object")
        intensity = raw.get("intensity")
        if intensity not in INTENSITIES:
            raise _invalid("intensity not allowed")
        minutes = int(_number(raw.get("minutes"), 5, 240, "minutes"))
        if max_minutes:
            minutes = min(minutes, max_minutes)
        exercises_raw = raw.get("exercises")
        if not isinstance(exercises_raw, list):
            raise _invalid("exercises not list")
        exercises = []
        for ex in exercises_raw[:10]:
            if not isinstance(ex, dict):
                raise _invalid("exercise not object")
            weight, dropped = _load_kg(ex.get("weight_kg"), body_weight)
            loads_dropped += dropped
            note = ex.get("weight_note")
            item = {
                "name": _clean_str(ex.get("name"), 60, "name", allow_empty=False),
                "amount": _clean_str(ex.get("amount"), 40, "amount"),
                "weight_kg": weight,
                "weight_note": _clean_str(note, 60, "weight_note") if isinstance(note, str) else "",
                "tip": _clean_str(ex.get("tip"), 160, "tip"),
            }
            if any(pat in item["name"] or pat in item["tip"] for pat in UNSAFE_PATTERNS):
                removed += 1
                continue
            exercises.append(item)
        if not exercises:
            continue
        weekday = WEEKDAY_NAMES.index(raw["weekday"]) if raw.get("weekday") in WEEKDAY_NAMES else None
        if training_days is not None:
            if weekday is None:
                raise _invalid("weekday missing")
            if weekday not in training_days or any(s["weekday"] == weekday for s in sessions):
                continue  # a rest day or a second session on the same day
        sessions.append({
            "weekday": weekday,
            "title": _clean_str(raw.get("title"), 40, "title", allow_empty=False),
            "focus": _clean_str(raw.get("focus"), 60, "focus"),
            "minutes": minutes,
            "intensity": intensity,
            "exercises": exercises,
        })
    if not sessions:
        raise _invalid("no usable sessions")
    if training_days is not None:
        if sorted(s["weekday"] for s in sessions) != sorted(training_days):
            raise _invalid("sessions do not cover the training days")
        sessions.sort(key=lambda s: s["weekday"])
    summary, _ = strip_unsafe_sentences(_clean_str(data.get("summary"), 500, "summary"))
    cautions = []
    for text in _clean_str_list(data.get("cautions"), 5, 200, "cautions"):
        cleaned, _ = strip_unsafe_sentences(text)
        if cleaned:
            cautions.append(cleaned)
    return {
        "summary": summary,
        "sessions": sessions,
        "cautions": cautions,
        "filtered_exercises": removed,
        "dropped_weights": loads_dropped,
        "questions": _clean_str_list(data.get("questions"), 3, 160, "questions"),
        "format": 3,  # 2 = exercises carry weight_kg / weight_note, 3 = sessions are placed on weekdays
    }


# ---------------------------------------------------------------- client

class AnthropicJSONClient:
    """Calls the Claude Messages API and returns the structured JSON payload."""

    def __init__(self, model: str, effort: str):
        import anthropic  # imported lazily so the app runs without the SDK installed

        self.anthropic = anthropic
        self.model = model
        self.effort = effort
        self.client = anthropic.Anthropic(timeout=120.0, max_retries=1)

    @staticmethod
    def _blocks(content: list[dict]) -> list[dict]:
        blocks = []
        for part in content:
            if part["type"] == "image":
                blocks.append({"type": "image", "source": {"type": "base64", "media_type": part["mime_type"], "data": part["data"]}})
            else:
                blocks.append({"type": "text", "text": part["text"]})
        return blocks

    def complete_json(self, system: str, content: list[dict], schema: dict, task: str | None = None) -> dict:
        a = self.anthropic
        try:
            response = self.client.beta.messages.create(
                model=self.model,
                max_tokens=16000,
                betas=[FALLBACK_BETA],
                fallbacks="default",
                system=system,
                output_config={"effort": self.effort, "format": {"type": "json_schema", "schema": schema}},
                messages=[{"role": "user", "content": self._blocks(content)}],
            )
        except a.AuthenticationError as exc:
            raise AIError("AI 서비스 인증에 실패했어요. 서버의 API 키 설정을 확인해 주세요.", False, "ai_auth") from exc
        except a.PermissionDeniedError as exc:
            raise AIError("이 API 키로는 AI 모델을 사용할 수 없어요. 키 권한을 확인해 주세요.", False, "ai_permission") from exc
        except a.NotFoundError as exc:
            raise AIError("설정된 AI 모델을 찾을 수 없어요. HEALTHLINGO_AI_MODEL 값을 확인해 주세요.", False, "ai_model") from exc
        except a.RateLimitError as exc:
            raise AIError("AI 요청이 많아 잠시 처리할 수 없어요. 잠시 후 다시 시도해 주세요.", True, "ai_rate_limited") from exc
        except a.BadRequestError as exc:
            log.warning("AI bad request: %s", type(exc).__name__)
            raise AIError("AI가 이 요청을 처리하지 못했어요. 다른 사진을 쓰거나 직접 입력해 주세요.", False, "ai_bad_request") from exc
        except a.APIStatusError as exc:
            raise AIError("AI 서비스가 일시적으로 불안정해요. 잠시 후 다시 시도해 주세요.", True, "ai_unavailable") from exc
        except a.APITimeoutError as exc:
            raise AIError("AI 응답이 너무 오래 걸렸어요. 다시 시도해 주세요.", True, "ai_timeout") from exc
        except a.APIConnectionError as exc:
            raise AIError("AI 서비스에 연결하지 못했어요. 인터넷 연결을 확인하고 다시 시도해 주세요.", True, "ai_network") from exc

        if response.stop_reason == "refusal":
            raise AIError("AI가 이 요청은 분석하지 않았어요. 다른 사진을 쓰거나 직접 입력해 주세요.", False, "ai_refusal")
        if response.stop_reason == "max_tokens":
            raise AIError("AI 응답이 중간에 끊겼어요. 다시 시도해 주세요.", True, "ai_truncated")
        text = next((block.text for block in response.content if block.type == "text"), None)
        if not text:
            raise _invalid("no text block")
        try:
            return json.loads(text)
        except json.JSONDecodeError as exc:
            raise _invalid("not JSON") from exc


class GeminiJSONClient:
    """Calls the Gemini Interactions API (google-genai SDK) and returns the structured JSON payload.

    Popular models are sometimes overloaded (503 "high demand") or very slow. Each
    request therefore has a short time limit, and on overload/timeout we move on to
    the next model in the list and skip the slow one for a few minutes.
    """

    # Recognizing foods in a photo needs little reasoning; estimates and coaching a bit more.
    THINKING = {"recognize": "minimal", "estimate": "low", "coach": "low", "workout": "low", "explain": "minimal", "agent": "low"}
    FALLBACK_CODES = {"ai_unavailable", "ai_timeout", "ai_rate_limited", "ai_model", "ai_truncated"}
    COOLDOWN_SECONDS = 300

    def __init__(self, model: str, api_key: str, base_url: str | None = None,
                 fallback_models=(), timeout: float = 45.0):
        from google import genai  # imported lazily so the app runs without the SDK installed
        from google.genai import types

        self.model = model
        self.models = [model] + [m for m in fallback_models if m and m != model]
        self.timeout = timeout
        self._cooldown: dict[str, float] = {}
        self._lock = threading.Lock()
        self._local = threading.local()
        http_options = types.HttpOptions(
            timeout=int(timeout * 1000),  # milliseconds, per HTTP attempt
            # The Interactions API retries 503/429 on its own (3 extra calls by default, and still
            # 1 with attempts=0). Retrying an overloaded model only multiplies the wait, so we limit
            # HTTP-status retries to a code the API never sends and switch models ourselves instead.
            retry_options=types.HttpRetryOptions(attempts=1, http_status_codes=[599]),
            base_url=base_url,
        )
        self.client = genai.Client(api_key=api_key, http_options=http_options)

    @property
    def last_model(self) -> str | None:
        return getattr(self._local, "model", None)

    def _candidates(self) -> list[str]:
        now = time.monotonic()
        with self._lock:
            ready = [m for m in self.models if self._cooldown.get(m, 0) <= now]
        return ready or list(self.models)  # everything cooling down: try them all anyway

    def _cool(self, model: str) -> None:
        with self._lock:
            self._cooldown[model] = time.monotonic() + self.COOLDOWN_SECONDS

    @staticmethod
    def _map_exception(exc: Exception) -> AIError | None:
        """Classify SDK errors by HTTP status / class name.

        The Interactions API raises its own error classes from private SDK modules,
        so we avoid importing them and look at the attributes they all share.
        """
        name = type(exc).__name__
        code = getattr(exc, "status_code", None)
        if not isinstance(code, int):
            code = getattr(exc, "code", None)
        if isinstance(code, int) and code >= 400:
            body = getattr(exc, "body", None) or getattr(exc, "details", None)
            err = body.get("error", {}) if isinstance(body, dict) else {}
            status = str(err.get("status") or getattr(exc, "status", "") or "").upper()
            text = f"{err.get('message', '')} {exc}".upper()
            if code == 401 or status == "UNAUTHENTICATED" or "API KEY" in text or "API_KEY" in text:
                return AIError("AI 서비스 인증에 실패했어요. 서버의 GEMINI_API_KEY 설정을 확인해 주세요.", False, "ai_auth")
            if code == 403 or status == "PERMISSION_DENIED":
                return AIError("이 API 키로는 AI 모델을 사용할 수 없어요. 키 권한이나 결제 설정을 확인해 주세요.", False, "ai_permission")
            if code == 404 or status == "NOT_FOUND":
                return AIError("설정된 AI 모델을 찾을 수 없어요. HEALTHLINGO_AI_MODEL 값을 확인해 주세요.", False, "ai_model")
            if code == 429 or status == "RESOURCE_EXHAUSTED":
                return AIError("AI 요청이 많거나 사용 한도에 도달했어요. 잠시 후 다시 시도해 주세요.", True, "ai_rate_limited")
            if code >= 500:
                return AIError("AI 서비스가 일시적으로 불안정해요. 잠시 후 다시 시도해 주세요.", True, "ai_unavailable")
            log.warning("Gemini request rejected: %s %s", code, status)
            return AIError("AI가 이 요청을 처리하지 못했어요. 다른 사진을 쓰거나 직접 입력해 주세요.", False, "ai_bad_request")
        if "Timeout" in name:
            return AIError("AI 응답이 너무 오래 걸렸어요. 다시 시도해 주세요.", True, "ai_timeout")
        if "Connect" in name or "Network" in name or "Transport" in name or isinstance(exc, OSError):
            return AIError("AI 서비스에 연결하지 못했어요. 인터넷 연결을 확인하고 다시 시도해 주세요.", True, "ai_network")
        return None

    def complete_json(self, system: str, content: list[dict], schema: dict, task: str | None = None) -> dict:
        candidates = self._candidates()
        last_error: AIError | None = None
        for index, model in enumerate(candidates):
            started = time.monotonic()
            try:
                result = self._call(model, system, content, schema, task)
            except AIError as err:
                if err.code not in self.FALLBACK_CODES:
                    raise
                self._cool(model)
                last_error = err
                more = index + 1 < len(candidates)
                cause = err.__cause__
                log.warning("Gemini model %s failed (%s, http %s) after %.1fs%s", model, err.code,
                            getattr(cause, "status_code", None) or getattr(cause, "code", None) or "-",
                            time.monotonic() - started, f"; trying {candidates[index + 1]}" if more else "")
                continue
            self._local.model = model
            log.info("Gemini %s answered %s in %.1fs", model, task or "request", time.monotonic() - started)
            return result
        if last_error and last_error.code in {"ai_unavailable", "ai_timeout", "ai_rate_limited"}:
            raise AIError("지금 AI 서비스 사용자가 많아 응답이 늦어요. 잠시 후 다시 시도하거나 직접 입력해 주세요.", True, last_error.code)
        raise last_error or AIError("AI 분석을 마치지 못했어요. 다시 시도해 주세요.", True, "ai_unavailable")

    def _call(self, model: str, system: str, content: list[dict], schema: dict, task: str | None) -> dict:
        extra = {}
        if task in self.THINKING:
            extra["generation_config"] = {"thinking_level": self.THINKING[task]}
        try:
            interaction = self.client.interactions.create(
                model=model,
                system_instruction=system,
                input=content,  # [{"type": "image", "data", "mime_type"}, {"type": "text", "text"}]
                response_format={"type": "text", "mime_type": "application/json", "schema": schema},
                store=False,  # health data: don't keep the interaction on Google's side
                timeout=self.timeout,  # seconds, bounds the whole call
                **extra,
            )
        except Exception as exc:
            mapped = self._map_exception(exc)
            if mapped is None:
                raise
            raise mapped from exc

        status = str(interaction.status or "")
        if status == "incomplete":
            raise AIError("AI 응답이 중간에 끊겼어요. 다시 시도해 주세요.", True, "ai_truncated")
        if status != "completed":
            details = " ".join(f"{e.code or ''} {e.message or ''}" for e in (interaction.errors or [])).lower()
            log.warning("Gemini interaction ended with status %s", status)
            if any(word in details for word in ("safety", "blocked", "prohibited", "policy")):
                raise AIError("AI가 이 요청은 분석하지 않았어요. 다른 사진을 쓰거나 직접 입력해 주세요.", False, "ai_refusal")
            raise AIError("AI 분석을 마치지 못했어요. 다시 시도해 주세요.", True, "ai_unavailable")
        text = interaction.output_text
        if not text:
            raise _invalid("no text output")
        try:
            return json.loads(text)
        except json.JSONDecodeError as exc:
            raise _invalid("not JSON") from exc


class AIService:
    def __init__(self, client=None, key_name: str = "GEMINI_API_KEY"):
        self.client = client
        self.key_name = key_name

    @property
    def configured(self) -> bool:
        return self.client is not None

    def _require(self):
        if self.client is None:
            raise AINotConfigured(self.key_name)
        return self.client

    def _ask(self, system: str, content: list[dict], schema: dict, task: str) -> tuple[dict, str | None]:
        client = self._require()
        data = client.complete_json(system, content, schema, task=task)
        return data, getattr(client, "last_model", None)

    def recognize(self, image: bytes, mime: str) -> dict:
        content = [
            {"type": "image", "data": base64.standard_b64encode(image).decode("ascii"), "mime_type": mime},
            {"type": "text", "text": "이 사진 속 음식을 인식해 주세요."},
        ]
        data, model = self._ask(RECOGNITION_SYSTEM, content, RECOGNITION_SCHEMA, "recognize")
        return {**validate_recognition(data), "model": model}

    def estimate(self, meal: dict, profile: dict, day_context: dict) -> dict:
        payload = {
            "meal_type": meal["meal_type"],
            "confirmed_items": [{"name": i["name"], "amount": i.get("amount", ""), "cooking_method": i.get("cooking", "")} for i in meal["items"]],
            "memo": meal.get("memo", ""),
            "user_profile": profile,
            "day_context": day_context,
        }
        content = [{"type": "text", "text": "다음 식사의 영양을 추정하고 피드백을 주세요.\n" + json.dumps(payload, ensure_ascii=False)}]
        data, model = self._ask(NUTRITION_SYSTEM, content, NUTRITION_SCHEMA, "estimate")
        return {**validate_nutrition(data, meal["items"]), "model": model}

    def coach(self, payload: dict, blocked_terms: list[str]) -> dict:
        content = [{"type": "text", "text": "이 날의 식사 기록을 바탕으로 피드백과 추천을 주세요.\n" + json.dumps(payload, ensure_ascii=False)}]
        data, model = self._ask(COACH_SYSTEM, content, COACH_SCHEMA, "coach")
        return {**validate_coach(data, blocked_terms), "model": model}

    def ask_json(self, system: str, content: list[dict], schema: dict, task: str) -> tuple[dict, str | None]:
        """One structured-output call (used by the routine adjustment loop, which validates on its own)."""
        return self._ask(system, content, schema, task)

    def explain_targets(self, payload: dict) -> dict:
        content = [{"type": "text", "text": "다음 계산 결과를 사용자에게 설명해 주세요.\n" + json.dumps(payload, ensure_ascii=False)}]
        data, model = self._ask(TARGET_EXPLAIN_SYSTEM, content, TARGET_EXPLAIN_SCHEMA, "explain")
        return {**validate_target_explanation(data, payload), "model": model}

    def workout(self, profile: dict) -> dict:
        content = [{"type": "text", "text": "다음 정보를 바탕으로 이번 주 운동 루틴을 추천해 주세요.\n" + json.dumps(profile, ensure_ascii=False)}]
        data, model = self._ask(WORKOUT_SYSTEM, content, WORKOUT_SCHEMA, "workout")
        body_weight = (profile.get("body") or {}).get("weight_kg")
        days = profile.get("training_weekdays")
        training = [WEEKDAY_NAMES.index(d) for d in days] if days else None
        return {**validate_workout(data, profile.get("minutes_per_session"), body_weight, training), "model": model}


def build_ai_service(settings) -> AIService:
    key_name = settings.ai_key_name
    try:
        if settings.ai_provider == "gemini":
            if not settings.gemini_key_set:
                return AIService(None, key_name)
            client = GeminiJSONClient(
                settings.ai_model,
                os.environ["GEMINI_API_KEY"].strip(),
                fallback_models=settings.ai_fallback_models,
                timeout=settings.ai_timeout,
            )
            return AIService(client, key_name)
        if not settings.anthropic_key_set:
            return AIService(None, key_name)
        return AIService(AnthropicJSONClient(settings.ai_model, settings.ai_effort), key_name)
    except ImportError:
        log.warning("AI SDK for provider %s is not installed; AI features disabled", settings.ai_provider)
        return AIService(None, key_name)
