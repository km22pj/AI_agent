"""Body status in settings, the AI workout routine, and the record streak on the Today tab."""
import unittest
from datetime import date

from backend.ai import AIError, validate_workout
from backend.app import App
from backend.logic import ValidationError, body_metrics, default_settings, normalize_settings
from tests.test_api import ServerTestCase


class BodySettingsTests(unittest.TestCase):
    def test_values_are_validated_and_metrics_calculated(self):
        s = normalize_settings({"body": {"height_cm": "165", "weight_kg": 58.46, "skeletal_muscle_kg": 22.4}}, default_settings(), "T1")
        self.assertEqual(s["body"]["weight_kg"], 58.5)
        self.assertEqual(s["body_updated_at"], "T1")
        m = body_metrics(s)
        self.assertEqual((m["bmi"], m["muscle_ratio_pct"], m["missing"]), (21.5, 38.3, []))
        # Saving the same values again does not move the "updated" time.
        again = normalize_settings({"body": {"height_cm": 165, "weight_kg": 58.5, "skeletal_muscle_kg": 22.4}}, s, "T2")
        self.assertEqual(again["body_updated_at"], "T1")
        # Partial update keeps the other values; clearing a value works.
        cleared = normalize_settings({"body": {"skeletal_muscle_kg": None}}, s, "T3")
        self.assertEqual((cleared["body"]["height_cm"], cleared["body"]["skeletal_muscle_kg"]), (165.0, None))
        self.assertEqual(body_metrics(cleared)["missing"], ["골격근량"])

    def test_out_of_range_and_impossible_values_are_rejected(self):
        for body in ({"height_cm": 40}, {"weight_kg": 999}, {"skeletal_muscle_kg": 1},
                     {"weight_kg": 50, "skeletal_muscle_kg": 60}, {"height_cm": "abc"}, "nope"):
            with self.subTest(body=body), self.assertRaises(ValidationError):
                normalize_settings({"body": body}, default_settings(), "T")


class WorkoutValidationTests(unittest.TestCase):
    def good(self, **over):
        data = {"summary": "요약", "sessions": [{"title": "1일차", "focus": "하체", "minutes": 30, "intensity": "보통",
                "exercises": [{"name": "스쿼트", "amount": "2세트 × 10회", "tip": "무릎을 발끝 방향으로"}]}],
                "cautions": ["통증이 있으면 멈추세요."], "questions": []}
        data.update(over)
        return data

    def test_accepts_valid_plan_and_caps_minutes(self):
        out = validate_workout(self.good(), max_minutes=20)
        self.assertEqual(out["sessions"][0]["minutes"], 20)
        self.assertEqual(out["filtered_exercises"], 0)

    def test_rejects_bad_shapes(self):
        bad_session = {"title": "x", "focus": "", "minutes": 30, "intensity": "죽을 만큼", "exercises": [{"name": "a", "amount": "", "tip": ""}]}
        for data in ("x", self.good(sessions=[]), self.good(sessions=[bad_session]),
                     self.good(sessions=[{**bad_session, "intensity": "보통", "minutes": 9999}])):
            with self.subTest(data=str(data)[:40]), self.assertRaises(AIError):
                validate_workout(data)


class WorkoutApiTests(ServerTestCase):
    def test_requires_height_and_weight(self):
        a = self.login()
        status, body = a.get("/api/workout-plan")
        self.assertEqual(status, 200)
        self.assertIsNone(body["plan"])
        self.assertEqual(body["body"]["missing"], ["키", "몸무게", "골격근량"])
        status, body = a.post("/api/workout-plan", {})
        self.assertEqual(status, 400)
        self.assertIn("키와 몸무게", body["error"]["message"])
        self.assertEqual(self.fake.calls, 0)

    def test_plan_uses_body_and_settings_filters_unsafe_and_goes_stale(self):
        a = self.login()
        a.put("/api/settings", {"workouts_per_week": 2, "minutes_per_session": 30, "experience": "처음이에요",
                                "body": {"height_cm": 170, "weight_kg": 65, "skeletal_muscle_kg": 28, "condition_note": "무릎 통증"}})
        status, body = a.post("/api/workout-plan", {})
        self.assertEqual(status, 200, body)
        sent = self.fake.last_workout_input
        self.assertEqual(sent["body"], {"height_cm": 170.0, "weight_kg": 65.0, "skeletal_muscle_kg": 28.0,
                                        "bmi_calculated": 22.5, "skeletal_muscle_ratio_pct_calculated": 43.1})
        self.assertEqual(sent["cautions_from_user"], "무릎 통증")
        self.assertNotIn("email", str(sent))
        plan = body["plan"]
        self.assertEqual(len(plan["sessions"]), 2)
        self.assertTrue(all(s["minutes"] == 30 for s in plan["sessions"]))
        names = [e["name"] for s in plan["sessions"] for e in s["exercises"]]
        self.assertNotIn("벌칙 버피", names)
        self.assertEqual(plan["filtered_exercises"], 2)
        self.assertNotIn("태워야", plan["summary"])
        self.assertFalse(plan["stale"])
        self.assertEqual(plan["model"], "fake-model")
        # Saved: survives a reload, and goes stale once the body values change.
        self.assertEqual(a.get("/api/workout-plan")[1]["plan"]["sessions"], plan["sessions"])
        a.put("/api/settings", {"body": {"weight_kg": 64}})
        self.assertTrue(a.get("/api/workout-plan")[1]["plan"]["stale"])
        # Another account never sees it.
        b = self.login("사용자B")
        self.assertIsNone(b.get("/api/workout-plan")[1]["plan"])

    def test_ai_failure_is_reported_not_faked(self):
        a = self.login()
        a.put("/api/settings", {"body": {"height_cm": 170, "weight_kg": 65}})
        self.fake.mode = "fail"
        status, body = a.post("/api/workout-plan", {})
        self.assertEqual(status, 502)
        self.assertIsNone(a.get("/api/workout-plan")[1]["plan"])


class WorkoutWithoutAITests(ServerTestCase):
    ai_enabled = False

    def test_workout_needs_ai(self):
        a = self.login()
        a.put("/api/settings", {"body": {"height_cm": 170, "weight_kg": 65}})
        status, body = a.post("/api/workout-plan", {})
        self.assertEqual((status, body["error"]["code"]), (503, "ai_not_configured"))


class StreakTests(unittest.TestCase):
    T = date(2026, 11, 3)

    def streak(self, days):
        return App._streaks(days, self.T)

    def test_counts_consecutive_days_up_to_today(self):
        self.assertEqual(self.streak(["2026-11-01", "2026-11-02", "2026-11-03"]), {"current": 3, "best": 3, "today_recorded": True})

    def test_today_not_recorded_yet_keeps_yesterdays_run(self):
        self.assertEqual(self.streak(["2026-11-01", "2026-11-02"]), {"current": 2, "best": 2, "today_recorded": False})

    def test_gap_breaks_the_run_but_best_is_kept(self):
        days = ["2026-10-20", "2026-10-21", "2026-10-22", "2026-10-23", "2026-11-03"]
        self.assertEqual(self.streak(days), {"current": 1, "best": 4, "today_recorded": True})
        self.assertEqual(self.streak(["2026-10-30"])["current"], 0)
        self.assertEqual(self.streak([]), {"current": 0, "best": 0, "today_recorded": False})

    def test_future_dates_do_not_count(self):
        self.assertEqual(self.streak(["2026-11-03", "2026-11-04"])["current"], 1)


class StreakApiTests(ServerTestCase):
    def test_overview_streak_from_meals_body_logs_and_missions(self):
        a = self.login()
        self.create_meal(a, date="2026-11-01")
        a.post("/api/activities", {"date": "2026-11-02", "kind": "warmup"})
        ov = a.get("/api/overview?today=2026-11-03")[1]
        self.assertEqual(ov["streak"], {"current": 2, "best": 2, "today_recorded": False})
        a.post("/api/body-logs", {"client_id": "streakbody01", "date": "2026-11-03", "note": "가벼운 산책"})
        self.assertEqual(a.get("/api/overview?today=2026-11-03")[1]["streak"]["current"], 3)


if __name__ == "__main__":
    unittest.main()
