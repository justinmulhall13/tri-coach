from __future__ import annotations

import unittest

from app import coaching_contract as cc


def _profile(**kw) -> dict:
    base = {
        "event_name": "Some Marathon",
        "event_date": "2026-10-18",
        "distances": {"run_km": 42.2},
        "goal": {"target": "sub 3:30"},
        "mode": "MARATHON",
    }
    base.update(kw)
    return base


def _activate(profile: dict) -> dict:
    return cc.prepare_event_profile_for_activation(profile)


class NamespaceOwnershipTests(unittest.TestCase):
    """The persistence namespace decides which plans, drafts and chat rows an
    event can see, so it must never be chosen by the model."""

    def test_a_model_supplied_id_is_discarded(self) -> None:
        record = _activate(_profile(id="attacker-chosen-id"))
        self.assertNotEqual(record["id"], "attacker-chosen-id")

    def test_a_new_event_cannot_claim_the_reserved_t100_namespace(self) -> None:
        record = _activate(_profile(id="t100-vancouver-2026"))
        self.assertNotEqual(record["id"], "t100-vancouver-2026")

    def test_the_real_default_profile_keeps_its_stable_namespace(self) -> None:
        default = dict(cc.DEFAULT_EVENT_PROFILE)
        record = _activate(default)
        self.assertEqual(record["id"], cc.DEFAULT_EVENT_PROFILE["id"])

    def test_the_same_profile_always_resolves_to_the_same_namespace(self) -> None:
        # Otherwise a re-confirmation would orphan the plan it just built.
        self.assertEqual(_activate(_profile())["id"], _activate(_profile())["id"])


class MaterialDifferenceTests(unittest.TestCase):
    """Two events sharing a name and date are still different events if what
    they actually demand differs."""

    def setUp(self) -> None:
        self.baseline = _activate(_profile())["id"]

    def test_different_distances_do_not_share_state(self) -> None:
        other = _activate(_profile(distances={"run_km": 21.1}))["id"]
        self.assertNotEqual(other, self.baseline)

    def test_a_different_goal_does_not_share_state(self) -> None:
        other = _activate(_profile(goal={"target": "just finish"}))["id"]
        self.assertNotEqual(other, self.baseline)

    def test_a_different_mode_does_not_share_state(self) -> None:
        other = _activate(_profile(mode="TRIATHLON",
                                   distances={"swim_km": 2, "bike_km": 80, "run_km": 18}))["id"]
        self.assertNotEqual(other, self.baseline)

    def test_a_different_date_does_not_share_state(self) -> None:
        other = _activate(_profile(event_date="2026-11-01"))["id"]
        self.assertNotEqual(other, self.baseline)

    def test_a_different_event_name_does_not_share_state(self) -> None:
        other = _activate(_profile(event_name="Another Marathon"))["id"]
        self.assertNotEqual(other, self.baseline)


class NamespaceShapeTests(unittest.TestCase):
    def test_the_namespace_is_safe_to_use_as_a_key(self) -> None:
        record = _activate(_profile(
            event_name="  Marathon <script>alert(1)</script> / 2026  ",
        ))
        # A namespace is compared and stored, never rendered raw, but it must
        # still be a plain slug: no angle brackets, quotes, spaces or slashes
        # survive, so the markup is inert even though the word does not vanish.
        self.assertRegex(record["id"], r"^[a-z0-9-]+$")

    def test_a_wildly_long_event_name_does_not_produce_a_runaway_key(self) -> None:
        record = _activate(_profile(event_name="x" * 5000))
        self.assertLess(len(record["id"]), 200, record["id"])

    def test_unicode_event_names_still_produce_a_usable_namespace(self) -> None:
        record = _activate(_profile(event_name="Marathón de Sevilla 東京"))
        self.assertTrue(record["id"])
        self.assertRegex(record["id"], r"[a-z0-9-]")


class IncompleteProfileTests(unittest.TestCase):
    def test_an_incomplete_profile_is_never_activated(self) -> None:
        for missing in ("event_name", "event_date", "distances", "goal", "mode"):
            profile = _profile()
            profile[missing] = "" if missing != "distances" else {}
            with self.assertRaises(ValueError, msg=missing):
                _activate(profile)

    def test_a_nonsense_distance_is_refused(self) -> None:
        for bad in ({"run_km": 0}, {"run_km": -5}, {"run_km": "far"}, {"run_km": True}):
            with self.assertRaises(ValueError, msg=str(bad)):
                _activate(_profile(distances=bad))

    def test_an_unparseable_date_is_refused(self) -> None:
        for bad in ("October 18", "2026-13-45", "", "next Sunday"):
            with self.assertRaises(ValueError, msg=bad):
                _activate(_profile(event_date=bad))


if __name__ == "__main__":
    unittest.main()
