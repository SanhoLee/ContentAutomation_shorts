import os
from dataclasses import dataclass

FALSE_VALUES = {"0", "false", "off", "no", "n"}

SPEECH_PACE_PROFILES = {
    "slow": {"atempo": 0.95, "script_density": 4.0},
    "normal": {"atempo": 1.10, "script_density": 4.66},
    "fast": {"atempo": 1.20, "script_density": 5.10},
    "very_fast": {"atempo": 1.30, "script_density": 5.50},
}


def env_bool(name, default):
    value = os.environ.get(name)
    if value in (None, ""):
        return default
    return value.strip().lower() not in FALSE_VALUES


def env_csv(name, default=()):
    value = os.environ.get(name)
    if value in (None, ""):
        return tuple(default)
    return tuple(part.strip() for part in value.split(",") if part.strip())


def env_int(name, default):
    return int(os.environ.get(name, str(default)))


def env_float(name, default):
    return float(os.environ.get(name, str(default)))


def speech_pace_profile(value):
    pace = str(value or "normal").strip().lower().replace("-", "_")
    if pace not in SPEECH_PACE_PROFILES:
        allowed = ", ".join(SPEECH_PACE_PROFILES)
        raise ValueError(f"SPEECH_PACE must be one of: {allowed}")
    return pace, SPEECH_PACE_PROFILES[pace]


def youtube_feedback_strictness(value):
    aliases = {
        "loose": "loose", "relaxed": "loose", "느슨": "loose", "느슨함": "loose",
        "balanced": "balanced", "medium": "balanced", "중간": "balanced", "보통": "balanced",
        "strict": "strict", "엄격": "strict", "엄격함": "strict",
    }
    normalized = aliases.get(str(value or "balanced").strip().lower())
    if normalized is None:
        raise ValueError("YOUTUBE_FEEDBACK_STRICTNESS must be one of: loose, balanced, strict")
    return normalized


def resolve_speech_settings():
    configured_pace = os.environ.get("SPEECH_PACE")
    if configured_pace not in (None, ""):
        pace, profile = speech_pace_profile(configured_pace)
        return pace, profile["atempo"], profile["script_density"]

    # Backward compatibility for existing deployments until they set SPEECH_PACE.
    atempo = env_float("ATEMPO", "1.0")
    chars_per_sec = env_float("CHARS_PER_SEC", "4.66")
    return "legacy", atempo, atempo * chars_per_sec


def script_length_targets(target_duration_sec, script_density):
    total = int(target_duration_sec * script_density)
    prompt_target = int(total * 1.15)
    min_scenes = max(8, min(10, prompt_target // 55))
    return total, prompt_target, min_scenes


@dataclass(frozen=True)
class ScriptRuntimeSettings:
    work_dir: str
    speech_pace: str
    atempo: float
    target_duration_sec: int
    script_density: float
    trend_candidate_count: int
    request_timeout: int
    claude_timeout: int
    claude_http_retries: int
    pubmed_query_timeout: int
    pubmed_retmax: int
    pubmed_abstract_char_limit: int
    ncbi_api_key: str
    claude_model: str
    claude_script_model: str
    claude_query_model: str
    claude_research_model: str
    claude_strategy_model: str
    claude_strategy_fallback_models: tuple[str, ...]
    claude_strategy_max_tokens: int
    claude_planner_model: str
    claude_critic_model: str
    claude_audit_model: str
    claude_interpreter_model: str
    claude_planner_max_tokens: int
    claude_critic_max_tokens: int
    claude_audit_max_tokens: int
    claude_interpreter_max_tokens: int
    claude_selection_percentile: float
    claude_confidence_percentile: float
    claude_selection_band: float
    claude_job_budget_usd: float
    claude_daily_budget_usd: float
    claude_max_web_searches_per_job: int
    claude_cost_mode: str
    max_tokens: int
    enable_web_research: bool
    web_research_timeout: int
    web_research_max_uses: int
    web_research_max_tokens: int
    web_research_max_tool_turns: int
    enable_case_research: bool
    case_research_timeout: int
    case_research_max_uses: int
    case_research_max_tokens: int
    case_research_max_tool_turns: int
    research_mode: str
    strategy_path: str
    youtube_feedback_strictness: str
    youtube_feedback_auto_sync: bool
    youtube_feedback_sync_ttl_hours: int
    total_chars: int
    prompt_target_chars: int
    min_scenes_estimate: int


def load_runtime_settings():
    work_dir = os.environ.get("WORK_DIR", os.path.expanduser("~/brain50/data/work"))
    speech_pace, atempo, script_density = resolve_speech_settings()
    target_duration_sec = env_int("TARGET_DURATION_SEC", 60)
    total_chars, prompt_target_chars, min_scenes_estimate = script_length_targets(
        target_duration_sec,
        script_density,
    )
    claude_model = os.environ.get("CLAUDE_MODEL", "claude-sonnet-4-6")
    return ScriptRuntimeSettings(
        work_dir=work_dir,
        speech_pace=speech_pace,
        atempo=atempo,
        target_duration_sec=target_duration_sec,
        script_density=script_density,
        trend_candidate_count=env_int("TREND_CANDIDATE_COUNT", 5),
        request_timeout=env_int("REQUEST_TIMEOUT", 20),
        claude_timeout=env_int("CLAUDE_TIMEOUT", 180),
        claude_http_retries=env_int("CLAUDE_HTTP_RETRIES", os.environ.get("CLAUDE_RETRIES", "2")),
        pubmed_query_timeout=env_int("PUBMED_QUERY_TIMEOUT", 60),
        pubmed_retmax=env_int("PUBMED_RETMAX", 3),
        pubmed_abstract_char_limit=env_int("PUBMED_ABSTRACT_CHAR_LIMIT", 7000),
        ncbi_api_key=os.environ.get("NCBI_API_KEY", ""),
        claude_model=claude_model,
        claude_script_model=os.environ.get("CLAUDE_SCRIPT_MODEL", claude_model),
        claude_query_model=os.environ.get("CLAUDE_QUERY_MODEL", os.environ.get("CLAUDE_STRATEGY_MODEL", "claude-haiku-4-5-20251001")),
        claude_research_model=os.environ.get("CLAUDE_RESEARCH_MODEL", os.environ.get("CLAUDE_SCRIPT_MODEL", claude_model)),
        claude_strategy_model=os.environ.get("CLAUDE_STRATEGY_MODEL", "claude-haiku-4-5-20251001"),
        claude_strategy_fallback_models=env_csv("CLAUDE_STRATEGY_FALLBACK_MODELS", ("claude-sonnet-4-5-20250929",)),
        claude_strategy_max_tokens=env_int("CLAUDE_STRATEGY_MAX_TOKENS", 2000),
        claude_planner_model=os.environ.get("CLAUDE_PLANNER_MODEL", "claude-haiku-4-5-20251001"),
        claude_critic_model=os.environ.get("CLAUDE_CRITIC_MODEL", "claude-haiku-4-5-20251001"),
        claude_audit_model=os.environ.get("CLAUDE_AUDIT_MODEL", "claude-haiku-4-5-20251001"),
        claude_interpreter_model=os.environ.get("CLAUDE_INTERPRETER_MODEL", "claude-haiku-4-5-20251001"),
        claude_planner_max_tokens=env_int("CLAUDE_PLANNER_MAX_TOKENS", 2400),
        claude_critic_max_tokens=env_int("CLAUDE_CRITIC_MAX_TOKENS", 1500),
        claude_audit_max_tokens=env_int("CLAUDE_AUDIT_MAX_TOKENS", 1600),
        claude_interpreter_max_tokens=env_int("CLAUDE_INTERPRETER_MAX_TOKENS", 900),
        # Percentile of historical planning_runs.adjusted_score used as the
        # limited_test/selected pass bar instead of a fixed number — see
        # docs/design/objective-driven-content-planner.md "동적 결정 임계값".
        claude_selection_percentile=env_float("CLAUDE_SELECTION_PERCENTILE", 0.5),
        # Same rationale as claude_selection_percentile, applied to the
        # confidence gate: a young channel's evidence-transfer confidence
        # (shrink_percentile x cohort_reliability) rarely clears an arbitrary
        # absolute 0.6 either, so the "selected" bar tracks what this channel
        # has actually produced instead. See "동적 결정 임계값" in
        # docs/design/objective-driven-content-planner.md.
        claude_confidence_percentile=env_float("CLAUDE_CONFIDENCE_PERCENTILE", 0.5),
        # adjusted_score points below the best candidate that still count as a
        # statistical tie. Candidates inside the band are picked at random so
        # unattended runs stop converging on one topic shape — see
        # objective_planner.select_within_band. 0 restores strict top-1.
        claude_selection_band=env_float("CLAUDE_SELECTION_BAND", 6.0),
        claude_job_budget_usd=env_float("CLAUDE_JOB_BUDGET_USD", 0.30),
        claude_daily_budget_usd=env_float("CLAUDE_DAILY_BUDGET_USD", 1.00),
        claude_max_web_searches_per_job=env_int("CLAUDE_MAX_WEB_SEARCHES_PER_JOB", 4),
        claude_cost_mode=os.environ.get("CLAUDE_COST_MODE", "balanced"),
        max_tokens=env_int("MAX_TOKENS", 2600),
        enable_web_research=env_bool("ENABLE_WEB_RESEARCH", True),
        web_research_timeout=env_int("WEB_RESEARCH_TIMEOUT", 60),
        web_research_max_uses=env_int("WEB_RESEARCH_MAX_USES", 2),
        web_research_max_tokens=env_int("WEB_RESEARCH_MAX_TOKENS", 900),
        web_research_max_tool_turns=env_int("WEB_RESEARCH_MAX_TOOL_TURNS", 2),
        enable_case_research=env_bool("ENABLE_CASE_RESEARCH", True),
        case_research_timeout=env_int("CASE_RESEARCH_TIMEOUT", 60),
        case_research_max_uses=env_int("CASE_RESEARCH_MAX_USES", 2),
        case_research_max_tokens=env_int("CASE_RESEARCH_MAX_TOKENS", 900),
        case_research_max_tool_turns=env_int("CASE_RESEARCH_MAX_TOOL_TURNS", 2),
        research_mode=os.environ.get("RESEARCH_MODE", "adaptive").strip().lower(),
        strategy_path=os.environ.get("STRATEGY_PATH", os.path.join(work_dir, "strategy.json")),
        youtube_feedback_strictness=youtube_feedback_strictness(
            os.environ.get("YOUTUBE_FEEDBACK_STRICTNESS", "balanced")
        ),
        youtube_feedback_auto_sync=env_bool("YOUTUBE_FEEDBACK_AUTO_SYNC", True),
        youtube_feedback_sync_ttl_hours=env_int("YOUTUBE_FEEDBACK_SYNC_TTL_HOURS", 6),
        total_chars=total_chars,
        prompt_target_chars=prompt_target_chars,
        min_scenes_estimate=min_scenes_estimate,
    )
