#!/usr/bin/env python3
"""scene_visuals.py — decide what each scene shows, after the script is final.

Stage 2 used to emit the script body and the visual plan in one Claude call,
which meant the plan was fixed before the operator was ever allowed to touch
the words. `script.txt` is literally `scenes.json` flattened
("\\n\\n".join(scene["text"])), so editing the body at the approval gate left
the two copies of the same object disagreeing: voice and captions followed the
edit, `role`/`visual`/`visual_query` did not, and B-roll went looking for
footage that matched the sentences the operator had just deleted.

This stage runs after the script gate, on the approved text:

  1. re-sync scene text from `script.txt` (paragraph i <-> scene i)
  2. plan `visual` + `visual_query` for the text as it now reads
  3. fill deterministically for anything the plan is missing
  4. refresh `content_package.json` so the X/Instagram adapters see the edit

Never fails. A Claude outage, a blown budget or malformed JSON falls through
to the deterministic fill, because a job that has a finished, approved script
must not be thrown away over a downstream-only field (KNOWN_ISSUES #6/#10).
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import re
import sys
from pathlib import Path
from typing import Any

COMMON_DIR = Path(__file__).resolve().parent
if str(COMMON_DIR) not in sys.path:
    sys.path.insert(0, str(COMMON_DIR))

import claude_cost
import content_package
import story_types
from script_runtime import env_bool

# Operational kill switch for the planning call alone. Off still re-syncs
# scene text to the approved script and still fills every field
# deterministically -- what stops is the per-job Haiku spend.
PLAN_WITH_CLAUDE = env_bool("SCENE_VISUALS_PLAN", True)
USE_STORY_TYPES = env_bool("USE_STORY_TYPES", True)

VISUAL_MODEL = os.environ.get("CLAUDE_STRATEGY_MODEL", "claude-haiku-4-5-20251001")
VISUAL_MAX_TOKENS = int(os.environ.get("SCENE_VISUALS_MAX_TOKENS", "2000"))
VISUAL_TIMEOUT_SEC = int(os.environ.get("SCENE_VISUALS_TIMEOUT_SEC", "90"))
# Same order of magnitude as the x_thread humanize pass: a few thousand
# Haiku tokens. Checked before the call so a job near its cap skips
# planning instead of tripping the budget guard mid-stage.
VISUAL_ANTICIPATED_COST_USD = 0.01

JSON_OBJECT = re.compile(r"\{.*\}", re.DOTALL)


def clip_at_word_boundary(text, limit):
    """Trim to a word boundary. On-screen copy must never end mid-word."""
    value = re.sub(r"\s+", " ", str(text or "")).strip()
    if len(value) <= limit:
        return value
    head, _, _ = value[:limit].rstrip().rpartition(" ")
    return (head or value[:limit]).rstrip(" ,·:-")


def positional_role(index, total, sequence):
    """Map scene position onto the genre's role sequence.

    hook and cta are pinned to the first and last scenes; the middle roles are
    spread evenly over what remains, so a 10-scene script over a 7-role
    sequence repeats middle roles rather than dropping any.
    """
    if index == 0 or total <= 1:
        return sequence[0]
    if index == total - 1:
        return sequence[-1]
    middle = sequence[1:-1] or sequence
    span = max(1, total - 2)
    return middle[min(len(middle) - 1, (index - 1) * len(middle) // span)]


def fallback_visual_brief(scene):
    """A directing line derived from what the scene already says.

    Used when the planning call produced no brief for this scene. Backfilling
    deterministically beats the alternatives: a retry would double the cost,
    and failing would throw away a finished script over a downstream-only
    field. The omission is counted in script_quality.json so a model that
    keeps skipping it stays visible rather than silently patched over.
    """
    text = re.sub(r"\s+", " ", str(scene.get("text") or "")).strip()
    if text:
        return clip_at_word_boundary(text, 40)
    return clip_at_word_boundary(str(scene.get("visual_query") or "") or "장면 화면", 40)


# ─────────────────────────────────────────────
# 1. 최종 대본 → 씬 텍스트 재동기화
# ─────────────────────────────────────────────

def split_script_paragraphs(script_text: str) -> list[str]:
    """Undo write_outputs' "\\n\\n".join(scene["text"]).

    Exact by construction when the operator only edited wording. Split on any
    newline, not just blank ones: an edit pasted back through Slack comes in
    with the blank lines flattened away (the real job that prompted this stage
    had 8 scenes on 8 consecutive lines), and splitting on "\\n\\n" there would
    fold the whole script into a single scene. Scene text is one paragraph of
    prose, so a newline inside one is not a case worth protecting.
    """
    return [block.strip() for block in re.split(r"\n\s*", str(script_text or "")) if block.strip()]


def resync_scene_text(scenes: list[dict], paragraphs: list[str], sequence) -> tuple[list[dict], int]:
    """Return the scene list carrying the approved text, and how many changed.

    Equal counts is the normal case -- paragraph i is scene i, and everything
    else about the scene is kept. When the operator merged or split
    paragraphs the counts diverge, so the scene list is rebuilt from the
    paragraphs and roles are re-derived positionally: a scene whose text came
    from somewhere else has no claim to the old scene's role, and keeping a
    stale one would tell the visual planner the wrong beat.
    """
    if not paragraphs:
        return scenes, 0

    if len(paragraphs) == len(scenes):
        changed = 0
        resynced = []
        for scene, text in zip(scenes, paragraphs):
            scene = dict(scene)
            if scene.get("text") != text:
                changed += 1
            scene["text"] = text
            resynced.append(scene)
        return resynced, changed

    total = len(paragraphs)
    rebuilt = []
    for index, text in enumerate(paragraphs):
        scene = {"text": text}
        if USE_STORY_TYPES:
            scene["role"] = positional_role(index, total, sequence)
        rebuilt.append(scene)
    return rebuilt, total


# ─────────────────────────────────────────────
# 2. 시각 계획 (Claude 1회, 실패해도 진행)
# ─────────────────────────────────────────────

PLAN_PROMPT = """아래는 이미 확정된 유튜브 쇼츠 대본입니다. 각 Scene이 화면에 무엇을 보여줄지 계획하세요.
대본 문장은 절대 고치지 마세요. 시각 계획만 출력합니다.

[주제] {topic}
[제목] {title}

[Scene 목록]
{numbered}

[visual — 이 Scene에서 화면에 무엇이 보여야 하는가]
- 분위기용 배경이 아니라 말하고 있는 대상 자체가 보여야 합니다.
- type: {visual_types} 중 하나
- brief: 한국어 한 줄 연출 지시. (예: "식탁에 앉아 약통을 열다 멈추는 손")
- must_show: 화면에 반드시 보여야 할 요소 1~3개(한국어 배열)
- 같은 brief를 두 Scene 이상에서 반복하지 마세요.

[visual_query — 스톡 영상 검색어]
- 장면 내용을 그대로 번역하지 말고, 스톡 사이트에 실제로 영상이 많이 존재하는 **일반적인 검색어**로 쓰세요. 영어 키워드 3~4개.
- 구성 규칙: 일반 키워드 2~3개 + 이 영상 주제와 연결되는 키워드 1개(최대 2개). 주제 키워드를 3개 이상 넣으면 검색 결과가 거의 없어 매번 같은 영상만 나옵니다.
- 반드시 사람이 실제 속도로 움직이는 장면을 고르세요. 정지된 풍경, 잔잔한 배경, 슬로우모션·타임랩스로 촬영된 영상은 화면이 늘어져 보입니다.
- 검색어에는 사람을 가리키는 단어(person / man / woman / senior / couple / hands)가 반드시 하나 들어가야 합니다. 뇌·신경·세포처럼 눈에 안 보이는 것을 설명하는 Scene도 그것을 겪고 있는 사람을 찍은 영상으로 보여주세요.
- "animation", "illustration", "diagram", "graphic", "3d render", "microscope", "x-ray" 같은 단어도 넣지 마세요. 스톡 사이트에서 이런 영상은 조악한 자료화면만 나옵니다.
- 움직임 키워드 예: walking, cooking, stretching, talking, pouring, exercising, gardening, laughing, commuting
- "slow motion", "timelapse", "cinematic", "aerial", "drone", "calm", "peaceful", "serene", "background" 같은 단어는 넣지 마세요. 이런 단어는 느리고 정적인 영상만 검색됩니다.
- 장면마다 서로 다른 동작·장소를 쓰세요. 같은 검색어나 거의 같은 조합을 두 장면 이상에서 반복하지 마세요.
- 차가운 병원, MRI, 주사기 등 공포감을 주는 이미지는 절대 금지입니다.
- 좋은 예: "senior couple walking outdoors morning", "woman cooking kitchen healthy", "elderly man stretching living room"
- 나쁜 예: "deep sleep brain memory decline neuron"(주제어만 나열 → 검색 결과 없음), "calm peaceful background"(움직임 없음)

반드시 아래 JSON 객체 포맷으로만 출력하세요. scenes 배열의 길이는 정확히 {count}개여야 합니다.
마크다운이나 추가 설명은 절대 넣지 마세요.

{{"scenes": [{{"index": 1, "visual": {{"type": "paradox", "brief": "한 줄 연출 지시", "must_show": ["요소1"]}}, "visual_query": "english search keywords"}}]}}"""


def build_plan_prompt(scenes: list[dict], *, topic: str, title: str) -> str:
    numbered = "\n".join(
        f"{i}. [{scene.get('role') or '-'}] {scene.get('text', '')}"
        for i, scene in enumerate(scenes, start=1)
    )
    return PLAN_PROMPT.format(
        topic=topic or "뇌 건강",
        title=title or "(없음)",
        numbered=numbered,
        count=len(scenes),
        visual_types=" / ".join(story_types.VISUAL_TYPES),
    )


def plan_with_claude(scenes: list[dict], *, topic: str, title: str) -> list[dict] | None:
    """One bounded, non-retrying planning pass. None on any failure.

    Returns a list parallel to `scenes`; a response whose length does not
    match is rejected outright, because a shifted plan is worse than none --
    the caller's deterministic fill at least stays in step with the text.
    """
    if not scenes or not PLAN_WITH_CLAUDE:
        return None
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        return None
    try:
        claude_cost.assert_budget(anticipated_cost_usd=VISUAL_ANTICIPATED_COST_USD)
    except RuntimeError:
        return None

    import requests

    payload = {
        "model": VISUAL_MODEL,
        "max_tokens": VISUAL_MAX_TOKENS,
        "messages": [{"role": "user", "content": build_plan_prompt(scenes, topic=topic, title=title)}],
    }
    try:
        res = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": api_key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json=payload,
            timeout=VISUAL_TIMEOUT_SEC,
        )
        res.raise_for_status()
        data = res.json()
    except Exception as exc:
        print(f"시각 계획 호출 실패, 기본값으로 채웁니다: {exc}", file=sys.stderr)
        return None

    with contextlib.suppress(Exception):
        claude_cost.record_usage("scene_visuals", VISUAL_MODEL, data)

    text = "".join(
        block.get("text", "") for block in data.get("content") or [] if block.get("type") == "text"
    )
    match = JSON_OBJECT.search(text)
    if not match:
        return None
    try:
        parsed = json.loads(match.group(0))
    except json.JSONDecodeError:
        return None
    planned = parsed.get("scenes") if isinstance(parsed, dict) else None
    if not isinstance(planned, list) or len(planned) != len(scenes):
        print(
            f"시각 계획 응답이 씬 수와 맞지 않아 무시합니다 "
            f"({len(planned) if isinstance(planned, list) else '없음'} vs {len(scenes)}개)",
            file=sys.stderr,
        )
        return None
    return [item if isinstance(item, dict) else {} for item in planned]


# ─────────────────────────────────────────────
# 3. 결정론적 채우기 — 여기서는 절대 실패하지 않는다
# ─────────────────────────────────────────────

def fill_scene_visuals(scenes: list[dict], planned: list[dict] | None, sequence) -> tuple[list[dict], int]:
    """Give every scene a `role`, a `visual` and a `visual_query`.

    2_caption.py carries these keys straight through to scenes_timed.json and
    3_broll.py indexes `visual_query` directly, so a scene missing one would
    KeyError mid-pipeline. The Korean brief cannot stand in for the query --
    Pexels is searched in English.

    With story types off, scenes keep their pre-story_type shape
    ({text, visual_query}) -- USE_STORY_TYPES=0 has to roll back to exactly
    what the pipeline produced before genres existed (story-types.md §6).
    """
    filled = []
    missing_brief = 0
    for index, raw in enumerate(scenes):
        scene = dict(raw) if isinstance(raw, dict) else {"text": str(raw)}
        proposed = (planned[index] if planned else None) or {}
        if not USE_STORY_TYPES:
            query = re.sub(r"\s+", " ", str(proposed.get("visual_query") or "")).strip()
            scene["visual_query"] = query or story_types.FALLBACK_VISUAL_QUERY
            filled.append(scene)
            continue

        role = str(scene.get("role") or "").strip()
        if role not in sequence:
            role = positional_role(index, len(scenes), sequence)
        scene["role"] = role

        visual = proposed.get("visual")
        visual = dict(visual) if isinstance(visual, dict) else {}

        visual_type = str(visual.get("type") or "").strip()
        if visual_type not in story_types.VISUAL_TYPES:
            visual_type = story_types.default_visual_type(role)
        brief = re.sub(r"\s+", " ", str(visual.get("brief") or "")).strip()
        if not brief:
            missing_brief += 1
            brief = fallback_visual_brief(scene)
        must_show = visual.get("must_show")
        if not isinstance(must_show, list):
            must_show = [must_show] if must_show else []
        must_show = [str(item).strip() for item in must_show if str(item or "").strip()]
        scene["visual"] = {"type": visual_type, "brief": brief, "must_show": must_show[:3]}

        query = re.sub(r"\s+", " ", str(proposed.get("visual_query") or "")).strip()
        scene["visual_query"] = query or story_types.FALLBACK_VISUAL_QUERY
        filled.append(scene)
    return filled, missing_brief


# ─────────────────────────────────────────────
# 스테이지 진입점
# ─────────────────────────────────────────────

def _read_json(path: Path, default=None):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


def _record_quality(job_dir: Path, warnings: list[dict], metrics: dict) -> None:
    """Fold this stage's findings into script_quality.json.

    Warnings, never gates: the operator sees a model that keeps skipping
    briefs, but the job keeps moving (KNOWN_ISSUES #6).
    """
    quality = _read_json(job_dir / "script_quality.json", None)
    if not isinstance(quality, dict):
        quality = {"ok": True, "errors": [], "warnings": [], "metrics": {}}
    existing = [w for w in quality.get("warnings") or [] if w.get("code") not in {w2["code"] for w2 in warnings}]
    quality["warnings"] = existing + warnings
    quality.setdefault("metrics", {}).update(metrics)
    with contextlib.suppress(OSError):
        (job_dir / "script_quality.json").write_text(
            json.dumps(quality, ensure_ascii=False, indent=2) + "\n", encoding="utf-8",
        )


def refresh(job_dir: str | Path) -> dict[str, Any] | None:
    """Re-plan visuals for the approved script. Returns a summary, or None
    when there is nothing to work with (no scenes.json yet)."""
    job_dir = Path(job_dir)
    scenes = _read_json(job_dir / "scenes.json", None)
    if not isinstance(scenes, list) or not scenes:
        print("scenes.json이 없어 시각 계획을 건너뜁니다.")
        return None

    video_meta = _read_json(job_dir / "video_meta.json", {}) or {}
    strategy = _read_json(job_dir / "strategy.json", {}) or {}
    story_type = str(
        video_meta.get("story_type") or strategy.get("story_type") or ""
    ) or story_types.load_config()["default_story_type"]
    sequence = story_types.role_sequence(story_type)

    try:
        script_text = (job_dir / "script.txt").read_text(encoding="utf-8")
    except OSError:
        script_text = ""
    scenes, resynced = resync_scene_text(scenes, split_script_paragraphs(script_text), sequence)
    if resynced:
        print(f"승인된 본문에 맞춰 씬 텍스트 {resynced}개를 다시 맞췄습니다.")

    planned = plan_with_claude(
        scenes,
        topic=str(video_meta.get("topic") or strategy.get("topic") or ""),
        title=str(video_meta.get("title") or ""),
    )
    scenes, missing_brief = fill_scene_visuals(scenes, planned, sequence)

    (job_dir / "scenes.json").write_text(
        json.dumps(scenes, ensure_ascii=False, indent=2) + "\n", encoding="utf-8",
    )

    warnings = []
    if planned is None:
        warnings.append({
            "code": "visual_plan_unavailable",
            "level": "warning",
            "message": "시각 계획 호출이 실패해 기본 검색어로 채웠습니다. B-roll이 일반적인 영상으로 나갑니다.",
        })
    if missing_brief:
        warnings.append({
            "code": "visual_brief_backfilled",
            "level": "warning",
            "message": f"visual.brief가 없는 장면 {missing_brief}개를 대본 문장으로 채웠습니다.",
        })
    _record_quality(job_dir, warnings, {
        "scene_count": len(scenes),
        "scenes_with_visual_brief": len(scenes) - missing_brief,
        "visual_plan_source": "claude" if planned else "fallback",
    })
    for warning in warnings:
        print(f"⚠️  {warning['message']}")

    # Rebuilt here rather than left to Stage 2: the package feeds the X and
    # Instagram adapters, and it was written before the operator's edit.
    content_package.build_content_package(job_dir)

    for index, scene in enumerate(scenes, start=1):
        print(f"{index}: {scene['role']} | {scene['visual_query']}")
    return {"scenes": scenes, "resynced": resynced, "planned": planned is not None}


def resolve_work_dir(job_id: str) -> Path:
    base = os.environ.get("WORK_DIR_BASE") or os.environ.get("WORK_DIR")
    if base:
        return Path(base) / job_id
    return Path(__file__).resolve().parents[2] / "data" / "work" / job_id


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="확정된 대본 기준으로 씬 시각 계획 생성")
    parser.add_argument("--job-id", required=True)
    args = parser.parse_args(argv)
    refresh(resolve_work_dir(args.job_id))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
