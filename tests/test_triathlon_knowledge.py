from __future__ import annotations

import datetime
import json
import unittest
from unittest.mock import patch

from app import coach, hevy_connector
from app import triathlon_knowledge as tk

D = datetime.date
TODAY = D(2026, 11, 9)          # a Monday
RACE = D(2027, 5, 30)           # a Sunday

TOPIC_FIELDS = {"title", "evidence", "pros", "research", "for_him", "do_not"}


def row(date: str, discipline: str, title: str, minutes: int = 60, intensity: str = "",
        phase: str = "base") -> dict:
    return {"date": date, "discipline": discipline, "title": title, "duration_min": minutes,
            "intensity": intensity, "phase": phase, "is_rest": 0}


def check(text: str, *, today: datetime.date = TODAY, plan=(), phase: str | None = "base",
          race: datetime.date | None = RACE) -> dict | None:
    return tk.night_out_check(text, today=today, race_date=race, plan_days=plan,
                              block_phase=phase)


class ReferenceTests(unittest.TestCase):
    def test_every_topic_carries_the_same_six_parts(self) -> None:
        self.assertEqual(len(tk.TOPICS), 14)
        for key, topic in tk.TOPICS.items():
            self.assertEqual(set(topic), TOPIC_FIELDS, key)
            self.assertTrue(topic["title"] and topic["evidence"], key)
            for part in ("pros", "research", "for_him", "do_not"):
                self.assertIsInstance(topic[part], list, f"{key}.{part}")
                self.assertTrue(all(isinstance(line, str) and line for line in topic[part]),
                                f"{key}.{part}")
            for part in ("pros", "for_him", "do_not"):
                self.assertTrue(topic[part], f"{key}.{part}")
            # No study is invented to fill a gap: a topic with no research behind it
            # has to say that it rests on practice.
            if not topic["research"]:
                self.assertIn("practice", topic["evidence"].lower(), key)

    def test_every_route_names_a_real_topic_and_every_topic_is_routed(self) -> None:
        self.assertEqual({key for key, _, _ in tk._ROUTES}, set(tk.TOPICS))
        self.assertLessEqual(tk.TRI_ONLY, set(tk.TOPICS))

    def test_no_em_dash_reaches_the_coach(self) -> None:
        text = json.dumps([tk.TOPICS, tk.HOW_TO_ANSWER, tk.HOW_TO_USE, tk.LIMITS,
                           tk.DAMAGE_CONTROL_ALCOHOL, tk.DAMAGE_CONTROL_LATE,
                           tk.DAMAGE_CONTROL_IF_DRINKING], ensure_ascii=False)
        self.assertNotIn("—", text)
        self.assertNotIn("–", text)

    def test_a_question_about_method_gets_that_topic(self) -> None:
        self.assertEqual(tk.topics_for("how do the Norwegians train")[0], "intensity")
        self.assertEqual(tk.topics_for("should I try baking soda before the race")[0],
                         "supplements")
        self.assertEqual(tk.topics_for("how do I get faster in the swim")[0], "swim")
        self.assertIn("taper", tk.topics_for("what does race week look like"))
        self.assertIn("recovery", tk.topics_for("I missed my long run, should I make it up"))

    def test_a_scheduling_turn_gets_no_reference(self) -> None:
        self.assertEqual(tk.topics_for("what's my workout Tuesday"), [])
        self.assertIsNone(tk.context_for("what's my workout Tuesday"))
        self.assertNotIn("swim", tk.topics_for("move my swim to Tuesday"))
        self.assertEqual(tk.topics_for(""), [])
        self.assertEqual(tk.topics_for(None), [])

    def test_at_most_three_topics_and_the_limit_is_honoured(self) -> None:
        text = ("how should I pace the bike leg and the run leg, taper, sleep, fuel plan, "
                "sauna, bicarb and the brick")
        self.assertEqual(len(tk.topics_for(text)), 3)
        self.assertEqual(len(tk.topics_for(text, limit=1)), 1)
        self.assertEqual(tk.topics_for(text, limit=0), [])
        self.assertEqual(len(tk.context_for(text)["topics"]), 3)

    def test_triathlon_only_topics_drop_out_of_another_event(self) -> None:
        text = "how should I pace the bike leg and how long is the taper"
        self.assertIn("bike", tk.topics_for(text))
        marathon = tk.topics_for(text, mode="MARATHON")
        self.assertNotIn("bike", marathon)
        self.assertIn("taper", marathon)
        self.assertEqual(tk.topics_for("how do I pace the bike leg", mode="marathon"), [])

    def test_drinking_on_the_bike_is_fuel_and_drinking_on_friday_is_alcohol(self) -> None:
        on_bike = tk.topics_for("what should I drink on the bike")
        self.assertEqual(on_bike[0], "fuel")
        self.assertNotIn("alcohol", on_bike)
        self.assertEqual(tk.topics_for("should I drink more on long rides"), ["fuel"])
        self.assertEqual(tk.topics_for("can I drink tonight"), ["alcohol"])
        self.assertEqual(tk.topics_for("party friday"), ["alcohol"])
        self.assertIn("alcohol", tk.topics_for("I'm hungover, what do I do about today's run"))

    def test_context_carries_the_review_date_and_how_to_use_it(self) -> None:
        context = tk.context_for("can I have a few beers saturday")
        self.assertEqual(context["reviewed"], tk.REVIEWED)
        self.assertEqual(context["how_to_use"], tk.HOW_TO_USE)
        self.assertEqual(list(context["topics"]), ["alcohol"])
        self.assertIs(context["topics"]["alcohol"], tk.TOPICS["alcohol"])


class DetectionTests(unittest.TestCase):
    def test_a_night_out_ahead_is_recognised(self) -> None:
        for text in (
            "thinking about drinking friday",
            "can I go out and drink tonight",
            "friday night, can I drink",
            "going out sat night",
            "I'll be up late studying tonight",
            "can I go out",
            "party friday",
        ):
            self.assertTrue(tk.is_night_out_query(text), text)

    def test_sport_and_the_past_are_not_a_night_out(self) -> None:
        for text in (
            "I took 3 shots on goal",
            "I had a late night",
            "I stayed up till 3am",
            "can I go out too hard on the swim",
            "should I go out for a run",
            "I've drunk 2 litres of water",
            "clif shot bloks",
            "how much should I drink before my long run",
            "",
        ):
            self.assertFalse(tk.is_night_out_query(text), text)

    def test_alcohol_is_told_apart_from_fluid(self) -> None:
        self.assertTrue(tk.mentions_alcohol("a few beers with the boys"))
        self.assertTrue(tk.mentions_alcohol("can I drink friday"))
        self.assertFalse(tk.mentions_alcohol("how much should I drink on the bike"))
        self.assertFalse(tk.mentions_alcohol("I'll be up late studying tonight"))


class NightDateTests(unittest.TestCase):
    def nights(self, text: str, today: datetime.date = TODAY) -> list[datetime.date]:
        return tk.night_dates(text, today=today, race_date=RACE)

    def test_relative_days(self) -> None:
        self.assertEqual(self.nights("can I drink tonight"), [TODAY])
        self.assertEqual(self.nights("can I drink tomorrow night"), [D(2026, 11, 10)])
        self.assertEqual(self.nights("can I drink friday"), [D(2026, 11, 13)])
        self.assertEqual(self.nights("party sat"), [D(2026, 11, 14)])
        self.assertEqual(self.nights("can I drink next friday"), [D(2026, 11, 20)])
        self.assertEqual(self.nights("can I drink in 3 days"), [D(2026, 11, 12)])
        self.assertEqual(self.nights("can I drink this weekend"),
                         [D(2026, 11, 13), D(2026, 11, 14)])

    def test_a_night_with_no_day_named_is_tonight(self) -> None:
        self.assertEqual(self.nights("can I go out and drink"), [TODAY])

    def test_calendar_dates(self) -> None:
        self.assertEqual(self.nights("night out 2026-11-21"), [D(2026, 11, 21)])
        self.assertEqual(self.nights("party on november 21"), [D(2026, 11, 21)])
        self.assertEqual(self.nights("party on 21 november"), [D(2026, 11, 21)])

    def test_the_race_anchors_its_own_nights(self) -> None:
        self.assertEqual(self.nights("drinks the night before the race"), [D(2027, 5, 29)])
        self.assertEqual(self.nights("beers after the race"), [RACE])

    def test_only_the_clause_about_the_night_is_read(self) -> None:
        self.assertEqual(self.nights("can I drink friday? I have a long run saturday"),
                         [D(2026, 11, 13)])
        self.assertEqual(self.nights("can I drink friday, or is saturday better"),
                         [D(2026, 11, 13), D(2026, 11, 14)])

    def test_an_earlier_night_stands_until_a_follow_up_replaces_it(self) -> None:
        self.assertEqual(self.nights("can I drink friday\nok thanks, what's my swim tomorrow"),
                         [D(2026, 11, 13)])
        self.assertEqual(self.nights("can I drink friday\nwhat about saturday instead"),
                         [D(2026, 11, 14)])
        self.assertEqual(self.nights("can I drink friday\nsaturday?"), [D(2026, 11, 14)])

    def test_past_nights_and_other_questions_give_nothing(self) -> None:
        self.assertEqual(self.nights("night out 2026-11-01"), [])
        self.assertEqual(self.nights("I had a late night last night, can I still do the long run"),
                         [])
        self.assertEqual(self.nights("what is my workout today"), [])
        self.assertEqual(self.nights(""), [])
        self.assertEqual(tk.night_dates("can I drink tonight", today="not a date"), [])

    def test_weekday_abbreviations_are_not_read_out_of_other_words(self) -> None:
        self.assertEqual(self.nights("I sat around all day, can I drink tonight"), [TODAY])
        self.assertEqual(self.nights("too much sun today, can I have a beer tonight"), [TODAY])

    def test_today_may_be_a_date_or_an_iso_string(self) -> None:
        self.assertEqual(tk.night_dates("can I drink friday", today="2026-11-09"),
                         [D(2026, 11, 13)])


class KeySessionTests(unittest.TestCase):
    def kind(self, discipline: str, title: str, minutes: int = 60, intensity: str = "",
             **extra) -> str | None:
        return tk.key_kind({**row("2026-11-14", discipline, title, minutes, intensity), **extra})

    def test_quality_long_brick_test_and_race_are_key(self) -> None:
        self.assertEqual(self.kind("bike", "Bike tempo"), "quality")
        self.assertEqual(self.kind("run", "Long run", 80), "long")
        self.assertEqual(self.kind("run", "Easy run", 95), "long")
        self.assertEqual(self.kind("bike", "Endurance ride", 180), "long")
        self.assertEqual(self.kind("brick", "Bike into run"), "brick")
        self.assertEqual(self.kind("swim", "CSS test: 400 + 200"), "test")
        self.assertEqual(self.kind("race", "IRONMAN 70.3 Victoria"), "race")

    def test_easy_rest_and_strength_days_are_not(self) -> None:
        self.assertIsNone(self.kind("swim", "Easy aerobic swim (technique, not CSS)"))
        self.assertIsNone(self.kind("run", "Easy run", 45, "Z1-2 conversational, no threshold sets"))
        self.assertIsNone(self.kind("run", "Recovery jog, nothing hard", 30))
        self.assertIsNone(self.kind("strength", "Full body, heavy and hard"))
        self.assertIsNone(self.kind("rest", "Rest"))
        self.assertIsNone(self.kind("swim", "Long swim", 70))
        self.assertIsNone(self.kind("run", "Tempo run", is_rest=1))
        self.assertIsNone(tk.key_kind(None))
        self.assertIsNone(tk.key_kind("Long run"))


class VerdictTests(unittest.TestCase):
    LONG_RIDE = row("2026-11-14", "bike", "Long ride", 180)
    LONG_RUN = row("2026-11-15", "run", "Long run", 100)
    EASY = [row(f"2026-11-{day:02d}", "swim", "Easy aerobic swim", 45) for day in (10, 11, 13)]

    def one(self, text: str, **kwargs) -> dict:
        result = check(text, **kwargs)
        self.assertIsNotNone(result, text)
        self.assertEqual(len(result["nights"]), 1, text)
        return result["nights"][0]

    def test_fourteen_days_out_is_a_flat_no(self) -> None:
        for days in (14, 12, 2):
            today = RACE - datetime.timedelta(days=days)
            result = check("can I drink tonight", today=today, phase="taper")
            night = result["nights"][0]
            self.assertEqual(night["verdict"], "hard_no", days)
            self.assertEqual(night["days_to_race"], days)
            self.assertIn(f"{days} days to the race", night["reason"])
            self.assertEqual(list(result["how_to_answer"]), ["hard_no"])
            self.assertNotIn("damage_control", result)
            self.assertNotIn("clear_nights", result)

    def test_a_hard_no_holds_whatever_the_plan_says(self) -> None:
        today = RACE - datetime.timedelta(days=10)
        tomorrow = (today + datetime.timedelta(days=1)).isoformat()
        night = self.one("can I drink tonight", today=today, phase="base",
                         plan=[row(tomorrow, "rest", "Rest")])
        self.assertEqual(night["verdict"], "hard_no")

    def test_the_night_before_the_race_is_a_hard_no(self) -> None:
        night = self.one("drinks the night before the race")
        self.assertEqual(night["verdict"], "hard_no")
        self.assertEqual(night["reason"], "The race is tomorrow.")

    def test_fifteen_days_out_is_a_yes_kept_light(self) -> None:
        night = self.one("can I drink tonight", today=RACE - datetime.timedelta(days=15),
                         phase="peak")
        self.assertEqual(night["verdict"], "go")
        self.assertTrue(night["keep_it_light"])

    def test_race_night_is_a_celebration(self) -> None:
        result = check("beers after the race")
        self.assertEqual(result["nights"][0]["verdict"], "celebrate")
        self.assertEqual(result["nights"][0]["days_to_race"], 0)
        self.assertEqual(result["damage_control"], list(tk.DAMAGE_CONTROL_ALCOHOL))

    def test_a_clear_night_is_a_plain_yes(self) -> None:
        night = self.one("can I drink friday",
                         plan=[row("2026-11-14", "swim", "Easy aerobic swim", 45)])
        self.assertEqual(night["verdict"], "go")
        self.assertEqual(night["weekday"], "Friday")
        self.assertEqual(night["reason"], "Nothing important sits behind it on Saturday.")
        self.assertEqual(night["next_day"]["title"], "Easy aerobic swim")
        self.assertIsNone(night["next_day"]["key"])
        self.assertNotIn("keep_it_light", night)

    def test_no_plan_row_is_a_yes_that_says_so(self) -> None:
        night = self.one("can I drink friday")
        self.assertEqual(night["verdict"], "go")
        self.assertEqual(night["reason"], "No session on record for Saturday.")
        self.assertIsNone(night["next_day"])

    def test_a_key_session_early_in_the_block_moves(self) -> None:
        night = self.one("can I drink friday", plan=[self.LONG_RIDE, self.LONG_RUN])
        self.assertEqual(night["verdict"], "go_and_move")
        self.assertIn("the long ride", night["reason"])
        self.assertEqual(night["next_day"]["key"], "long")
        self.assertEqual(night["day_after"]["title"], "Long run")

    def test_a_key_session_in_build_holds_and_offers_other_nights(self) -> None:
        plan = [{**self.LONG_RIDE, "phase": "build"}, self.LONG_RUN, *self.EASY]
        result = check("can I drink friday", plan=plan, phase="build")
        night = result["nights"][0]
        self.assertEqual(night["verdict"], "not_this_night")
        self.assertIn("the long ride", night["reason"])
        self.assertEqual([item["weekday"] for item in result["clear_nights"]],
                         ["Monday", "Tuesday", "Thursday"])
        self.assertNotIn("damage_control", result)
        self.assertEqual(list(result["how_to_answer"]), ["not_this_night"])

    def test_the_day_row_decides_the_phase_before_the_block_does(self) -> None:
        strict = self.one("can I drink friday", phase="base",
                          plan=[{**self.LONG_RIDE, "phase": "specific"}])
        self.assertEqual(strict["verdict"], "not_this_night")
        loose = self.one("can I drink friday", phase="build",
                         plan=[{**self.LONG_RIDE, "phase": "recovery"}])
        self.assertEqual(loose["verdict"], "go_and_move")

    def test_without_a_phase_the_last_twelve_weeks_are_strict(self) -> None:
        ride = {**self.LONG_RIDE, "phase": None}
        far = self.one("can I drink friday", phase=None, plan=[ride])
        self.assertEqual(far["verdict"], "go_and_move")
        today = RACE - datetime.timedelta(days=60)
        tomorrow = (today + datetime.timedelta(days=1)).isoformat()
        near = self.one("can I drink tonight", today=today, phase=None,
                        plan=[{**ride, "date": tomorrow}])
        self.assertEqual(near["verdict"], "not_this_night")

    def test_a_race_the_next_morning_is_a_no(self) -> None:
        night = self.one("can I drink friday",
                         plan=[row("2026-11-14", "race", "Local sprint triathlon")])
        self.assertEqual(night["verdict"], "not_this_night")
        self.assertEqual(night["reason"], "Saturday is a race.")

    def test_training_on_the_day_itself_is_reported(self) -> None:
        night = self.one("can I drink saturday",
                         plan=[self.LONG_RIDE, row("2026-11-15", "rest", "Rest")])
        self.assertEqual(night["verdict"], "go")
        self.assertEqual(night["trained_that_day"]["title"], "Long ride")

    def test_what_was_asked_decides_the_advice_that_comes_with_a_yes(self) -> None:
        drink = check("can I drink friday")
        self.assertEqual(drink["asked_about"], "alcohol")
        self.assertEqual(drink["damage_control"], list(tk.DAMAGE_CONTROL_ALCOHOL))
        party = check("party friday")
        self.assertEqual(party["asked_about"], "night_out")
        self.assertEqual(party["damage_control"],
                         [*tk.DAMAGE_CONTROL_LATE, tk.DAMAGE_CONTROL_IF_DRINKING])
        late = check("I'll be up late studying tonight")
        self.assertEqual(late["asked_about"], "late_night")
        self.assertEqual(late["damage_control"], list(tk.DAMAGE_CONTROL_LATE))

    def test_each_night_asked_about_gets_its_own_verdict(self) -> None:
        result = check("can I drink friday, or is saturday better",
                       plan=[{**self.LONG_RIDE, "phase": "build"},
                             row("2026-11-15", "rest", "Rest", 0)], phase="build")
        self.assertEqual([night["verdict"] for night in result["nights"]],
                         ["not_this_night", "go"])
        self.assertEqual(list(result["how_to_answer"]), ["not_this_night", "go"])
        self.assertIn("damage_control", result)

    def test_no_race_date_still_answers(self) -> None:
        night = self.one("can I drink friday", race=None, plan=[self.LONG_RIDE], phase=None)
        self.assertIsNone(night["days_to_race"])
        self.assertEqual(night["verdict"], "go_and_move")

    def test_the_result_names_its_own_limits_and_is_json(self) -> None:
        result = check("can I drink friday", plan=[self.LONG_RIDE])
        self.assertEqual(result["limits"], tk.LIMITS)
        self.assertNotIn("—", json.dumps(result, ensure_ascii=False))
        self.assertIsNone(check("what is my workout today"))

    def test_a_plan_title_with_an_em_dash_is_tidied(self) -> None:
        night = self.one("can I drink friday",
                         plan=[row("2026-11-14", "swim", "Easy swim — technique", 45)])
        self.assertEqual(night["next_day"]["title"], "Easy swim: technique")

    def test_the_hard_no_is_blunt_about_the_night_and_never_about_him(self) -> None:
        text = tk.HOW_TO_ANSWER["hard_no"]
        self.assertIn("GET TO BED", text)
        self.assertIn("never an insult about him", text)
        self.assertEqual(set(tk.HOW_TO_ANSWER), set(tk.VERDICTS))


class CoachPromptTests(unittest.TestCase):
    def test_the_strict_and_forgiving_rules_sit_before_the_reply_style(self) -> None:
        prompt = coach._SYSTEM
        strict = prompt.index("STRICT WHERE IT COUNTS, FORGIVING WHERE IT DOES NOT")
        pros = prompt.index("WHAT THE BEST LONG-COURSE ATHLETES DO")
        style = prompt.index("FINAL RULE: RESPONSE STYLE")
        self.assertLess(strict, pros)
        self.assertLess(pros, style)

    def test_the_prompt_names_both_context_keys_and_the_fallback_rule(self) -> None:
        prompt = coach._SYSTEM
        self.assertIn("`night_out_check`", prompt)
        self.assertIn("`pro_reference`", prompt)
        self.assertIn("14 days or fewer to", prompt)
        self.assertIn("Get to bed.", prompt)
        self.assertIn("Never suggest that he drink", prompt)
        self.assertIn("One slip is data, not a character flaw.", prompt)

    def test_the_prompt_keeps_its_older_promises(self) -> None:
        prompt = coach._SYSTEM
        self.assertNotIn("—", prompt)
        self.assertNotIn("never negotiable", prompt)
        self.assertIn("Do not moralize", prompt)
        self.assertIn(f"reviewed {tk.REVIEWED}", prompt)


class CoachContextTests(unittest.TestCase):
    """The reference and the verdict reach the model only on turns that ask for them."""

    def payload(self, query: str, *, now: datetime.datetime, phase: dict, plan=()) -> dict:
        live = {"readiness": {}, "load": {"activities": [], "by_sport": {}},
                "training_load": {}, "fitness": {}, "weight": {}, "pmc": {}, "zones": {}}
        previous = hevy_connector.connector()
        hevy_connector.reset()
        self.addCleanup(hevy_connector.configure, previous)
        rows = list(plan)
        with (
            patch.object(coach, "_live_context", return_value=live),
            patch.object(coach.config, "local_now", return_value=now),
            patch.object(coach.config, "race_phase", return_value=phase),
            patch.object(coach.rings, "t100_readiness", return_value={}),
            patch.object(coach.suggest, "todays_suggestion", return_value={}),
            patch("app.baselines.get_baselines", return_value={}),
            patch("app.insights.get_insights", return_value={}),
            patch.object(coach.db, "get_plan",
                         side_effect=lambda start, end: [r for r in rows
                                                         if start <= r["date"] <= end]),
            patch.object(coach.db, "get_constraints", return_value=[]),
            patch.object(coach.db, "get_constraint_history", return_value=[]),
            patch.object(coach.db, "get_plan_day", return_value=None),
        ):
            return json.loads(coach._context_block(query))

    @staticmethod
    def phase(days: int, block: str, mode: str = "TRIATHLON") -> dict:
        return {"name": "IRONMAN 70.3 Victoria", "date": RACE.isoformat(),
                "days_remaining": days, "weeks_remaining": days // 7, "is_past": False,
                "phase": "taper" if days <= 14 else "build", "mode": mode,
                "block_phase": block, "distances": {"swim_km": 1.9, "bike_km": 90, "run_km": 21.1}}

    def test_twelve_days_out_the_coach_is_handed_a_hard_no(self) -> None:
        now = datetime.datetime(2027, 5, 18, 9, 0, tzinfo=datetime.timezone.utc)
        payload = self.payload("can I go out drinking tonight", now=now,
                               phase=self.phase(12, "taper"))
        check_ = payload["night_out_check"]
        self.assertEqual(check_["asked_about"], "alcohol")
        self.assertEqual(check_["nights"][0]["verdict"], "hard_no")
        self.assertEqual(check_["nights"][0]["days_to_race"], 12)
        self.assertIn("alcohol", payload["pro_reference"]["topics"])

    def test_months_out_with_nothing_behind_the_night_it_is_a_yes(self) -> None:
        now = datetime.datetime(2026, 11, 9, 9, 0, tzinfo=datetime.timezone.utc)
        payload = self.payload(
            "can I drink friday", now=now, phase=self.phase(202, "base"),
            plan=[row("2026-11-14", "swim", "Easy aerobic swim", 45)])
        night = payload["night_out_check"]["nights"][0]
        self.assertEqual(night["night"], "2026-11-13")
        self.assertEqual(night["verdict"], "go")
        self.assertEqual(night["next_day"]["title"], "Easy aerobic swim")

    def test_the_plan_row_behind_the_night_reaches_the_check(self) -> None:
        now = datetime.datetime(2026, 11, 9, 9, 0, tzinfo=datetime.timezone.utc)
        payload = self.payload(
            "can I drink friday", now=now, phase=self.phase(202, "build"),
            plan=[row("2026-11-14", "bike", "Long ride", 180, phase="build")])
        self.assertEqual(payload["night_out_check"]["nights"][0]["verdict"], "not_this_night")

    def test_an_ordinary_turn_carries_neither(self) -> None:
        now = datetime.datetime(2026, 11, 9, 9, 0, tzinfo=datetime.timezone.utc)
        payload = self.payload("what is my workout today", now=now,
                               phase=self.phase(202, "base"))
        self.assertNotIn("night_out_check", payload)
        self.assertNotIn("pro_reference", payload)
        self.assertEqual(payload["race"]["days_remaining"], 202)

    def test_the_morning_brief_carries_neither(self) -> None:
        now = datetime.datetime(2026, 11, 9, 9, 0, tzinfo=datetime.timezone.utc)
        payload = self.payload("", now=now, phase=self.phase(202, "base"))
        self.assertNotIn("night_out_check", payload)
        self.assertNotIn("pro_reference", payload)

    def test_the_event_mode_filters_the_reference(self) -> None:
        now = datetime.datetime(2026, 11, 9, 9, 0, tzinfo=datetime.timezone.utc)
        query = "how should I pace the bike leg and how long is the taper"
        triathlon = self.payload(query, now=now, phase=self.phase(202, "base"))
        self.assertIn("bike", triathlon["pro_reference"]["topics"])
        marathon = self.payload(query, now=now, phase=self.phase(202, "base", mode="MARATHON"))
        self.assertNotIn("bike", marathon["pro_reference"]["topics"])
        self.assertIn("taper", marathon["pro_reference"]["topics"])

    def test_a_failure_in_either_lookup_never_breaks_the_turn(self) -> None:
        now = datetime.datetime(2026, 11, 9, 9, 0, tzinfo=datetime.timezone.utc)
        with (
            patch.object(coach.triathlon_knowledge, "night_out_check",
                         side_effect=RuntimeError("boom")),
            patch.object(coach.triathlon_knowledge, "context_for",
                         side_effect=RuntimeError("boom")),
        ):
            payload = self.payload("can I drink friday", now=now,
                                   phase=self.phase(202, "base"))
        self.assertNotIn("night_out_check", payload)
        self.assertNotIn("pro_reference", payload)
        self.assertEqual(payload["today"], "2026-11-09")


if __name__ == "__main__":
    unittest.main()
