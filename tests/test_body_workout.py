"""Body status in settings, the AI workout routine, and the record streak on the Today tab."""
import unittest
from datetime import date

from backend.ai import AIError, validate_workout
from backend.app import App
from backend.logic import ValidationError, body_metrics, build_challenges, default_settings, normalize_settings, training_schedule
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
        # No rest days chosen yet: 2 workouts spread over the week (Mon, Thu).
        self.assertEqual(sent["training_weekdays"], ["월", "목"])
        self.assertEqual([s["weekday"] for s in plan["sessions"]], [0, 3])
        self.assertEqual(body["schedule"]["source"], "auto")
        self.assertTrue(all(s["minutes"] == 30 for s in plan["sessions"]))
        names = [e["name"] for s in plan["sessions"] for e in s["exercises"]]
        self.assertNotIn("벌칙 버피", names)
        self.assertEqual(plan["filtered_exercises"], 2)
        # Weights: rounded to 0.5kg, bodyweight moves have none, unsafe loads (400kg) are dropped.
        loads = {e["name"]: (e["weight_kg"], e["weight_note"]) for e in plan["sessions"][0]["exercises"]}
        self.assertEqual(loads["덤벨 스쿼트"], (4.0, "한 손 기준"))
        self.assertEqual(loads["제자리 걷기"], (None, "맨몸"))
        self.assertEqual(loads["바벨 데드리프트"][0], None)
        self.assertEqual(plan["dropped_weights"], 2)
        self.assertEqual(plan["format"], 3)
        self.assertNotIn("태워야", plan["summary"])
        self.assertFalse(plan["stale"])
        self.assertEqual(plan["model"], "fake-model")
        # Saved: survives a reload, and goes stale once the body values change.
        self.assertEqual(a.get("/api/workout-plan")[1]["plan"]["sessions"], plan["sessions"])
        # Changing body values starts the automatic AI review (no button); afterwards the plan reflects the new values.
        body = a.put("/api/settings", {"body": {"weight_kg": 64}})[1]
        self.assertTrue(body["routine_review_started"])
        self.assertEqual(self.fake.agent_calls[-1]["context"]["user"]["body"]["weight_kg"], 64.0)
        after = a.get("/api/workout-plan")[1]
        self.assertEqual(after["review"]["status"], "done")
        self.assertFalse(after["plan"]["stale"])
        self.fake.mode = "fail"  # if the review fails, the plan stays and is marked stale
        a.put("/api/settings", {"body": {"weight_kg": 63}})
        after = a.get("/api/workout-plan")[1]
        self.assertEqual((after["review"]["status"], after["plan"]["stale"]), ("failed", True))
        self.assertEqual(after["plan"]["sessions"], plan["sessions"])
        self.fake.mode = "ok"
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


class RestDayTests(unittest.TestCase):
    def test_rest_days_validation_and_count(self):
        s = normalize_settings({"rest_days": [6, 2, 2]}, default_settings(), "T")
        self.assertEqual((s["rest_days"], s["workouts_per_week"]), ([2, 6], 5))
        self.assertEqual(normalize_settings({"rest_days": []}, s, "T")["workouts_per_week"], 7)
        self.assertIsNone(normalize_settings({"rest_days": None}, s, "T")["rest_days"])
        for bad in ([0, 1, 2, 3, 4, 5, 6], [7], ["월"], [True], "2"):
            with self.subTest(bad=bad), self.assertRaises(ValidationError):
                normalize_settings({"rest_days": bad}, default_settings(), "T")

    def test_schedule_from_rest_days_or_spread_evenly(self):
        self.assertEqual(training_schedule({"rest_days": [2, 6]})["training_days"], [0, 1, 3, 4, 5])
        self.assertEqual(training_schedule({"rest_days": None, "workouts_per_week": 3})["training_days"], [0, 2, 4])
        self.assertEqual(training_schedule({"rest_days": None, "workouts_per_week": 5})["training_days"], [0, 1, 2, 4, 5])
        self.assertEqual(training_schedule({"rest_days": None, "workouts_per_week": None})["source"], "auto")

    def test_plan_must_cover_every_training_day(self):
        base = {"summary": "", "cautions": [], "questions": [], "sessions": [
            {"weekday": "월", "title": "하체", "focus": "", "minutes": 30, "intensity": "보통",
             "exercises": [{"name": "스쿼트", "amount": "2세트", "weight_kg": 0, "weight_note": "", "tip": ""}]}]}
        self.assertEqual(validate_workout(base, training_days=[0])["sessions"][0]["weekday"], 0)
        with self.assertRaises(AIError):
            validate_workout(base, training_days=[0, 2])  # Wednesday missing


def next_weekday(start: date, weekday: int) -> str:
    from datetime import timedelta
    return (start + timedelta(days=(weekday - start.weekday()) % 7)).isoformat()


class PinnedWeek(ServerTestCase):
    """Routines are saved per week: pin the server's date to the week the test dates are in (Mon 2026-11-02)."""

    def setUp(self):
        super().setUp()
        self.server.app.today_override = date(2026, 11, 2)


class WeeklyRoutineApiTests(PinnedWeek):
    def setUp(self):
        super().setUp()
        self.a = self.login()
        self.a.put("/api/settings", {"rest_days": [2, 6], "body": {"height_cm": 170, "weight_kg": 65}})

    def today_workout(self, weekday):
        return self.a.get(f"/api/overview?today={next_weekday(date(2026, 11, 2), weekday)}")[1]["today_workout"]

    def test_rest_days_shape_the_week_and_today(self):
        self.assertEqual(self.today_workout(0)["status"], "no_plan")
        self.assertEqual(self.today_workout(2)["status"], "rest")  # rest day even before any routine exists
        self.fake.workout_extra_day = True  # the AI also puts a session on Sunday (a rest day)
        status, body = self.a.post("/api/workout-plan", {})
        self.assertEqual(status, 200, body)
        self.assertEqual(self.fake.last_workout_input["training_weekdays"], ["월", "화", "목", "금", "토"])
        self.assertEqual(self.fake.last_workout_input["rest_weekdays"], ["수", "일"])
        self.assertEqual([s["weekday"] for s in body["plan"]["sessions"]], [0, 1, 3, 4, 5])
        monday = self.today_workout(0)
        self.assertEqual(monday["status"], "workout")
        self.assertEqual(monday["session"]["title"], "월요일 전신")
        self.assertEqual(self.today_workout(6)["status"], "rest")
        # Wednesday becomes a workout day: the automatic review must add it; the default fake reply
        # (keep, no change) fails the server check, so the plan stays as it was and is marked stale.
        self.a.put("/api/settings", {"rest_days": [6]})
        wednesday = self.today_workout(2)
        self.assertEqual((wednesday["status"], wednesday["stale"]), ("unscheduled", True))
        # Monday becomes a rest day: the user's rest day wins over the older routine.
        self.a.put("/api/settings", {"rest_days": [0, 6]})
        self.assertEqual(self.today_workout(0)["status"], "rest")


class ExerciseCheckTests(PinnedWeek):
    def test_checking_exercises_counts_as_a_movement_day(self):
        a = self.login()
        a.put("/api/settings", {"rest_days": [2, 6], "body": {"height_cm": 170, "weight_kg": 65}})
        a.post("/api/workout-plan", {})
        monday = next_weekday(date(2026, 11, 2), 0)
        ov = a.get(f"/api/overview?today={monday}")[1]
        tw = ov["today_workout"]
        keys = [ex["key"] for ex in tw["session"]["exercises"]]
        self.assertEqual((tw["checked"], ov["missions"]["movement"]), ([], False))
        status, body = a.put("/api/exercise-checks", {"date": monday, "item": keys[0], "done": True})
        self.assertEqual((status, body["checked"]), (200, [keys[0]]))
        ov = a.get(f"/api/overview?today={monday}")[1]
        self.assertTrue(ov["missions"]["movement"])
        self.assertEqual(ov["streak"]["current"], 1)
        self.assertEqual(next(i for i in ov["challenges"]["items"] if i["id"] == "movement_week")["current"], 1)
        # Checking it twice is harmless; un-checking the last one removes the movement day again.
        a.put("/api/exercise-checks", {"date": monday, "item": keys[0], "done": True})
        a.put("/api/exercise-checks", {"date": monday, "item": keys[0], "done": False})
        ov = a.get(f"/api/overview?today={monday}")[1]
        self.assertEqual((ov["today_workout"]["checked"], ov["missions"]["movement"]), ([], False))

    def test_rejects_items_not_in_todays_routine(self):
        a = self.login()
        a.put("/api/settings", {"rest_days": [2, 6], "body": {"height_cm": 170, "weight_kg": 65}})
        a.post("/api/workout-plan", {})
        monday, wednesday = next_weekday(date(2026, 11, 2), 0), next_weekday(date(2026, 11, 2), 2)
        for body in ({"date": monday, "item": "99:없는 운동", "done": True},
                     {"date": wednesday, "item": "0:제자리 걷기", "done": True},  # rest day
                     {"date": monday, "item": "0:제자리 걷기", "done": "yes"}):
            with self.subTest(body=body):
                self.assertEqual(a.put("/api/exercise-checks", body)[0], 400)
        # Another account can't tick this user's routine.
        b = self.login("사용자B")
        self.assertEqual(b.put("/api/exercise-checks", {"date": monday, "item": "0:제자리 걷기", "done": True})[0], 400)


class StaleServerTests(ServerTestCase):
    def test_unknown_api_says_restart_when_code_changed_after_start(self):
        a = self.login()
        status, body = a.get("/api/no-such-thing")
        self.assertEqual((status, body["error"]["code"]), (404, "not_found"))
        self.server.app._started_code_stamp = 0  # pretend the code on disk is newer than the running server
        status, body = a.get("/api/no-such-thing")
        self.assertEqual((status, body["error"]["code"]), (404, "restart_needed"))
        self.assertIn("다시 시작", body["error"]["message"])


class ChallengeTests(unittest.TestCase):
    M = {"total_record_days": 2, "best_streak": 2, "movement_week": 1, "meals_week": 5, "body_month": 0, "days_month": 2, "has_plan": False}

    def test_level_follows_experience_and_targets_scale(self):
        beginner = build_challenges({"experience": "처음이에요"}, self.M)
        advanced = build_challenges({"experience": "2년 이상"}, self.M)
        self.assertEqual((beginner["level"], advanced["level"]), ("입문", "상급"))
        target = lambda ch, cid: next(i["target"] for i in ch["items"] if i["id"] == cid)  # noqa: E731
        self.assertLess(target(beginner, "streak"), target(advanced, "streak"))
        self.assertLess(target(beginner, "meals_week"), target(advanced, "meals_week"))
        self.assertIn("first_plan", [i["id"] for i in beginner["items"]])
        self.assertNotIn("first_plan", [i["id"] for i in advanced["items"]])
        meals = next(i for i in beginner["items"] if i["id"] == "meals_week")
        self.assertEqual((meals["current"], meals["done"]), (5, True))
        self.assertEqual(beginner["done_count"], 1)

    def test_without_experience_level_comes_from_history_and_plan_sets_movement(self):
        self.assertEqual(build_challenges({}, self.M)["level"], "입문")
        self.assertEqual(build_challenges({}, {**self.M, "total_record_days": 40})["level"], "중급")
        ch = build_challenges({"experience": "6개월 미만", "workouts_per_week": 5}, self.M)
        move = next(i for i in ch["items"] if i["id"] == "movement_week")
        self.assertEqual((ch["level"], move["target"], move["current"]), ("초급", 5, 1))


class ChallengeApiTests(ServerTestCase):
    def test_overview_counts_stored_records(self):
        a = self.login()
        a.put("/api/settings", {"experience": "처음이에요"})
        for day in ("2026-11-02", "2026-11-03"):
            self.create_meal(a, date=day)
        a.post("/api/activities", {"date": "2026-11-03", "kind": "movement"})
        ch = a.get("/api/overview?today=2026-11-03")[1]["challenges"]
        items = {i["id"]: i for i in ch["items"]}
        self.assertEqual(ch["level"], "입문")
        self.assertEqual((items["meals_week"]["current"], items["movement_week"]["current"], items["streak"]["current"]), (2, 1, 2))
        self.assertFalse(items["first_plan"]["done"])


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
