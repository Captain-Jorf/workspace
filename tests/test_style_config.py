"""Issue #33: explicit style config — minimal is the default, rich is
experimental/manual-only and can NEVER be cron- or LLM-selected.

These tests pin the selection contract:
  * schedule (cron) -> minimal, unconditionally, even with both rich flags;
  * rich requires BOTH explicit flags (REEL_STYLE=experimental-rich AND
    ALLOW_EXPERIMENTAL_STYLE=1); one flag alone -> minimal;
  * no env at all -> minimal (default for manual daily dry-runs and
    production rendering);
  * unknown/typos raise (fail closed) — never silently pick a renderer;
  * LLM free text is NOT an input: the module exposes no function that
    takes script/caption/prompt text as a style source, and normalize_style
    rejects anything that is not exactly a known style name.
"""
import os
import unittest

sys_path_hack = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
import sys  # noqa: E402
sys.path.insert(0, os.path.join(sys_path_hack, "build"))
import style_config as sc  # noqa: E402


def env(**kw):
    return dict(kw)


class ResolutionContract(unittest.TestCase):
    def test_cron_is_always_minimal(self):
        # both rich flags set — a schedule run must STILL be minimal
        e = env(GITHUB_EVENT_NAME="schedule",
                REEL_STYLE="experimental-rich",
                ALLOW_EXPERIMENTAL_STYLE="1")
        self.assertEqual(sc.resolve_style(e), sc.MINIMAL)
        self.assertEqual(sc.resolve_style(e, event_name="schedule"), sc.MINIMAL)
        # cron with no flags
        self.assertEqual(sc.resolve_style(env(GITHUB_EVENT_NAME="schedule")),
                         sc.MINIMAL)
        # explicit event arg wins over a clean env
        self.assertEqual(sc.resolve_style(env(), event_name="schedule"),
                         sc.MINIMAL)

    def test_rich_requires_both_explicit_flags(self):
        e = env(REEL_STYLE="experimental-rich", ALLOW_EXPERIMENTAL_STYLE="1")
        self.assertEqual(sc.resolve_style(e), sc.EXPERIMENTAL_RICH)
        # a rich REQUEST without the gate is a misconfiguration: fail
        # closed (nothing half-configured may silently switch renderers)
        with self.assertRaises(ValueError):
            sc.resolve_style(env(REEL_STYLE="experimental-rich"))
        # the gate ALONE is harmless: nothing requested -> minimal
        self.assertEqual(
            sc.resolve_style(env(ALLOW_EXPERIMENTAL_STYLE="1")), sc.MINIMAL)

    def test_default_is_minimal(self):
        self.assertEqual(sc.resolve_style(env()), sc.MINIMAL)
        self.assertEqual(sc.resolve_style({}), sc.MINIMAL)

    def test_manual_dispatch_is_minimal_by_default(self):
        # a normal manual daily dry-run (workflow_dispatch, no flags)
        e = env(GITHUB_EVENT_NAME="workflow_dispatch")
        self.assertEqual(sc.resolve_style(e), sc.MINIMAL)

    def test_unknown_styles_fail_closed(self):
        for bad in ("rich", "minimal-rich", "experimental", "minimal rich",
                    "richer", "auto", "photo", "RICH"):
            with self.assertRaises(ValueError):
                sc.normalize_style(bad)

    def test_rich_normalization_requires_gate(self):
        self.assertEqual(sc.normalize_style(None), sc.MINIMAL)
        self.assertEqual(sc.normalize_style("minimal"), sc.MINIMAL)
        self.assertEqual(sc.normalize_style(" MINIMAL "), sc.MINIMAL)
        with self.assertRaises(ValueError):
            sc.normalize_style("experimental-rich")  # gate closed
        self.assertEqual(
            sc.normalize_style("experimental-rich", allow_rich=True),
            sc.EXPERIMENTAL_RICH)

    def test_flag_typo_falls_to_minimal_not_rich(self):
        # REEL_STYLE typo must never activate the rich renderer
        for typo in ("Rich", "experimental rich", "minimal_rich"):
            with self.assertRaises(ValueError):
                sc.resolve_style(env(REEL_STYLE=typo,
                                     ALLOW_EXPERIMENTAL_STYLE="1"))
        # a rich request with a gate value other than "1" is still an
        # ungated rich request -> fail closed
        with self.assertRaises(ValueError):
            sc.resolve_style(env(REEL_STYLE="experimental-rich",
                                 ALLOW_EXPERIMENTAL_STYLE="true"))

    def test_llm_text_is_never_a_style_input(self):
        # the public API takes only env mappings / event names — no
        # function may accept script/caption/prompt text
        for fn in (sc.resolve_style, sc.normalize_style, sc.style_for_pipeline):
            import inspect
            params = list(inspect.signature(fn).parameters)
            for p in params:
                self.assertNotIn(p, ("script", "text", "prompt", "caption"),
                                 f"{fn.__name__}({p}) — LLM text is never a "
                                 "style input")
        # and a script-like string value is just an unknown style
        with self.assertRaises(ValueError):
            sc.normalize_style("please make this one fancy with photos")

    def test_style_for_pipeline_source_labels(self):
        s, why = sc.style_for_pipeline(env())
        self.assertEqual(s, sc.MINIMAL)
        self.assertTrue(why.startswith("default"), why)
        s, why = sc.style_for_pipeline(env(GITHUB_EVENT_NAME="schedule"))
        self.assertEqual(s, sc.MINIMAL)
        self.assertIn("cron", why)
        s, why = sc.style_for_pipeline(env(REEL_STYLE="experimental-rich",
                                           ALLOW_EXPERIMENTAL_STYLE="1"))
        self.assertEqual(s, sc.EXPERIMENTAL_RICH)
        self.assertIn("manual/test", why)


if __name__ == "__main__":
    unittest.main(verbosity=2)
