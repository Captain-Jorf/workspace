"""Explicit Reel style selection (issue #33 — minimal is the default format).

Product decision: the MINIMAL style is the default scheduled production
format for @metacognition.hq. The complex photo/procedural style is kept
ONLY as an experimental/manual mode (`experimental-rich`) — it is:

  * disabled by default;
  * NEVER cron-selected (schedule events force minimal unconditionally);
  * un-activatable by LLM free text — no script field, caption word or
    plan string can switch the style. The ONLY inputs are the explicit
    manual/test environment flags below;
  * activatable only by the explicit manual/test flag pair.

Selection inputs (and nothing else):
  GITHUB_EVENT_NAME  — set by GitHub Actions; "schedule" = the daily cron.
  REEL_STYLE         — explicit request: "minimal" or "experimental-rich".
  ALLOW_EXPERIMENTAL_STYLE — the second explicit flag required to run the
                 experimental rich renderer (safety gate; tests set both).

Resolution rules (deterministic, fail-closed):
  1. schedule (cron)            -> "minimal"  (always; flags are ignored)
  2. REEL_STYLE=experimental-rich AND ALLOW_EXPERIMENTAL_STYLE=1
                                 -> "experimental-rich"
  3. everything else            -> "minimal"

`normalize_style` rejects unknown values so a typo can never silently
switch renderers.
"""
import os

MINIMAL = "minimal"
EXPERIMENTAL_RICH = "experimental-rich"
KNOWN_STYLES = (MINIMAL, EXPERIMENTAL_RICH)

ENV_EVENT = "GITHUB_EVENT_NAME"
ENV_REQUEST = "REEL_STYLE"
ENV_ALLOW_RICH = "ALLOW_EXPERIMENTAL_STYLE"
CRON_EVENT = "schedule"


def normalize_style(value, *, allow_rich=False):
    """Validate/normalize an explicit style value.

    Returns the canonical name. Raises ValueError for unknown values.
    `experimental-rich` additionally requires `allow_rich=True` — the
    normalization itself enforces the safety gate even when a caller
    bypasses resolve_style.
    """
    if value is None:
        return MINIMAL
    v = str(value).strip().lower()
    if v not in KNOWN_STYLES:
        raise ValueError(
            f"unknown reel style {value!r} — known: {', '.join(KNOWN_STYLES)} "
            "(LLM output is never a style input)")
    if v == EXPERIMENTAL_RICH and not allow_rich:
        raise ValueError(
            "experimental-rich is disabled by default — it requires the "
            "explicit manual/test flag pair REEL_STYLE=experimental-rich "
            "AND ALLOW_EXPERIMENTAL_STYLE=1")
    return v


def resolve_style(environ=None, event_name=None):
    """Resolve the effective render style from the explicit flags ONLY.

    Parameters
    ----------
    environ : mapping, optional
        Environment to read (defaults to os.environ; tests inject both).
    event_name : str, optional
        The triggering event name. Defaults to environ[GITHUB_EVENT_NAME].
        Tests inject this explicitly so resolution is deterministic.
    """
    env = os.environ if environ is None else environ
    ev = event_name if event_name is not None else env.get(ENV_EVENT, "")
    # 1. cron is never rich-selected — the scheduled format is minimal,
    #    unconditionally; manual flags cannot override a schedule run.
    if str(ev).strip().lower() == CRON_EVENT:
        return MINIMAL
    # 2. explicit manual/test flag pair (both are required)
    requested = (env.get(ENV_REQUEST) or "").strip().lower()
    if requested:
        allow = (env.get(ENV_ALLOW_RICH) or "").strip() == "1"
        return normalize_style(requested, allow_rich=allow)
    # 3. default: minimal
    return MINIMAL


def is_cron(event_name=None, environ=None):
    env = os.environ if environ is None else environ
    ev = event_name if event_name is not None else env.get(ENV_EVENT, "")
    return str(ev).strip().lower() == CRON_EVENT


def style_for_pipeline(environ=None, event_name=None):
    """Convenience for the pipeline: (style, source) for logging/reports.

    Fail-closed: an invalid or unterminated explicit request (a typo, or a
    rich request missing the ALLOW_EXPERIMENTAL_STYLE=1 gate) raises
    ValueError so a half-configured flag pair can never silently run the
    wrong renderer.
    """
    env = os.environ if environ is None else environ
    if is_cron(environ=env, event_name=event_name):
        return MINIMAL, "cron schedule (forced minimal — never cron-rich)"
    requested = (env.get(ENV_REQUEST) or "").strip().lower()
    if requested:
        # resolve_style validates the request (raises on typo / ungated rich)
        style = resolve_style(environ=env, event_name=event_name)
        if style == EXPERIMENTAL_RICH:
            return (EXPERIMENTAL_RICH,
                    "explicit manual/test flags REEL_STYLE + ALLOW_EXPERIMENTAL_STYLE")
        return MINIMAL, "explicit flags"
    return MINIMAL, "default (minimal is the default production format)"


if __name__ == "__main__":
    # manual probe: python3 build/style_config.py
    s, why = style_for_pipeline()
    print(f"style={s} ({why})")
