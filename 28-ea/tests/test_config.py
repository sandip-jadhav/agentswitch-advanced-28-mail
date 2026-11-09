"""config.py: system table, zones, harness-mode checks (P-B-11, P-B-21)."""
import unittest
from types import SimpleNamespace

from ea_briefing import config
from ea_briefing.config import ConfigError, resolve


def args(system=None, tz=None):
    return SimpleNamespace(system=system, tz=tz)


HARNESS = {"AGENTSWITCH_TOKEN": "tok", "AGENTSWITCH_INSTANCE": "keystone",
           "AGENTSWITCH_BASE_URL": "https://harness.example.test/"}


NO_MODEL_WARNING = "BRIEFING_LLM_MODEL not set — no drafts this run"
GLC_DEFAULT = "http://127.0.0.1:8111/v1"


class SystemTable(unittest.TestCase):
    def test_both_systems_with_default_zones_and_urls(self):
        s = config.SYSTEMS
        self.assertEqual(s["suryodaya"].base_url, "https://agentswitch.theschoolofai.in")
        self.assertEqual(s["keystone"].base_url, "https://class.agentswitch.theschoolofai.in")
        self.assertEqual(s["suryodaya"].default_zone, "Asia/Kolkata")
        self.assertEqual(s["keystone"].default_zone, "America/New_York")
        self.assertEqual((s["suryodaya"].label, s["keystone"].label), ("Suryodaya", "Keystone"))


class LocalMode(unittest.TestCase):
    def test_default_run_reads_both_systems_in_their_zones(self):
        st = resolve(args(), {})
        self.assertEqual(st.mode, "local")
        self.assertEqual([x.name for x in st.systems], ["suryodaya", "keystone"])
        self.assertEqual([x.zone.key for x in st.systems], ["Asia/Kolkata", "America/New_York"])
        self.assertEqual(st.notes, [])
        self.assertEqual(st.warnings, [NO_MODEL_WARNING])  # nothing else: the only warning is the unset model

    def test_system_option_limits_the_run(self):
        st = resolve(args(system="keystone"), {})
        self.assertEqual([x.name for x in st.systems], ["keystone"])

    def test_tz_override_changes_one_system_only(self):
        st = resolve(args(tz=["keystone=Europe/Amsterdam"]), {})
        zones = {x.name: x.zone.key for x in st.systems}
        self.assertEqual(zones, {"suryodaya": "Asia/Kolkata", "keystone": "Europe/Amsterdam"})

    def test_the_platform_zone_stays_the_default_when_the_display_zone_is_overridden(self):
        st = resolve(args(tz=["suryodaya=America/New_York", "keystone=Mars/Olympus"]), {})
        self.assertEqual([x.storage_zone.key for x in st.systems], ["Asia/Kolkata", "America/New_York"])

    def test_several_tz_options(self):
        st = resolve(args(tz=["suryodaya=America/New_York", "keystone=Asia/Kolkata"]), {})
        self.assertEqual([x.zone.key for x in st.systems], ["America/New_York", "Asia/Kolkata"])

    def test_unknown_zone_falls_back_with_the_prds_message(self):
        st = resolve(args(tz=["suryodaya=Mars/Olympus", "keystone=Nope"]), {})
        self.assertEqual([x.zone.key for x in st.systems], ["Asia/Kolkata", "America/New_York"])
        self.assertEqual(st.notes, ["Unknown time zone 'Mars/Olympus' — using IST for Suryodaya",
                                    "Unknown time zone 'Nope' — using US Eastern for Keystone"])

    def test_malformed_zone_values_fall_back_too(self):
        for bad in ("", "../etc/passwd", "  "):
            st = resolve(args(tz=["suryodaya=" + bad]), {})
            self.assertEqual(st.systems[0].zone.key, "Asia/Kolkata", bad)
            self.assertEqual(len(st.notes), 1, bad)

    def test_tz_without_equals_or_with_unknown_system_is_a_usage_error(self):
        for bad in (["keystone"], ["mars=UTC"], ["=UTC"]):
            with self.assertRaises(ConfigError, msg=bad):
                resolve(args(tz=bad), {})

    def test_unknown_system_is_a_usage_error(self):
        with self.assertRaises(ConfigError):
            resolve(args(system="atlantis"), {})

    def test_briefing_dir_from_env_else_default_folder(self):
        self.assertEqual(resolve(args(), {"BRIEFING_DIR": "/x/y"}).briefing_dir, "/x/y")
        self.assertTrue(resolve(args(), {}).briefing_dir.replace("\\", "/").endswith("28-ea/briefings"))

    def test_base_url_alone_is_ignored_with_a_warning(self):
        st = resolve(args(), {"AGENTSWITCH_BASE_URL": "https://harness.example.test"})
        self.assertEqual(st.mode, "local")
        self.assertEqual(st.systems[0].base_url, "https://agentswitch.theschoolofai.in")
        self.assertEqual(len(st.warnings), 2)  # the base-url one, and the unset model
        self.assertIn("AGENTSWITCH_BASE_URL", st.warnings[0])


class HarnessMode(unittest.TestCase):
    def test_valid_harness_run_covers_only_that_instance_at_that_url(self):
        st = resolve(args(), dict(HARNESS))
        self.assertEqual(st.mode, "harness")
        self.assertEqual([(x.name, x.base_url) for x in st.systems],
                         [("keystone", "https://harness.example.test")])
        self.assertEqual(st.systems[0].zone.key, "America/New_York")

    def test_matching_system_option_and_tz_are_fine(self):
        st = resolve(args(system="keystone", tz=["keystone=Asia/Kolkata"]), dict(HARNESS))
        self.assertEqual(st.systems[0].zone.key, "Asia/Kolkata")

    def test_suryodaya_instance_uses_its_zone(self):
        st = resolve(args(), dict(HARNESS, AGENTSWITCH_INSTANCE="suryodaya"))
        self.assertEqual(st.systems[0].name, "suryodaya")
        self.assertEqual(st.systems[0].zone.key, "Asia/Kolkata")

    def test_any_one_of_the_three_set_without_the_others_exits_two_style_error(self):
        for drop in ("AGENTSWITCH_TOKEN", "AGENTSWITCH_INSTANCE", "AGENTSWITCH_BASE_URL"):
            env = {k: v for k, v in HARNESS.items() if k != drop}
            with self.assertRaises(ConfigError, msg=drop) as cm:
                resolve(args(), env)
            self.assertIn(drop, str(cm.exception))
            self.assertNotIn("\n", str(cm.exception))

    def test_token_alone_or_instance_alone_starts_harness_mode_and_fails(self):
        for env in ({"AGENTSWITCH_TOKEN": "tok"}, {"AGENTSWITCH_INSTANCE": "keystone"}):
            with self.assertRaises(ConfigError):
                resolve(args(), env)

    def test_empty_values_count_as_unset(self):
        env = dict(HARNESS, AGENTSWITCH_BASE_URL="")
        with self.assertRaises(ConfigError):
            resolve(args(), env)
        st = resolve(args(), {"AGENTSWITCH_TOKEN": "", "AGENTSWITCH_INSTANCE": ""})
        self.assertEqual(st.mode, "local")

    def test_a_base_url_without_http_or_https_is_an_error(self):
        for url in ("harness.example.test", "ftp://harness.example.test", "//harness.example.test", "localhost:8000"):
            with self.assertRaises(ConfigError, msg=url) as cm:
                resolve(args(), dict(HARNESS, AGENTSWITCH_BASE_URL=url))
            self.assertIn("AGENTSWITCH_BASE_URL", str(cm.exception))
            self.assertNotIn("\n", str(cm.exception))
        for url in ("http://localhost:8000", "HTTPS://harness.example.test"):
            self.assertEqual(resolve(args(), dict(HARNESS, AGENTSWITCH_BASE_URL=url)).mode, "harness")

    def test_bad_instance_is_an_error(self):
        with self.assertRaises(ConfigError) as cm:
            resolve(args(), dict(HARNESS, AGENTSWITCH_INSTANCE="atlantis"))
        self.assertIn("atlantis", str(cm.exception))

    def test_system_option_naming_another_system_is_an_error(self):
        with self.assertRaises(ConfigError):
            resolve(args(system="suryodaya"), dict(HARNESS))

    def test_the_tokens_never_appear_in_error_text(self):
        with self.assertRaises(ConfigError) as cm:
            resolve(args(), {"AGENTSWITCH_TOKEN": "super-secret-token"})
        self.assertNotIn("super-secret-token", str(cm.exception))


class ModelSettingsFromBriefingVariables(unittest.TestCase):
    """P-B-36 without OPENAI_BASE_URL: BRIEFING_LLM_URL (default glc) and BRIEFING_LLM_MODEL."""

    def test_defaults_to_glc_on_localhost_with_no_model_and_says_no_drafts(self):
        st = resolve(args(), {})
        self.assertEqual((st.model.url, st.model.model, st.model.api_key), (GLC_DEFAULT, None, None))
        self.assertEqual(st.warnings, [NO_MODEL_WARNING])

    def test_url_and_model_from_the_briefing_variables_without_a_warning(self):
        st = resolve(args(), {"BRIEFING_LLM_URL": "http://glc.test:9/v1/", "BRIEFING_LLM_MODEL": "m1"})
        self.assertEqual((st.model.url, st.model.model, st.model.api_key), ("http://glc.test:9/v1", "m1", None))
        self.assertEqual(st.warnings, [])

    def test_a_blank_model_counts_as_unset(self):
        st = resolve(args(), {"BRIEFING_LLM_MODEL": "   "})
        self.assertIsNone(st.model.model)
        self.assertEqual(st.warnings, [NO_MODEL_WARNING])

    def test_other_openai_variables_are_ignored_without_openai_base_url(self):
        st = resolve(args(), {"OPENAI_MODEL": "gpt-x", "OPENAI_API_KEY": "sk-secret", "BRIEFING_LLM_MODEL": "m1"})
        self.assertEqual((st.model.url, st.model.model, st.model.api_key), (GLC_DEFAULT, "m1", None))
        self.assertEqual(st.warnings, [])

    def test_other_openai_variables_do_not_fill_in_a_missing_briefing_model(self):
        st = resolve(args(), {"OPENAI_MODEL": "gpt-x"})
        self.assertIsNone(st.model.model)
        self.assertEqual(st.warnings, [NO_MODEL_WARNING])

    def test_a_briefing_url_that_is_not_http_is_a_usage_error(self):
        with self.assertRaises(ConfigError) as cm:
            resolve(args(), {"BRIEFING_LLM_URL": "glc.test/v1", "BRIEFING_LLM_MODEL": "m1"})
        self.assertIn("BRIEFING_LLM_URL", str(cm.exception))


class ModelSettingsFromOpenaiVariables(unittest.TestCase):
    """P-B-36 with OPENAI_BASE_URL: the harness's endpoint, model (required) and key (optional)."""

    def test_base_url_model_and_key(self):
        st = resolve(args(), {"OPENAI_BASE_URL": "https://models.test/v1/", "OPENAI_MODEL": "gpt-x",
                              "OPENAI_API_KEY": "sk-secret"})
        self.assertEqual((st.model.url, st.model.model, st.model.api_key),
                         ("https://models.test/v1", "gpt-x", "sk-secret"))
        self.assertEqual(st.warnings, [])

    def test_the_key_is_optional(self):
        st = resolve(args(), {"OPENAI_BASE_URL": "https://models.test/v1", "OPENAI_MODEL": "gpt-x"})
        self.assertIsNone(st.model.api_key)

    def test_a_missing_or_blank_model_is_a_usage_error_in_one_line(self):
        for env in ({"OPENAI_BASE_URL": "https://models.test/v1"},
                    {"OPENAI_BASE_URL": "https://models.test/v1", "OPENAI_MODEL": "  "}):
            with self.assertRaises(ConfigError) as cm:
                resolve(args(), env)
            self.assertIn("OPENAI_MODEL", str(cm.exception))
            self.assertNotIn("\n", str(cm.exception))

    def test_a_base_url_that_is_not_http_is_a_usage_error(self):
        with self.assertRaises(ConfigError) as cm:
            resolve(args(), {"OPENAI_BASE_URL": "models.test/v1", "OPENAI_MODEL": "gpt-x"})
        self.assertIn("OPENAI_BASE_URL", str(cm.exception))

    def test_they_win_over_the_briefing_variables_and_the_unset_model_warning_goes(self):
        st = resolve(args(), {"OPENAI_BASE_URL": "https://models.test/v1", "OPENAI_MODEL": "gpt-x",
                              "BRIEFING_LLM_URL": "http://glc.test/v1", "BRIEFING_LLM_MODEL": "m1"})
        self.assertEqual((st.model.url, st.model.model), ("https://models.test/v1", "gpt-x"))
        self.assertEqual(st.warnings, [])

    def test_the_key_stays_out_of_the_settings_repr(self):
        st = resolve(args(), {"OPENAI_BASE_URL": "https://models.test/v1", "OPENAI_MODEL": "gpt-x",
                              "OPENAI_API_KEY": "sk-secret"})
        self.assertNotIn("sk-secret", repr(st.model))
        self.assertNotIn("sk-secret", repr(st))

    def test_the_key_never_appears_in_an_error_line(self):
        with self.assertRaises(ConfigError) as cm:
            resolve(args(), {"OPENAI_BASE_URL": "https://models.test/v1", "OPENAI_API_KEY": "sk-secret"})
        self.assertNotIn("sk-secret", str(cm.exception))


class ModelSettingsInBothModes(unittest.TestCase):
    def test_harness_mode_reads_the_same_variables(self):
        st = resolve(args(), dict(HARNESS, OPENAI_BASE_URL="https://models.test/v1", OPENAI_MODEL="gpt-x",
                                  OPENAI_API_KEY="sk-secret"))
        self.assertEqual(st.mode, "harness")
        self.assertEqual((st.model.url, st.model.model, st.model.api_key),
                         ("https://models.test/v1", "gpt-x", "sk-secret"))

    def test_harness_mode_without_openai_falls_back_to_the_briefing_variables(self):
        st = resolve(args(), dict(HARNESS, BRIEFING_LLM_MODEL="m1"))
        self.assertEqual((st.model.url, st.model.model), (GLC_DEFAULT, "m1"))
        st = resolve(args(), dict(HARNESS))
        self.assertEqual(st.warnings, [NO_MODEL_WARNING])

    def test_harness_mode_missing_openai_model_is_a_usage_error(self):
        with self.assertRaises(ConfigError) as cm:
            resolve(args(), dict(HARNESS, OPENAI_BASE_URL="https://models.test/v1"))
        self.assertIn("OPENAI_MODEL", str(cm.exception))


if __name__ == "__main__":
    unittest.main()
