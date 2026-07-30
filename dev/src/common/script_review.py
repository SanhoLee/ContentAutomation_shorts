"""Assemble everything a human needs to judge a script, in one place.

The script-approval gate used to show `script.txt` and nothing else.  The
reviewer could not see why this topic was chosen, what evidence it rests on,
or whether automated validation passed -- those live in `topic_plan.json`,
`pubmed_status.json` and `script_quality.json`, sent separately or not at all.
Approving without them is approving blind, which is exactly the decision the
two-gate flow makes expensive: after this gate nobody looks again until the
finished video.

`build_bundle()` reads the job's artifacts and returns plain data.  Rendering
is deliberately a separate step (`render_text`) so Telegram, Slack and the CLI
can each apply their own length limit to the same bundle.

This module only reads files.  It knows nothing about bots, the stage graph,
or how the artifacts were produced -- a missing artifact is a skipped section,
never an error, because half the jobs (manually-typed topics) legitimately
have no `topic_plan.json`.
"""

from __future__ import annotations

import json
from pathlib import Path

# Section keys, in the order a reviewer should read them: why this topic ->
# how it is framed -> what backs it -> did checks pass -> the script itself.
SECTION_ORDER = ("topic_plan", "strategy", "evidence", "validation", "script")

SECTION_TITLES = {
    "topic_plan": "주제 선정 근거",
    "strategy": "기획 전략",
    "evidence": "논문 근거",
    "validation": "대본 검증",
    "script": "대본 전문",
}


def _read_json(path):
    """Return parsed JSON, or None when absent/unreadable.

    Unreadable is treated the same as absent on purpose: a corrupt artifact
    must not block the gate that would let a human notice the corruption.
    """
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None


def _read_text(path):
    try:
        return Path(path).read_text(encoding="utf-8", errors="replace")
    except (FileNotFoundError, OSError):
        return None


def _topic_plan_section(work_dir):
    plan = _read_json(Path(work_dir) / "topic_plan.json")
    if not isinstance(plan, dict):
        return None
    objective = plan.get("objective") or {}
    planning = plan.get("planning") or {}
    return {
        "topic": plan.get("topic", ""),
        "objective_type": objective.get("type", ""),
        "decision": objective.get("decision", ""),
        "confidence": objective.get("confidence"),
        "base_score": objective.get("base_score"),
        "adjusted_score": objective.get("adjusted_score"),
        "evidence_refs": objective.get("evidence_refs") or [],
        "reason": objective.get("reason", ""),
        "sync_status": objective.get("sync_status", ""),
        "candidate_count": planning.get("candidate_count"),
        "duplicates_rejected": planning.get("duplicates_rejected"),
        "claude_cost_usd": planning.get("claude_cost_usd"),
    }


def _strategy_section(work_dir):
    strategy = _read_json(Path(work_dir) / "strategy.json")
    if not isinstance(strategy, dict):
        return None
    return {
        "title": strategy.get("title", ""),
        "main_keyword": strategy.get("main_keyword", ""),
        "hook_type": strategy.get("hook_type", ""),
        "core_message": strategy.get("core_message", ""),
        "search_intent": strategy.get("search_intent", ""),
    }


def _evidence_section(work_dir):
    status = _read_json(Path(work_dir) / "pubmed_status.json")
    if not isinstance(status, dict):
        return None
    return {
        "status": status.get("status", ""),
        "pubmed_query": status.get("pubmed_query", ""),
        "pmid_count": status.get("pmid_count", 0),
        "pmids": status.get("pmids") or [],
        "citations": status.get("citations") or [],
        # Only meaningful when status != "ok"; explains why evidence is thin.
        "message": status.get("message", ""),
    }


def _validation_section(work_dir):
    quality = _read_json(Path(work_dir) / "script_quality.json")
    if not isinstance(quality, dict):
        return None
    return {
        "ok": quality.get("ok"),
        "errors": quality.get("errors") or [],
        "warnings": quality.get("warnings") or [],
        "metrics": quality.get("metrics") or {},
    }


def build_bundle(work_dir):
    """Collect the review material for one job.

    Returns {"work_dir": str, "sections": {name: data}, "missing": [names]}.
    Sections whose artifact is absent are omitted and listed under "missing"
    so the caller can tell "no evidence" from "evidence not collected".
    """
    work_dir = Path(work_dir)
    builders = {
        "topic_plan": _topic_plan_section,
        "strategy": _strategy_section,
        "evidence": _evidence_section,
        "validation": _validation_section,
    }
    sections = {}
    missing = []
    for name in SECTION_ORDER:
        if name == "script":
            script = _read_text(work_dir / "script.txt")
            if script is None:
                missing.append(name)
            else:
                sections[name] = {"text": script}
            continue
        data = builders[name](work_dir)
        if data is None:
            missing.append(name)
        else:
            sections[name] = data
    return {"work_dir": str(work_dir), "sections": sections, "missing": missing}


def _format_topic_plan(data):
    lines = [f"주제: {data['topic']}"]
    if data.get("objective_type"):
        lines.append(f"목표: {data['objective_type']} / 판정: {data.get('decision', '')}")
    scores = []
    if data.get("adjusted_score") is not None:
        scores.append(f"조정점수 {data['adjusted_score']}")
    if data.get("confidence") is not None:
        scores.append(f"확신도 {data['confidence']}")
    if scores:
        lines.append(" / ".join(scores))
    if data.get("candidate_count") is not None:
        lines.append(f"후보 {data['candidate_count']}개 / 중복 반려 {data.get('duplicates_rejected', 0)}개")
    if data.get("evidence_refs"):
        lines.append("근거: " + ", ".join(str(ref) for ref in data["evidence_refs"]))
    if data.get("reason"):
        lines.append(f"주의: {data['reason']}")
    return lines


def _format_strategy(data):
    lines = []
    for label, key in (
        ("제목", "title"), ("핵심 키워드", "main_keyword"), ("훅 유형", "hook_type"),
        ("핵심 메시지", "core_message"), ("검색 의도", "search_intent"),
    ):
        if data.get(key):
            lines.append(f"{label}: {data[key]}")
    return lines


def _format_evidence(data):
    if data.get("status") == "ok":
        lines = [f"PubMed 논문 {data.get('pmid_count', 0)}건"]
        for citation in data.get("citations") or []:
            lines.append(f"- {citation}")
        if not data.get("citations") and data.get("pmids"):
            lines.append("PMID: " + ", ".join(str(p) for p in data["pmids"]))
        return lines
    # Not an error -- the script was still written, just from general
    # knowledge.  The reviewer needs to weigh claims more carefully.
    lines = ["PubMed 직접 검색 결과 없이 생성했습니다."]
    if data.get("pubmed_query"):
        lines.append(f"검색어: {data['pubmed_query']}")
    if data.get("message"):
        lines.append(f"원인 추정: {data['message']}")
    return lines


def _format_validation(data):
    lines = ["통과" if data.get("ok") else "실패"]
    for error in data.get("errors") or []:
        lines.append(f"[오류] {error}")
    for warning in data.get("warnings") or []:
        lines.append(f"[경고] {warning}")
    metrics = data.get("metrics") or {}
    summary = []
    if metrics.get("scene_count") is not None:
        summary.append(f"씬 {metrics['scene_count']}개")
    if metrics.get("length_ratio") is not None:
        summary.append(f"길이비 {metrics['length_ratio']}")
    if metrics.get("final_answer_present") is not None:
        summary.append("결론 " + ("있음" if metrics["final_answer_present"] else "없음"))
    if summary:
        lines.append(" / ".join(summary))
    return lines


_FORMATTERS = {
    "topic_plan": _format_topic_plan,
    "strategy": _format_strategy,
    "evidence": _format_evidence,
    "validation": _format_validation,
}


def render_text(bundle, limit=None, script_limit=None):
    """Render a bundle as review text.

    `limit` caps the whole message; `script_limit` caps only the script body,
    which is what should shrink first -- the summary blocks are the part the
    reviewer cannot reconstruct from the attached `script.txt` file.
    """
    blocks = []
    for name in SECTION_ORDER:
        data = bundle["sections"].get(name)
        if data is None:
            continue
        title = SECTION_TITLES[name]
        if name == "script":
            text = data["text"]
            if script_limit is not None and len(text) > script_limit:
                text = text[:script_limit] + "\n...(생략)"
            blocks.append(f"■ {title}\n{text}")
            continue
        lines = _FORMATTERS[name](data)
        if lines:
            blocks.append(f"■ {title}\n" + "\n".join(lines))
    rendered = "\n\n".join(blocks)
    if limit is not None and len(rendered) > limit:
        rendered = rendered[:limit] + "\n...(생략)"
    return rendered


def evidence_is_weak(bundle):
    """True when the script was written without direct PubMed results.

    Surfaced separately so a caller can flag the gate message rather than
    relying on the reviewer to spot it inside the evidence block.
    """
    evidence = bundle["sections"].get("evidence")
    return bool(evidence) and evidence.get("status") != "ok"


def validation_failed(bundle):
    validation = bundle["sections"].get("validation")
    return bool(validation) and validation.get("ok") is False
