import json
import os
import sys
import tempfile
import unittest
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "dev" / "src" / "common"))

import story_types as st

MIX = {
    "story_type_mix": {
        "principle_experience": 0.35,
        "myth_bust": 0.30,
        "habit_mechanism": 0.25,
        "case_journey": 0.10,
    },
    "lookback_jobs": 20,
    "enforce_on_auto": True,
    "enforce_on_manual": False,
    "default_story_type": "principle_experience",
    "priority_on_tie": list(st.STORY_TYPES),
}


def config(**overrides):
    return st._coerce_config({**MIX, **overrides})


class NormalizeTests(unittest.TestCase):
    def test_accepts_id_label_and_loose_casing(self):
        self.assertEqual(st.normalize("myth_bust"), "myth_bust")
        self.assertEqual(st.normalize("오해 교정형"), "myth_bust")
        self.assertEqual(st.normalize("Habit-Mechanism"), "habit_mechanism")

    def test_rejects_unknown_and_empty(self):
        self.assertIsNone(st.normalize("설명형"))
        self.assertIsNone(st.normalize(""))
        self.assertIsNone(st.normalize(None))


class MappingTests(unittest.TestCase):
    def test_every_existing_format_type_maps_to_a_story_type(self):
        formats = ("오해반전형", "자가진단형", "사례추적형", "비교형", "연구발견형", "행동챌린지형")
        for format_type in formats:
            self.assertIn(st.story_type_for_format(format_type), st.STORY_TYPES)

    def test_every_story_type_has_a_canonical_format(self):
        for story_type in st.STORY_TYPES:
            self.assertEqual(st.story_type_for_format(st.format_for_story_type(story_type)), story_type)

    def test_unknown_format_maps_to_nothing(self):
        self.assertIsNone(st.story_type_for_format("존재하지않는형"))


class ReconcileTests(unittest.TestCase):
    def test_consistent_pair_passes_through_without_warning(self):
        story_type, format_type, warning = st.reconcile("myth_bust", "비교형", config())
        self.assertEqual((story_type, format_type), ("myth_bust", "비교형"))
        self.assertIsNone(warning)

    def test_story_type_wins_and_rewrites_a_conflicting_format(self):
        story_type, format_type, warning = st.reconcile("habit_mechanism", "사례추적형", config())
        self.assertEqual(story_type, "habit_mechanism")
        self.assertEqual(format_type, "행동챌린지형")
        self.assertIn("사례추적형", warning)

    def test_missing_story_type_is_derived_from_the_format(self):
        story_type, format_type, warning = st.reconcile(None, "연구발견형", config())
        self.assertEqual((story_type, format_type), ("principle_experience", "연구발견형"))
        self.assertIsNone(warning)

    def test_missing_both_falls_back_to_the_configured_default(self):
        story_type, format_type, warning = st.reconcile(None, None, config())
        self.assertEqual(story_type, "principle_experience")
        self.assertEqual(format_type, "자가진단형")
        self.assertIsNone(warning)

    def test_missing_format_is_filled_from_the_story_type(self):
        story_type, format_type, _ = st.reconcile("case_journey", "", config())
        self.assertEqual((story_type, format_type), ("case_journey", "사례추적형"))


class LoadConfigTests(unittest.TestCase):
    def test_repo_config_file_loads(self):
        loaded = st.load_config(st.DEFAULT_MIX_PATH)
        self.assertAlmostEqual(sum(loaded["story_type_mix"].values()), 1.0, places=6)
        self.assertEqual(set(loaded["story_type_mix"]), set(st.STORY_TYPES))

    def test_missing_file_degrades_to_defaults(self):
        loaded = st.load_config("/no/such/story_type_mix.json")
        self.assertEqual(loaded["story_type_mix"], st.DEFAULT_CONFIG["story_type_mix"])

    def test_malformed_file_degrades_to_defaults(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "mix.json"
            path.write_text("{not json", encoding="utf-8")
            self.assertEqual(st.load_config(path)["story_type_mix"], st.DEFAULT_CONFIG["story_type_mix"])

    def test_partial_mix_is_renormalized_to_one(self):
        loaded = st._coerce_config({"story_type_mix": {"myth_bust": 2, "case_journey": 2}})
        self.assertEqual(loaded["story_type_mix"], {"myth_bust": 0.5, "case_journey": 0.5})

    def test_priority_gains_any_id_the_operator_left_out(self):
        loaded = st._coerce_config({"priority_on_tie": ["case_journey"]})
        self.assertEqual(loaded["priority_on_tie"][0], "case_journey")
        self.assertEqual(set(loaded["priority_on_tie"]), set(st.STORY_TYPES))

    def test_edited_file_is_picked_up_without_a_restart(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "mix.json"
            path.write_text(json.dumps({"story_type_mix": {"myth_bust": 1.0}}), encoding="utf-8")
            self.assertEqual(set(st.load_config(path)["story_type_mix"]), {"myth_bust"})
            path.write_text(json.dumps({"story_type_mix": {"case_journey": 1.0}}), encoding="utf-8")
            os.utime(path, (0, 0))  # force a distinct mtime
            self.assertEqual(set(st.load_config(path)["story_type_mix"]), {"case_journey"})


class PickStoryTypeTests(unittest.TestCase):
    def test_cold_start_picks_the_highest_weighted_type(self):
        self.assertEqual(st.pick_story_type([], config()), "principle_experience")

    def test_over_produced_type_is_skipped(self):
        recent = ["principle_experience"] * 5
        self.assertNotEqual(st.pick_story_type(recent, config()), "principle_experience")

    def test_suggested_narrows_the_pool(self):
        picked = st.pick_story_type([], config(), suggested=["case_journey", "habit_mechanism"])
        self.assertEqual(picked, "habit_mechanism")

    def test_unknown_suggestions_fall_back_to_the_global_pick(self):
        self.assertEqual(st.pick_story_type([], config(), suggested=["설명형", ""]), "principle_experience")

    def test_suggested_cannot_flood_an_over_quota_type(self):
        # case_journey has already had 3 of a 10-job window; its quota is 1.
        recent = ["case_journey"] * 3 + ["myth_bust"] * 7
        self.assertNotEqual(st.pick_story_type(recent, config(), suggested=["case_journey"]), "case_journey")

    def test_disabled_type_is_never_picked(self):
        narrow = st._coerce_config({"story_type_mix": {"myth_bust": 0.5, "habit_mechanism": 0.5}})
        picks = {st.pick_story_type(["myth_bust"] * n, narrow) for n in range(6)}
        self.assertEqual(picks, {"myth_bust", "habit_mechanism"})

    def test_cold_start_with_an_rng_samples_by_weight(self):
        import random
        picks = Counter(st.pick_story_type([], config(), rng=random.Random(seed)) for seed in range(400))
        self.assertGreater(picks["principle_experience"], picks["case_journey"])
        self.assertEqual(set(picks) - set(st.STORY_TYPES), set())

    def test_twenty_sequential_picks_track_the_configured_mix(self):
        """Acceptance §10-1: auto planning converges on the mix within ±15%p."""
        history = []
        for _ in range(20):
            history.append(st.pick_story_type(history, config()))
        observed = st.distribution(history)
        for story_type, target in MIX["story_type_mix"].items():
            self.assertLessEqual(
                abs(observed[story_type] - target), 0.15,
                f"{story_type}: 목표 {target:.0%} 대비 실제 {observed[story_type]:.0%}",
            )

    def test_case_journey_never_exceeds_its_share(self):
        """Acceptance §10-8: the rare genre stays rare."""
        history = []
        for _ in range(40):
            history.append(st.pick_story_type(history, config()))
        self.assertLessEqual(st.distribution(history)["case_journey"], 0.10 + 0.05)

    def test_case_journey_never_repeats_back_to_back(self):
        history = []
        for _ in range(40):
            history.append(st.pick_story_type(history, config()))
        pairs = list(zip(history, history[1:]))
        self.assertNotIn(("case_journey", "case_journey"), pairs)


class RecentStoryTypesTests(unittest.TestCase):
    def _job(self, root, name, strategy):
        job_dir = Path(root) / name
        job_dir.mkdir(parents=True)
        (job_dir / "strategy.json").write_text(json.dumps(strategy, ensure_ascii=False), encoding="utf-8")
        return job_dir

    def test_reads_explicit_story_type_newest_first(self):
        with tempfile.TemporaryDirectory() as tmp:
            first = self._job(tmp, "job_a", {"story_type": "myth_bust"})
            second = self._job(tmp, "job_b", {"story_type": "case_journey"})
            os.utime(first / "strategy.json", (1, 1))
            os.utime(second / "strategy.json", (2, 2))
            self.assertEqual(st.recent_story_types(tmp), ["case_journey", "myth_bust"])

    def test_backfills_pre_story_type_jobs_from_format_type(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._job(tmp, "job_a", {"content_design": {"format_type": "행동챌린지형"}})
            self.assertEqual(st.recent_story_types(tmp), ["habit_mechanism"])

    def test_skips_jobs_with_no_genre_at_all(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._job(tmp, "job_a", {"title": "제목만 있는 잡"})
            self.assertEqual(st.recent_story_types(tmp), [])

    def test_unreadable_strategy_is_skipped_not_raised(self):
        with tempfile.TemporaryDirectory() as tmp:
            job_dir = Path(tmp) / "job_a"
            job_dir.mkdir()
            (job_dir / "strategy.json").write_text("{broken", encoding="utf-8")
            self._job(tmp, "job_b", {"story_type": "myth_bust"})
            self.assertEqual(st.recent_story_types(tmp), ["myth_bust"])

    def test_honours_the_lookback_limit(self):
        with tempfile.TemporaryDirectory() as tmp:
            for index in range(5):
                self._job(tmp, f"job_{index}", {"story_type": "myth_bust"})
            self.assertEqual(len(st.recent_story_types(tmp, limit=3)), 3)

    def test_db_format_rows_extend_the_history(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._job(tmp, "job_a", {"story_type": "myth_bust"})
            recent = st.recent_story_types(tmp, format_rows=["사례추적형", "행동챌린지형"])
            self.assertEqual(recent, ["myth_bust", "case_journey", "habit_mechanism"])

    def test_missing_work_root_returns_empty(self):
        self.assertEqual(st.recent_story_types("/no/such/work/root"), [])


class TemplateTests(unittest.TestCase):
    def test_every_story_type_ships_a_template(self):
        for story_type in st.STORY_TYPES:
            self.assertTrue(st.load_template(story_type), story_type)

    def test_common_template_ships(self):
        self.assertTrue(st.load_common_template())

    def test_template_states_its_own_role_sequence(self):
        for story_type in st.STORY_TYPES:
            template = st.load_template(story_type)
            for role in st.role_sequence(story_type):
                self.assertIn(role, template, f"{story_type} 템플릿에 {role} 누락")

    def test_unknown_story_type_yields_no_template(self):
        self.assertEqual(st.load_template("설명형"), "")

    def test_missing_directory_yields_no_template(self):
        self.assertEqual(st.load_template("myth_bust", directory="/no/such/dir"), "")


class RoleSequenceTests(unittest.TestCase):
    def test_every_sequence_starts_at_hook_and_ends_at_cta(self):
        for story_type in st.STORY_TYPES:
            sequence = st.role_sequence(story_type)
            self.assertEqual(sequence[0], "hook")
            self.assertEqual(sequence[-1], "cta")

    def test_every_role_has_a_default_visual_type(self):
        for story_type in st.STORY_TYPES:
            for role in st.role_sequence(story_type):
                self.assertIn(st.default_visual_type(role), st.VISUAL_TYPES)

    def test_unknown_story_type_falls_back_to_the_default_sequence(self):
        self.assertEqual(st.role_sequence("설명형"), st.ROLE_SEQUENCES["principle_experience"])


class SuggestFromTextTests(unittest.TestCase):
    def test_myth_wording_suggests_myth_bust(self):
        self.assertEqual(st.suggest_from_text("치매에 대한 흔한 오해, 사실은 다릅니다")[0], "myth_bust")

    def test_habit_wording_suggests_habit_mechanism(self):
        self.assertEqual(st.suggest_from_text("매일 아침 습관 하나")[0], "habit_mechanism")

    def test_no_hint_returns_empty(self):
        self.assertEqual(st.suggest_from_text("혈압과 뇌"), [])
        self.assertEqual(st.suggest_from_text(""), [])

    def test_suggestions_are_always_valid_ids(self):
        for suggestion in st.suggest_from_text("왜 매일 오해가 생기는 사례일까"):
            self.assertIn(suggestion, st.STORY_TYPES)


if __name__ == "__main__":
    unittest.main()
