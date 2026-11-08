"""Automatic AI routine review: feedback, settings changes and new weeks.

Checks: only the needed days change, past days and completed exercises are kept, next week is prepared
without a button, temporary feedback stays in its period, the same feedback never changes intensity twice,
pain blocks increases, and any failure keeps the existing plan."""
import unittest
from datetime import date, timedelta

from backend import routine_agent
from tests.test_api import ServerTestCase, agent_reply, agent_session

MON = date(2026, 11, 2)
WED, THU, SAT, SUN = (MON + timedelta(days=d) for d in (2, 3, 5, 6))
NEXT_MON = MON + timedelta(days=7)


class ParseTests(unittest.TestCase):
    def test_time_limits_from_feedback_text(self):
        self.assertEqual(routine_agent.parse_time_limits("내일은 20분만 가능해요", WED), [{"date": THU.isoformat(), "max_minutes": 20}])
        self.assertEqual(routine_agent.parse_time_limits("오늘은 15분 정도", WED), [{"date": WED.isoformat(), "max_minutes": 15}])
        week = routine_agent.parse_time_limits("이번 주만 바빠서 20분씩만 할게요", WED)
        self.assertEqual([x["date"] for x in week], [(WED + timedelta(days=i)).isoformat() for i in range(5)])
        self.assertEqual(routine_agent.parse_time_limits("너무 쉬웠어요", WED), [])
        self.assertTrue(routine_agent.has_pain("무릎이 아파서 못 했어요"))
        self.assertFalse(routine_agent.has_pain("약속 때문에 쉬었어요"))


class CheckerTests(unittest.TestCase):
    """The server checks on their own (no AI)."""

    def setUp(self):
        sq = {"name": "덤벨 스쿼트", "amount": "3세트 × 10회", "weight_kg": 10.0, "weight_note": "", "tip": ""}
        self.base = {"sessions": [{"weekday": d, "title": "전신", "focus": "", "minutes": 30, "intensity": "보통", "exercises": [dict(sq)]} for d in (0, 2, 3)]}
        self.ctx = {"week_dates": [(MON + timedelta(days=i)).isoformat() for i in range(7)], "first_editable": 2,
                    "user_rest_days": {1, 4, 5, 6}, "training_days": {0, 2, 3}, "minutes_limit": 40, "temp_limits": {},
                    "locked": {}, "weighted_ok": True, "pain_recent": False, "feedback_ids": {7}, "consumed_feedback": set(),
                    "records_new": False, "body_weight": 65.0, "base_loads": {"덤벨 스쿼트": 10.0}, "base_max_intensity": 1}

    def check(self, changes, **over):
        return routine_agent.check_proposal(self.base, agent_reply(changes=changes, summary=["테스트"], **over), self.ctx)

    def session(self, wd, kg=10.0, intensity="보통", minutes=30):
        return agent_session(routine_agent.WEEKDAY_NAMES[wd], intensity=intensity, minutes=minutes,
                             exercises=[{"name": "덤벨 스쿼트", "amount": "3세트 × 10회", "weight_kg": kg, "weight_note": "", "tip": ""}])

    def test_untouched_days_stay_identical(self):
        cand, errors, changed = self.check([{"weekday": "목", "type": "replace", "session": self.session(3, kg=11.0)}])
        self.assertEqual((errors, changed), ([], {3}))
        self.assertEqual([s for s in cand["sessions"] if s["weekday"] != 3], [s for s in self.base["sessions"] if s["weekday"] != 3])

    def test_rules(self):
        cases = [
            ([{"weekday": "월", "type": "replace", "session": self.session(0)}], {}, "지난 날"),
            ([{"weekday": "금", "type": "add", "session": self.session(4)}], {}, "휴식 요일"),
            ([{"weekday": "목", "type": "replace", "session": self.session(3, kg=12.0)}], {}, "10%"),
            ([{"weekday": "목", "type": "replace", "session": self.session(3, intensity="조금 힘들게", minutes=50)}], {}, "1회 가능 시간"),
            ([{"weekday": "목", "type": "replace", "session": self.session(3)}], {"decision": "increase"}, "이미 반영한"),
        ]
        for changes, over, expected in cases:
            with self.subTest(expected=expected):
                _, errors, _ = self.check(changes, **over)
                self.assertTrue(any(expected in e for e in errors), errors)
        self.ctx["pain_recent"] = True
        _, errors, _ = self.check([{"weekday": "목", "type": "replace", "session": self.session(3, kg=10.5)}])
        self.assertTrue(any("통증" in e for e in errors), errors)
        self.ctx.update(pain_recent=False, weighted_ok=False)
        _, errors, _ = self.check([{"weekday": "목", "type": "replace", "session": self.session(3, kg=10.5)}])
        self.assertTrue(any("중량 장비" in e for e in errors), errors)
        # an exercise already in the plan, unchanged, is not a new load (found by the browser test)
        _, errors, _ = self.check([{"weekday": "목", "type": "replace", "session": self.session(3, kg=10.0, minutes=25)}])
        self.assertEqual(errors, [])

    def test_rest_day_with_completed_work_is_not_a_conflict(self):
        """Today became a rest day after exercises were already done: keeping them must pass (found by the browser test)."""
        self.ctx.update(user_rest_days={1, 2, 4, 5, 6}, training_days={0, 3},
                        locked={2: {0: dict(self.base["sessions"][1]["exercises"][0])}})
        _, errors, _ = self.check([])
        self.assertEqual(errors, [])

    def test_new_feedback_or_new_records_allow_one_change(self):
        _, errors, _ = self.check([], decision="increase", feedback_ids=[7])
        self.assertEqual(errors, [])
        self.ctx["consumed_feedback"] = {7}
        _, errors, _ = self.check([], decision="increase", feedback_ids=[7])
        self.assertTrue(errors)
        self.ctx["records_new"] = True
        _, errors, _ = self.check([], decision="decrease", records=True)
        self.assertEqual(errors, [])


class ReviewTestCase(ServerTestCase):
    """Mon / Wed / Thu / Sat routine, 'today' is Wednesday."""

    def setUp(self):
        super().setUp()
        self.app = self.server.app
        self.app.today_override = WED
        self.a = self.login()
        self.a.put("/api/settings", {"rest_days": [1, 4, 6], "minutes_per_session": 30, "equipment": ["덤벨"],
                                     "body": {"height_cm": 170, "weight_kg": 65}})
        status, body = self.a.post("/api/workout-plan", {"today": WED.isoformat()})
        self.assertEqual(status, 200, body)
        self.plan0 = body["plan"]
        self.uid = self.a.get("/api/me")[1]["user"]["id"]

    @staticmethod
    def day(plan, wd):
        return next((s for s in plan["sessions"] if s["weekday"] == wd), None)

    def plan_now(self, today=None):
        today = today or self.app.today_override
        return self.a.get(f"/api/workout-plan?today={today.isoformat()}")[1]

    def feedback(self, text, today=None):
        status, body = self.a.put("/api/workout-feedback", {"date": (today or WED).isoformat(), "text": text})
        self.assertEqual(status, 200, body)
        return body

    def check_ex(self, day, key):
        self.assertEqual(self.a.put("/api/exercise-checks", {"date": day.isoformat(), "item": key, "done": True})[0], 200)


class DateBasedChangeTests(ReviewTestCase):
    def test_feedback_changes_only_needed_days_and_keeps_done_work(self):
        self.check_ex(WED, "0:제자리 걷기")  # one exercise already done today
        wed0 = self.day(self.plan0, 2)

        def script(ctx, steps):
            fid = ctx["feedback"][0]["id"]
            thu = agent_session("목", title="짧은 전신", minutes=20)
            wed = agent_session("수", exercises=[dict(wed0["exercises"][0], weight_kg=0), {"name": "플랭크", "amount": "2세트 × 20초", "weight_kg": 0, "weight_note": "", "tip": ""}])
            note = {"feedback_id": fid, "scope": "day", "kind": "time_limit", "applies_from": THU.isoformat(), "applies_to": THU.isoformat(), "max_minutes": 20}
            turn = len(steps)
            if turn == 0:  # draft breaks the 20-minute limit
                return agent_reply("check_plan", "reschedule", [{"weekday": "목", "type": "replace", "session": agent_session("목", minutes=30)}], ["목요일 조정"], [fid])
            if turn == 1:  # also tries to rewrite Monday (past)
                return agent_reply("submit", "reschedule", [{"weekday": "월", "type": "replace", "session": agent_session("월")}, {"weekday": "목", "type": "replace", "session": thu}], ["월·목 조정"], [fid])
            return agent_reply("submit", "mixed", [{"weekday": "수", "type": "replace", "session": wed}, {"weekday": "목", "type": "replace", "session": thu}],
                               ["내일은 20분만 가능하다고 해서 목요일을 20분으로 줄였어요.", "오늘 남은 운동을 가볍게 바꿨어요."], [fid], notes=[note])
        self.fake.agent_script = script
        body = self.feedback("내일은 20분만 가능해요")
        self.assertEqual(body["review"]["status"], "done", body["review"])
        calls = self.fake.agent_calls
        self.assertEqual(len(calls), 3)
        self.assertIn("20분 이하", calls[1]["steps"][0])              # check_plan failure went back to the AI
        self.assertIn("지난 날이라", calls[2]["steps"][1])             # rejected submit went back too
        ctx = calls[0]["context"]
        self.assertEqual(ctx["editable_from"], "수")
        self.assertEqual(ctx["week"][3]["time_limit_minutes"], 20)
        self.assertTrue(ctx["week"][2]["session"]["exercises"][0]["done"])

        plan = self.plan_now()["plan"]
        self.assertEqual(self.day(plan, 0), self.day(self.plan0, 0))   # Monday (past) unchanged
        self.assertEqual(self.day(plan, 5), self.day(self.plan0, 5))   # Saturday: no reason to change
        self.assertEqual(self.day(plan, 3)["minutes"], 20)
        self.assertEqual(self.day(plan, 2)["exercises"][0], wed0["exercises"][0])  # done exercise kept in place
        adj = plan["adjustments"][0]
        self.assertEqual((adj["source"], adj["decision"], adj["changed_days"], adj["trigger"]), ("agent", "mixed", [2, 3], "feedback"))
        self.assertIn("20분", adj["summary"][0])
        # Today's workout and the weekly routine show the same saved session; the done mark is kept.
        tw = self.a.get(f"/api/overview?today={WED.isoformat()}")[1]["today_workout"]
        self.assertEqual([e["name"] for e in tw["session"]["exercises"]], [e["name"] for e in self.day(plan, 2)["exercises"]])
        self.assertEqual(tw["checked"], ["0:제자리 걷기"])
        self.assertEqual(tw["feedback"]["label"], "내일은 20분만 가능해요")
        self.assertEqual(tw["latest_adjustment"]["summary"], adj["summary"])
        self.assertEqual(self.a.get(f"/api/overview?today={WED.isoformat()}")[1]["streak"]["today_recorded"], True)

    def test_empty_feedback_means_no_feedback(self):
        body = self.feedback("")
        self.assertEqual((body["feedback"]["text"], body["feedback"]["label"]), ("", "피드백 없음"))
        self.assertEqual(self.fake.agent_calls[-1]["context"]["feedback"][0]["text"], "피드백 없음")
        self.assertEqual(self.plan_now()["plan"]["sessions"], self.plan0["sessions"])  # default reply: keep


class NextWeekTests(ReviewTestCase):
    def test_new_week_is_prepared_and_temporary_limits_end(self):
        self.feedback("내일은 20분만 가능해요")
        self.fake.agent_script = None
        thu_short = agent_session("목", minutes=20)
        self.fake.agent_script = [agent_reply("submit", "reschedule", [{"weekday": "목", "type": "replace", "session": thu_short}], ["목요일 20분"])]
        self.feedback("내일은 20분만 가능해요!")
        self.assertEqual(self.day(self.plan_now()["plan"], 3)["minutes"], 20)

        def new_week(ctx, steps):
            self.assertEqual(ctx["mode"], "new_week")
            self.assertIsNone(ctx["week"][3]["time_limit_minutes"])     # last week's "내일만 20분" does not carry over
            self.assertEqual(ctx["week"][3]["session"]["minutes"], 30)  # the server already put the normal Thursday back
            self.assertEqual(ctx["restored_after_temporary_feedback"], ["목"])
            self.assertEqual(ctx["last_week_results"]["week_start"], MON.isoformat())
            return agent_reply("submit", "keep", [], ["지난주 결과를 보고 지금 루틴을 유지해요."])  # the AI changes nothing
        self.fake.agent_script = new_week
        self.app.today_override = NEXT_MON
        payload = self.plan_now()                                       # first visit of the new week, no button
        self.assertEqual(payload["review"]["status"], "done", payload["review"])
        new = payload["plan"]
        self.assertEqual((new["week_start"], self.day(new, 3)), (NEXT_MON.isoformat(), self.day(self.plan0, 3)))  # normal version
        self.assertTrue(self.a.get(f"/api/overview?today={NEXT_MON.isoformat()}")[1]["today_workout"]["session"])
        self.app.today_override = WED                                   # last week's record stays as it was
        self.assertEqual(self.day(self.plan_now()["plan"], 3)["minutes"], 20)
        self.assertNotIn("auto", {a.get("source") for a in new["adjustments"]})  # the old fixed weekly rule never runs

    def test_next_week_ready_before_monday(self):
        self.app.today_override = SUN                                   # nothing left this week
        calls = len(self.fake.agent_calls)
        payload = self.plan_now()
        self.assertTrue(payload["next_week"]["ready"])
        self.assertEqual(self.fake.agent_calls[-1]["context"]["week_start"], NEXT_MON.isoformat())
        self.app.today_override = NEXT_MON
        calls = len(self.fake.agent_calls)
        self.assertEqual(self.plan_now()["plan"]["week_start"], NEXT_MON.isoformat())
        self.assertEqual(len(self.fake.agent_calls), calls)             # already prepared: not generated again


class DuplicateAndSafetyTests(ReviewTestCase):
    def test_same_feedback_changes_intensity_only_once(self):
        harder = agent_session("목", intensity="보통")
        self.fake.agent_script = lambda ctx, steps: agent_reply("submit", "increase", [{"weekday": "목", "type": "replace", "session": harder}],
                                                                ["너무 쉬웠다고 해서 목요일 강도를 한 단계 올렸어요."], [ctx["feedback"][0]["id"]])
        self.feedback("너무 쉬웠어요")
        plan = self.plan_now()["plan"]
        self.assertEqual(self.day(plan, 3)["intensity"], "보통")
        # Nothing new: no AI call at all.
        calls = len(self.fake.agent_calls)
        self.assertEqual(self.feedback("너무 쉬웠어요")["changed"], False)
        self.assertEqual(self.app._run_agent_job(self.uid, MON, "feedback", WED), "up_to_date")
        self.assertEqual(len(self.fake.agent_calls), calls)
        # Another trigger (settings) must not raise it again for the same feedback.
        hardest = agent_session("목", intensity="조금 힘들게")
        self.fake.agent_script = lambda ctx, steps: agent_reply("submit", "increase", [{"weekday": "목", "type": "replace", "session": hardest}],
                                                                ["또 올렸어요"], [ctx["feedback"][0]["id"]])
        self.a.put("/api/settings", {"minutes_per_session": 35})
        payload = self.plan_now()
        self.assertEqual(payload["review"]["status"], "failed")
        self.assertIn("3번", payload["review"]["message"])
        self.assertEqual(self.day(payload["plan"], 3)["intensity"], "보통")  # kept
        self.assertTrue(self.fake.agent_calls[-1]["context"]["feedback"][0]["intensity_already_applied"])

    def test_pain_blocks_increase(self):
        harder = agent_session("목", intensity="보통")
        self.fake.agent_script = [agent_reply("submit", "increase", [{"weekday": "목", "type": "replace", "session": harder}], ["올림"]),
                                  agent_reply("submit", "keep", [], ["무릎 통증이 있어 강도는 그대로 두었어요."])]
        self.feedback("무릎이 아파서 못 했어요")
        self.assertTrue(any("통증" in s for s in self.fake.agent_calls[-1]["steps"]))
        plan = self.plan_now()
        self.assertEqual((plan["review"]["status"], self.day(plan["plan"], 3)["intensity"]), ("done", "가볍게"))

    def test_ai_failure_keeps_plan_and_feedback(self):
        self.fake.mode = "fail"
        body = self.feedback("힘들어서 못 했어요")
        self.assertEqual(body["feedback"]["text"], "힘들어서 못 했어요")
        payload = self.plan_now()
        self.assertEqual(payload["review"]["status"], "failed")
        self.assertIn("기존 계획", payload["review"]["message"])
        self.assertEqual(payload["plan"]["sessions"], self.plan0["sessions"])

    def test_loop_is_limited_and_history_tool_works(self):
        self.fake.agent_script = lambda ctx, steps: agent_reply("get_history", weeks=2)
        self.feedback("약속 때문에 쉬었어요")
        self.assertEqual(len(self.fake.agent_calls), routine_agent.MAX_TURNS)
        self.assertIn("get_history(weeks=2) 결과", self.fake.agent_calls[-1]["steps"][0])
        payload = self.plan_now()
        self.assertEqual(payload["review"]["status"], "failed")
        self.assertIn("정해진 단계", payload["review"]["message"])
        self.assertEqual(payload["plan"]["sessions"], self.plan0["sessions"])


if __name__ == "__main__":
    unittest.main()
