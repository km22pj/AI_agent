"""Daily nutrition targets calculated from the personal settings (no AI numbers).

Every number below comes from a published source (see NUTRITION.md). Where no source gives a
value, the app does not invent one: e.g. no calorie surplus for muscle gain (none is established).
Values marked APP CHOICE are picks inside a published range, and are shown as such.
"""
from __future__ import annotations

import hashlib
import json

# Inputs the user picks in the settings dialog.
SEXES = {"male": "남성", "female": "여성"}
# Daily life WITHOUT planned exercise (exercise is added separately, see exercise_kcal).
# FAO/WHO/UNU 2004 PAL ranges: sedentary/light 1.40-1.69, active/moderately active 1.70-1.99.
# APP CHOICE: one representative value per level, inside those ranges.
ACTIVITY_LEVELS = {
    "sedentary": ("주로 앉아서 지내요", 1.40, "FAO/WHO/UNU 앉아서 생활·가벼운 활동 범위(1.40~1.69)의 하한"),
    "light": ("가볍게 자주 움직여요", 1.55, "FAO/WHO/UNU 앉아서 생활·가벼운 활동 범위(1.40~1.69)의 중간값"),
    "active": ("몸을 많이 쓰는 일을 해요", 1.75, "FAO/WHO/UNU 활동적 생활 범위(1.70~1.99)의 하단"),
}
SPECIAL_CONDITIONS = {"none": "해당 없음", "pregnant": "임신 또는 수유 중", "medical": "의료진의 식이 관리를 받는 중"}

LOSS_GOALS = {"체중 감량"}
GAIN_GOALS = {"근육 증가"}

# Mifflin-St Jeor 1990: derived from 498 healthy adults aged 19-78.
AGE_MIN, AGE_MAX = 19, 78
# 2024 Adult Compendium of Physical Activities, code 02054:
# resistance (weight) training, multiple exercises, 8-15 reps at varied resistance = 3.5 MET.
EXERCISE_MET = 3.5
# AHA/ACC/TOS 2013 obesity guideline: 500-750 kcal/day deficit, or 1,200-1,500 (women) / 1,500-1,800 (men) kcal/day.
LOSS_DEFICIT = 500           # the lower (gentler) end of the guideline's deficit
LOSS_FLOOR = {"female": 1200, "male": 1500}
MAX_DEFICIT = 750            # never more than the guideline's upper deficit
# Protein (g per kg body weight): ISSN 2017 position stand 1.4-2.0 g/kg/day for exercising people;
# Morton et al. 2018 meta-analysis: gains in fat-free mass plateau around 1.6 g/kg/day.
PROTEIN_PER_KG = {"loss": 1.6, "gain": 1.6, "maintain": 1.4}
# IOM 2005 Acceptable Macronutrient Distribution Ranges (adults, % of energy).
AMDR = {"carbs": (0.45, 0.65), "fat": (0.20, 0.35), "protein": (0.10, 0.35)}
FAT_SHARE = 0.25             # APP CHOICE inside the fat AMDR (20-35 %)
KCAL_PER_G = {"carbs": 4, "protein": 4, "fat": 9}

SOURCES = [
    {"id": "mifflin", "title": "Mifflin MD, St Jeor ST et al. A new predictive equation for resting energy expenditure in healthy individuals. Am J Clin Nutr 1990;51:241-7"},
    {"id": "frankenfield", "title": "Frankenfield D et al. Comparison of predictive equations for resting metabolic rate in healthy nonobese and obese adults: a systematic review. J Am Diet Assoc 2005"},
    {"id": "fao", "title": "FAO/WHO/UNU. Human energy requirements (2004) – physical activity level (PAL) ranges"},
    {"id": "compendium", "title": "2024 Adult Compendium of Physical Activities, code 02054 (3.5 MET)"},
    {"id": "aha", "title": "2013 AHA/ACC/TOS Guideline for the Management of Overweight and Obesity in Adults"},
    {"id": "issn", "title": "Jäger R et al. ISSN Position Stand: protein and exercise. JISSN 2017"},
    {"id": "morton", "title": "Morton RW et al. Protein supplementation and resistance training-induced gains: meta-analysis. Br J Sports Med 2018;52:376-84"},
    {"id": "amdr", "title": "Institute of Medicine. Dietary Reference Intakes for Energy, Carbohydrate, Fiber, Fat, Fatty Acids, Cholesterol, Protein, and Amino Acids (2005) – AMDR"},
    {"id": "slater", "title": "Slater GJ et al. Is an energy surplus required to maximize skeletal muscle hypertrophy associated with resistance training? Front Nutr 2019"},
]


def _training(settings: dict) -> tuple[int | None, int | None]:
    """(workout days per week, minutes per session) from the user's own plan, or None if not set."""
    rest = settings.get("rest_days")
    days = 7 - len(rest) if rest is not None else settings.get("workouts_per_week")
    return days, settings.get("minutes_per_session")


def target_inputs(settings: dict) -> dict:
    body = settings.get("body") or {}
    days, minutes = _training(settings)
    return {
        "height_cm": body.get("height_cm"),
        "weight_kg": body.get("weight_kg"),
        "age": settings.get("age"),
        "sex": settings.get("sex") or None,
        "activity_level": settings.get("activity_level") or None,
        "special_condition": settings.get("special_condition") or None,
        "goal": settings.get("goal_type") or None,
        "workout_days": days,
        "workout_minutes": minutes,
    }


def inputs_hash(inputs: dict) -> str:
    return hashlib.sha256(json.dumps(inputs, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()


def compute_targets(settings: dict) -> dict:
    """Returns {"status": "ok" | "missing" | "unsupported", ...}. Never guesses a missing input."""
    i = target_inputs(settings)
    base = {"inputs": i, "input_hash": inputs_hash(i)}
    labels = [
        ("height_cm", "키"), ("weight_kg", "몸무게"), ("age", "나이"), ("sex", "성별(계산식용)"),
        ("activity_level", "평소 활동량(운동 제외)"), ("special_condition", "해당되는 상황"),
        ("goal", "운동·식단 목표"), ("workout_days", "운동 계획(휴식 요일)"), ("workout_minutes", "1회 운동 시간"),
    ]
    missing = [label for key, label in labels if i[key] is None]
    if missing:
        return {**base, "status": "missing", "missing": missing,
                "message": f"자동 계산에 필요한 정보가 없어요: {', '.join(missing)}. 개인 설정에서 입력해 주세요."}

    w, h, age, sex = float(i["weight_kg"]), float(i["height_cm"]), int(i["age"]), i["sex"]
    if i["special_condition"] != "none":
        return {**base, "status": "unsupported", "missing": [],
                "message": f"‘{SPECIAL_CONDITIONS[i['special_condition']]}’에는 일반 계산 기준을 적용할 수 없어 목표를 만들지 않았어요. 의료진이나 영양 전문가와 목표를 정해 주세요."}
    if not AGE_MIN <= age <= AGE_MAX:
        return {**base, "status": "unsupported", "missing": [],
                "message": f"계산식(Mifflin-St Jeor)이 만 {AGE_MIN}~{AGE_MAX}세 성인 자료로 만들어져, 이 나이에는 목표를 만들지 않았어요. 전문가와 상의해 주세요."}
    bmi = w / (h / 100) ** 2
    if i["goal"] in LOSS_GOALS and bmi < 18.5:
        return {**base, "status": "unsupported", "missing": [],
                "message": f"BMI {bmi:.1f}로 저체중 범위(18.5 미만)라 감량 목표 열량은 만들지 않았어요. 목표를 ‘건강 유지’로 바꾸거나 전문가와 상의해 주세요."}

    # 1) resting energy: Mifflin-St Jeor (uses total body weight; skeletal muscle mass is NOT fat-free mass and is not used)
    rmr = 10 * w + 6.25 * h - 5 * age + (5 if sex == "male" else -161)
    # 2) daily life without exercise: RMR x PAL
    label, pal, pal_note = ACTIVITY_LEVELS[i["activity_level"]]
    daily_life = rmr * pal
    # 3) planned exercise, added once: net MET (MET - 1, resting part is already in step 2), 1 MET ~ 1 kcal/kg/h
    days, minutes = int(i["workout_days"]), int(i["workout_minutes"])
    exercise = (EXERCISE_MET - 1) * w * (minutes / 60) * days / 7
    maintenance = daily_life + exercise

    notes = []
    if i["goal"] in LOSS_GOALS:
        goal_kind = "loss"
        floor = LOSS_FLOOR[sex]
        if maintenance - floor <= 0:
            kcal = maintenance
            notes.append(f"유지 열량이 권장 최저 섭취량({floor:,}kcal) 이하라 감량용 차감을 적용하지 않았어요.")
        else:
            kcal = max(maintenance - LOSS_DEFICIT, floor)
            notes.append(f"유지 열량에서 하루 {round(maintenance - kcal):,}kcal를 줄였어요 (권장 차감 500~750kcal, 최저 {floor:,}kcal).")
    elif i["goal"] in GAIN_GOALS:
        goal_kind = "gain"
        kcal = maintenance
        notes.append("근육 증가에 필요한 추가 열량은 근거로 정해진 값이 없어(Slater 2019) 유지 열량으로 계산하고 단백질을 높였어요.")
    else:
        goal_kind = "maintain"
        kcal = maintenance
    kcal = int(round(kcal / 10) * 10)
    if not 1000 <= kcal <= 5000:
        return {**base, "status": "unsupported", "missing": [],
                "message": "계산 결과가 일반 계산 기준의 적용 범위를 벗어나 목표를 만들지 않았어요. 전문가와 상의해 주세요."}

    # 4) macros: protein by body weight (kept inside AMDR), fat 25 %, carbohydrate = the rest (kept inside AMDR)
    per_kg = PROTEIN_PER_KG[goal_kind]
    protein_g = round(per_kg * w)
    lo, hi = AMDR["protein"]
    protein_capped = None
    if protein_g * 4 > kcal * hi:
        protein_g, protein_capped = int(kcal * hi // 4), "상한"
    elif protein_g * 4 < kcal * lo:
        protein_g, protein_capped = int(-(-kcal * lo // 4)), "하한"
    if protein_capped:
        notes.append(f"체중 기준 단백질이 권장 비율 범위(10~35%)를 벗어나 {protein_capped}에 맞췄어요.")
    p_share = protein_g * 4 / kcal
    fat_share = FAT_SHARE
    c_lo, c_hi = AMDR["carbs"]
    if 1 - p_share - fat_share < c_lo:
        fat_share = max(AMDR["fat"][0], 1 - p_share - c_lo)
    elif 1 - p_share - fat_share > c_hi:
        fat_share = min(AMDR["fat"][1], 1 - p_share - c_hi)
    fat_g = round(kcal * fat_share / 9)
    carbs_g = round((kcal - protein_g * 4 - fat_g * 9) / 4)

    result = {
        **base, "status": "ok", "missing": [], "message": "",
        "kcal": kcal, "carbs_g": carbs_g, "protein_g": protein_g, "fat_g": fat_g,
        "basis": {
            "rmr": round(rmr), "pal": pal, "activity_label": label, "pal_note": pal_note,
            "daily_life": round(daily_life), "exercise": round(exercise), "exercise_met": EXERCISE_MET,
            "workout_days": days, "workout_minutes": minutes, "maintenance": round(maintenance),
            "goal_kind": goal_kind, "protein_per_kg": per_kg, "bmi": round(bmi, 1),
            "shares": {k: round(v * 100) for k, v in macro_shares(kcal, carbs_g, protein_g, fat_g).items()},
            "notes": notes,
        },
    }
    problems = check_targets(result, sex)
    if problems:  # should never happen; refuse rather than show a number that breaks the rules
        return {**base, "status": "unsupported", "missing": [], "message": "계산 결과가 안전 기준 검증을 통과하지 못해 목표를 만들지 않았어요: " + "; ".join(problems)}
    return result


def macro_shares(kcal, carbs_g, protein_g, fat_g) -> dict:
    return {"carbs": carbs_g * 4 / kcal, "protein": protein_g * 4 / kcal, "fat": fat_g * 9 / kcal}


def check_targets(result: dict, sex: str) -> list[str]:
    """Invariants every stored target must satisfy."""
    problems = []
    kcal, c, p, f = result["kcal"], result["carbs_g"], result["protein_g"], result["fat_g"]
    if abs(c * 4 + p * 4 + f * 9 - kcal) > 4:
        problems.append("탄·단·지 열량 합계가 목표 열량과 맞지 않음")
    for key, share in macro_shares(kcal, c, p, f).items():
        lo, hi = AMDR[key]
        if not lo - 0.01 <= share <= hi + 0.01:
            problems.append(f"{key} 비율 {share:.0%}가 권장 범위 밖")
    b = result["basis"]
    if b["goal_kind"] == "loss":
        if kcal < LOSS_FLOOR[sex] - 5 and kcal < b["maintenance"] - 5:
            problems.append("권장 최저 섭취량보다 낮음")
        if b["maintenance"] - kcal > MAX_DEFICIT + 5:
            problems.append("하루 차감량이 750kcal를 넘음")
    elif abs(kcal - b["maintenance"]) > 5:
        problems.append("감량 목표가 아닌데 유지 열량과 다름")
    if min(c, p, f) <= 0:
        problems.append("0 이하의 영양소 목표")
    return problems
