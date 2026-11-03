"""Automatic nutrition targets: formulas, safety checks, history (no retroactive change), AI explanation.
Weekly workout adjustment from last week's completion."""
import itertools
import unittest
from datetime import date, timedelta

from backend import progression
from backend.logic import ValidationError, default_settings, normalize_settings
from backend.nutrition import AMDR, LOSS_FLOOR, check_targets, compute_targets, macro_shares
from tests.test_api import PROFILE, TODAY, ServerTestCase

YESTERDAY = (date.fromisoformat(TODAY) - timedelta(days=1)).isoformat()


def settings(**over):
    base = {"goal_type": "체중 감량", "age": 30, "sex": "female", "activity_level": "sedentary", "special_condition": "none",
            "rest_days": [2, 6], "minutes_per_session": 30, "body": {"height_cm": 165, "weight_kg": 58.5, "skeletal_muscle_kg": None}}
    body = over.pop("body", {})
    base.update(over)
    base["body"] = {**base["body"], **body}
    return base


class FormulaTests(unittest.TestCase):
    def test_worked_example(self):
        r = compute_targets(settings())
        b = r["basis"]
        # Mifflin-St Jeor (women): 10*58.5 + 6.25*165 - 5*30 - 161 = 1305.25
        self.assertEqual(b["rmr"], 1305)
        # daily life 1305.25 * 1.40 = 1827; exercise (3.5-1) MET * 58.5kg * 0.5h * 5 days / 7 = 52
        self.assertEqual((b["daily_life"], b["exercise"], b["maintenance"]), (1827, 52, 1880))
        # loss: maintenance - 500, above the 1,200 kcal floor
        self.assertEqual((r["kcal"], r["protein_g"], r["fat_g"], r["carbs_g"]), (1380, 94, 38, 166))

    def test_every_result_satisfies_the_rules(self):
        grid = itertools.product([150, 175, 195], [45, 70, 110], [19, 45, 78], ["male", "female"],
                                 ["sedentary", "light", "active"], ["체중 감량", "건강 유지", "근육 증가"], [[], [0, 2, 4, 6], [0, 1, 2, 3, 4, 5]], [20, 60])
        checked = 0
        for h, w, age, sex, act, goal, rest, minutes in grid:
            r = compute_targets(settings(age=age, sex=sex, activity_level=act, goal_type=goal, rest_days=rest, minutes_per_session=minutes, body={"height_cm": h, "weight_kg": w}))
            if r["status"] != "ok":
                self.assertEqual(r["status"], "unsupported")
                self.assertNotIn("kcal", r)
                continue
            checked += 1
            self.assertEqual(check_targets(r, sex), [])
            self.assertLessEqual(abs(r["carbs_g"] * 4 + r["protein_g"] * 4 + r["fat_g"] * 9 - r["kcal"]), 4)
            for key, share in macro_shares(r["kcal"], r["carbs_g"], r["protein_g"], r["fat_g"]).items():
                self.assertTrue(AMDR[key][0] - 0.01 <= share <= AMDR[key][1] + 0.01, (key, share))
            maint = r["basis"]["maintenance"]
            if goal == "체중 감량":
                self.assertLessEqual(maint - r["kcal"], 505)  # never more than the 500 kcal step (plus rounding)
                self.assertTrue(r["kcal"] >= LOSS_FLOOR[sex] or abs(r["kcal"] - maint) <= 5)
            else:
                self.assertLessEqual(abs(r["kcal"] - maint), 5)  # no unverified surplus for muscle gain
        self.assertGreater(checked, 500)

    def test_muscle_gain_uses_maintenance_and_more_protein(self):
        keep = compute_targets(settings(goal_type="건강 유지"))
        gain = compute_targets(settings(goal_type="근육 증가"))
        self.assertEqual(gain["kcal"], keep["kcal"])
        self.assertGreater(gain["protein_g"], keep["protein_g"])
        self.assertIn("Slater", " ".join(gain["basis"]["notes"]))

    def test_skeletal_muscle_is_not_used_as_lean_mass(self):
        a = compute_targets(settings(body={"skeletal_muscle_kg": None}))
        b = compute_targets(settings(body={"skeletal_muscle_kg": 25}))
        self.assertEqual({k: a[k] for k in ("kcal", "carbs_g", "protein_g", "fat_g")}, {k: b[k] for k in ("kcal", "carbs_g", "protein_g", "fat_g")})

    def test_exercise_is_counted_once(self):
        no_extra = compute_targets(settings(goal_type="건강 유지", minutes_per_session=20))
        more = compute_targets(settings(goal_type="건강 유지", minutes_per_session=60))
        self.assertEqual(no_extra["basis"]["daily_life"], more["basis"]["daily_life"])  # daily life excludes exercise
        self.assertGreater(more["basis"]["exercise"], no_extra["basis"]["exercise"])

    def test_missing_inputs_are_named_not_guessed(self):
        for key, label in [("age", "나이"), ("sex", "성별"), ("activity_level", "평소 활동량"), ("goal_type", "목표"),
                           ("special_condition", "해당되는 상황"), ("minutes_per_session", "1회 운동 시간")]:
            with self.subTest(key=key):
                r = compute_targets(settings(**{key: "" if key not in ("age", "minutes_per_session") else None}))
                self.assertEqual(r["status"], "missing")
                self.assertTrue(any(label in m for m in r["missing"]), r["missing"])
                self.assertNotIn("kcal", r)
        r = compute_targets(settings(rest_days=None, workouts_per_week=None))
        self.assertIn("운동 계획(휴식 요일)", r["missing"])
        r = compute_targets(settings(body={"weight_kg": None}))
        self.assertIn("몸무게", r["missing"])

    def test_conditions_outside_the_formulas_get_no_target(self):
        for over in ({"special_condition": "pregnant"}, {"special_condition": "medical"}, {"age": 17}, {"age": 80},
                     {"body": {"weight_kg": 45, "height_cm": 170}}):  # BMI 15.6 with a weight-loss goal
            with self.subTest(over=over):
                r = compute_targets(settings(**over))
                self.assertEqual(r["status"], "unsupported")
                self.assertNotIn("kcal", r)
                self.assertTrue(r["message"])

    def test_input_validation(self):
        for raw in ({"age": "abc"}, {"age": 30.5}, {"age": 5}, {"sex": "other"}, {"activity_level": "very"},
                    {"special_condition": "x"}, {"targets": {"kcal": 2000}}):
            with self.subTest(raw=raw), self.assertRaises(ValidationError):
                normalize_settings(raw, default_settings(), "T")
        self.assertEqual(normalize_settings({"age": "31"}, default_settings(), "T")["age"], 31)


class TargetStorageTests(ServerTestCase):
    def day_target(self, a, day):
        return a.get(f"/api/days/{day}")[1]["summary"]["progress"]["kcal"]["target"]

    def test_saved_once_and_only_recalculated_when_inputs_change(self):
        a = self.login()
        status, body = a.put("/api/settings", {**PROFILE, "today": TODAY})
        self.assertEqual(status, 200, body)
        self.assertTrue(body["nutrition_changed"])
        current = body["nutrition"]["current"]
        self.assertEqual((current["source"], current["effective_date"]), ("auto", TODAY))
        self.assertFalse(a.put("/api/settings", {**PROFILE, "food_preferences": "한식", "today": TODAY})[1]["nutrition_changed"])
        self.assertTrue(a.put("/api/settings", {"body": {"weight_kg": 62}, "today": TODAY})[1]["nutrition_changed"])
        self.assertEqual(len(a.get(f"/api/nutrition-target?today={TODAY}")[1]["history"]), 2)
        self.assertEqual(self.fake.calls, 0)  # no AI for numbers or for viewing

    def test_past_days_keep_their_target_and_same_day_change_keeps_meals(self):
        a = self.login()
        a.put("/api/settings", {**PROFILE, "today": YESTERDAY})
        old = a.get(f"/api/nutrition-target?today={YESTERDAY}")[1]["current"]["kcal"]
        meal_y = self.create_meal(a, date=YESTERDAY)
        a.put("/api/settings", {"body": {"weight_kg": 70}, "today": TODAY})
        new = a.get(f"/api/nutrition-target?today={TODAY}")[1]["current"]["kcal"]
        self.assertNotEqual(old, new)
        self.assertEqual(self.day_target(a, YESTERDAY), old)   # not changed retroactively
        self.assertEqual(self.day_target(a, TODAY), new)
        meal_t = self.create_meal(a, date=TODAY)
        a.put("/api/settings", {"goal_type": "체중 감량", "today": TODAY})  # second change on the same day
        newer = a.get(f"/api/nutrition-target?today={TODAY}")[1]["current"]["kcal"]
        self.assertLess(newer, new)
        self.assertEqual(self.day_target(a, TODAY), newer)
        self.assertEqual([m["id"] for m in a.get(f"/api/meals?date={TODAY}")[1]["meals"]], [meal_t["id"]])
        self.assertEqual([m["id"] for m in a.get(f"/api/meals?date={YESTERDAY}")[1]["meals"]], [meal_y["id"]])
        # Home (overview) and the food screen use the same stored value.
        ov = a.get(f"/api/overview?today={TODAY}")[1]["nutrition"]
        self.assertEqual(ov["current"]["kcal"], newer)
        self.assertEqual(ov["progress"]["kcal"]["target"], newer)
        stats = a.get(f"/api/stats/range?from={YESTERDAY}&to={TODAY}")[1]
        self.assertEqual([d["targets"]["kcal"] for d in stats["days"]], [old, newer])

    def test_removing_required_info_stops_new_targets_but_keeps_history(self):
        a = self.login()
        a.put("/api/settings", {**PROFILE, "today": YESTERDAY})
        kept = a.get(f"/api/nutrition-target?today={YESTERDAY}")[1]["current"]["kcal"]
        body = a.put("/api/settings", {"age": None, "today": TODAY})[1]
        self.assertIsNone(body["nutrition"]["current"])
        self.assertEqual(body["nutrition"]["setup"]["status"], "missing")
        self.assertIn("나이", body["nutrition"]["setup"]["missing"])
        self.assertIsNone(self.day_target(a, TODAY))
        self.assertEqual(self.day_target(a, YESTERDAY), kept)

    def test_targets_typed_in_before_keep_applying_to_past_days(self):
        a = self.login()
        uid = a.get("/api/me")[1]["user"]["id"]
        with self.server.app.db.write_lock, self.server.app.db.connect() as conn:
            conn.execute("INSERT INTO user_settings (user_id, data, updated_at) VALUES (?, ?, 'T')",
                         (uid, '{"targets": {"kcal": 2100, "carbs_g": null, "protein_g": 90, "fat_g": null}}'))
        self.assertEqual(self.day_target(a, "2026-10-01"), 2100)
        a.put("/api/settings", {**PROFILE, "today": TODAY})
        self.assertEqual(self.day_target(a, "2026-10-01"), 2100)
        self.assertNotEqual(self.day_target(a, TODAY), 2100)

    def test_ai_explanation_never_adds_numbers_and_failure_keeps_target(self):
        a = self.login()
        a.put("/api/settings", {**PROFILE, "today": TODAY})
        status, body = a.post("/api/nutrition-target/explain", {"today": TODAY})
        self.assertEqual(status, 200, body)
        exp = body["current"]["explanation"]
        self.assertIn("kcal는 개인 설정으로", exp["summary"])
        self.assertNotIn("3,333", exp["summary"])  # an invented number is removed
        self.assertTrue(exp["dropped_sentences"])
        a.put("/api/settings", {"body": {"weight_kg": 60}, "today": TODAY})
        self.fake.mode = "fail"
        status, body = a.post("/api/nutrition-target/explain", {"today": TODAY})
        self.assertEqual(status, 502)
        target = a.get(f"/api/nutrition-target?today={TODAY}")[1]["current"]
        self.assertIsNotNone(target["kcal"])
        self.assertEqual(target["explanation_status"], "failed")


def plan_with(weekdays=(0, 3), weight=10.0):
    return {"format": 3, "sessions": [
        {"weekday": d, "title": "전신", "focus": "", "minutes": 30, "intensity": "보통", "exercises": [
            {"name": "제자리 걷기", "amount": "5분", "weight_kg": None, "weight_note": "맨몸", "tip": ""},
            {"name": "덤벨 스쿼트", "amount": "3세트 × 10회", "weight_kg": weight, "weight_note": "", "tip": ""},
            {"name": "플랭크", "amount": "3세트 × 30초", "weight_kg": None, "weight_note": "", "tip": ""}]}
        for d in weekdays]}


class ProgressionTests(unittest.TestCase):
    MON = date(2026, 10, 26)

    def checks(self, days_done):
        return {((self.MON + timedelta(days=d)).isoformat(), progression.exercise_key(i, n))
                for d in days_done for i, n in enumerate(["제자리 걷기", "덤벨 스쿼트", "플랭크"])}

    def test_review_levels(self):
        plan = plan_with()
        self.assertEqual(progression.review_week(plan, self.MON, self.checks([0, 3]), self.MON)["level"], "increase")
        self.assertEqual(progression.review_week(plan, self.MON, self.checks([0]), self.MON)["level"], "keep")
        self.assertEqual(progression.review_week(plan, self.MON, set(), self.MON)["level"], "decrease")
        # Plan made on Wednesday: only Thursday counts.
        r = progression.review_week(plan, self.MON + timedelta(days=2), self.checks([3]), self.MON)
        self.assertEqual((r["planned"], r["done"], r["level"]), (3, 3, "increase"))
        self.assertIsNone(progression.review_week(plan, self.MON + timedelta(days=7), set(), self.MON))

    def test_decrease_and_increase(self):
        down, changes = progression.adjust_plan(plan_with(), "decrease")
        ex = down["sessions"][0]["exercises"]
        self.assertEqual(down["sessions"][0]["intensity"], "가볍게")
        self.assertEqual((ex[1]["amount"], ex[1]["weight_kg"], ex[2]["amount"], ex[0]["amount"]), ("2세트 × 8회", 9.0, "2세트 × 24초", "4분"))
        self.assertTrue(changes)
        up, _ = progression.adjust_plan(plan_with(), "increase")
        ex = up["sessions"][0]["exercises"]
        self.assertEqual(up["sessions"][0]["intensity"], "조금 힘들게")
        self.assertEqual((ex[1]["weight_kg"], ex[1]["amount"], ex[2]["amount"]), (10.5, "3세트 × 10회", "3세트 × 40초"))  # +5 %
        light, _ = progression.adjust_plan(plan_with(weight=2.0), "increase")  # 2.1kg rounds back to 2kg
        self.assertEqual((light["sessions"][0]["exercises"][1]["weight_kg"], light["sessions"][0]["exercises"][1]["amount"]), (2.0, "3세트 × 11회"))
        same, none = progression.adjust_plan(plan_with(), "keep")
        self.assertEqual((same, none), (plan_with(), []))

    def test_ai_routine_is_capped_against_previous(self):
        prev = plan_with(weight=10.0)
        new = plan_with(weight=20.0)
        new["sessions"][0]["intensity"] = "조금 힘들게"
        self.assertEqual(progression.cap_against_previous(new, prev, "decrease"), 4)
        self.assertEqual(new["sessions"][0]["exercises"][1]["weight_kg"], 10.0)
        self.assertEqual(new["sessions"][0]["intensity"], "가볍게")
        new = plan_with(weight=20.0)
        progression.cap_against_previous(new, prev, "increase")
        self.assertEqual(new["sessions"][0]["exercises"][1]["weight_kg"], 11.0)  # at most +10 %


class WeeklyAdjustmentApiTests(ServerTestCase):
    def test_last_week_is_reviewed_once_and_the_routine_changes(self):
        a = self.login()
        a.put("/api/settings", {"rest_days": [1, 2, 4, 5, 6], "body": {"height_cm": 170, "weight_kg": 65}})  # Mon + Thu
        self.assertEqual(a.post("/api/workout-plan", {"today": TODAY})[0], 200)
        uid = a.get("/api/me")[1]["user"]["id"]
        last_monday = progression.week_start(date.fromisoformat(TODAY)) - timedelta(days=7)
        app = self.server.app
        with app.db.write_lock, app.db.connect() as conn:  # pretend the routine was made last week and nothing was done
            row = conn.execute("SELECT plan FROM workout_plans WHERE user_id = ?", (uid,)).fetchone()
            plan = __import__("json").loads(row["plan"])
            plan.pop("adjusted_week")
            conn.execute("UPDATE workout_plans SET plan = ?, created_at = ? WHERE user_id = ?",
                         (__import__("json").dumps(plan, ensure_ascii=False), last_monday.isoformat() + "T00:00:00Z", uid))
        before = a.get(f"/api/workout-plan?today={TODAY}")[1]["plan"]
        self.assertEqual(before["adjustments"][0]["level"], "decrease")
        self.assertEqual(before["adjustments"][0]["done"], 0)
        self.assertEqual({s["intensity"] for s in before["sessions"]}, {"가볍게"})
        again = a.get(f"/api/workout-plan?today={TODAY}")[1]["plan"]
        self.assertEqual(len(again["adjustments"]), 1)  # only once per week
        self.assertEqual(again["sessions"], before["sessions"])
        # Asking the AI again passes the review along and caps the result against the previous routine.
        regenerated = a.post("/api/workout-plan", {"today": TODAY})[1]["plan"]
        self.assertEqual(self.fake.last_workout_input["progress_review"]["adjustment"], "decrease")
        self.assertIn("previous_plan", self.fake.last_workout_input)
        self.assertFalse(regenerated["stale"])  # the review is not part of the "settings changed" check
        self.assertEqual(regenerated["adjustments"][0]["source"], "ai")
        self.assertLessEqual({s["intensity"] for s in regenerated["sessions"]}, {"가볍게"})  # capped below the previous level


if __name__ == "__main__":
    unittest.main()
