#!/usr/bin/env python3
"""Drive the pipeline without a chat bot.

This is what makes the flow schedulable.  A cron entry or systemd timer can
call `advance`, let the job run as far as its mode allows, and exit; the job's
state stays in its work directory so the next call -- minutes or hours later,
from a different process -- picks up exactly where it stopped.

    run_pipeline.py advance --job-id J --mode review --topic "..."
    run_pipeline.py approve --job-id J          # human cleared the gate
    run_pipeline.py status  --job-id J
    run_pipeline.py rewind  --job-id J --stage script

Exit codes are meant to be read by a scheduler:

    0   the job finished
    10  the job is parked at a human gate (not an error)
    1   the job stopped and needs attention

The bots use the same pipeline_flow core, so a job started here can be
approved from Telegram and vice versa.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

import job_state
import pipeline_flow
import script_review
import topic_candidate_pipeline

EXIT_DONE = 0
EXIT_FAILED = 1
EXIT_GATE = 10


def default_base_dir():
    # .../dev/src/common/run_pipeline.py -> .../dev
    return Path(os.environ.get("BASE_DIR") or Path(__file__).resolve().parents[2])


def resolve_work_dir(job_id):
    """Where this job's artifacts live, matching config.sh's WORK_DIR layout."""
    base = os.environ.get("WORK_DIR_BASE")
    if base:
        return Path(base) / job_id
    return default_base_dir() / "data" / "work" / job_id


def make_runner(job_id, settings, base_dir, verbose=True):
    """Run a stage's shell script, inheriting the environment config.sh needs."""
    work_dir = resolve_work_dir(job_id)

    def run(stage_name, argv):
        env = os.environ.copy()
        env["JOB_ID"] = job_id
        # config.sh keeps any WORK_DIR/OUTPUT_FILE already in the environment,
        # so a value inherited from another job would silently redirect this
        # one's artifacts.  Drop them and let config.sh derive them from JOB_ID.
        env.pop("WORK_DIR", None)
        env.pop("OUTPUT_FILE", None)
        if settings.get("topic"):
            env["TOPIC"] = settings["topic"]
        env.update({k: str(v) for k, v in (settings.get("env") or {}).items()})
        full_argv = list(argv) + pipeline_flow.stage_extra_args(stage_name, settings)
        if verbose:
            print(f"[{stage_name}] {' '.join(full_argv)}", flush=True)
        result = subprocess.run(full_argv, cwd=str(base_dir), env=env,
                                text=True, capture_output=True)
        log_path = work_dir
        log_path.mkdir(parents=True, exist_ok=True)
        (log_path / f"cli_{stage_name}.log").write_text(
            (result.stdout or "") + "\n" + (result.stderr or ""), encoding="utf-8"
        )
        if result.returncode != 0:
            tail = (result.stderr or result.stdout or "")[-1200:]
            raise RuntimeError(f"종료 코드 {result.returncode}\n{tail}")

    return run


def report_event(kind, payload):
    stage = payload.get("stage", "")
    if kind == "stage_start":
        suffix = " (재시도)" if payload.get("retry") else ""
        print(f"▶ {stage} 시작{suffix}", flush=True)
    elif kind == "stage_done":
        print(f"✔ {stage} 완료 {payload.get('reason', '')}".rstrip(), flush=True)
    elif kind == "guard_failed":
        print(f"✖ {stage} 점검 실패: {payload.get('reason', '')}", flush=True)
    elif kind == "gate":
        print(f"⏸ {payload.get('gate')} 게이트에서 대기합니다.", flush=True)
    elif kind == "pipeline_done":
        print("● 파이프라인 완료", flush=True)


def cmd_advance(args):
    work_dir = resolve_work_dir(args.job_id)
    work_dir.mkdir(parents=True, exist_ok=True)

    state = job_state.load(work_dir)
    settings = dict(state.get("settings") or {})

    topic = args.topic
    if args.from_eligible and not topic and not args.topic_json and not settings.get("topic"):
        picked = topic_candidate_pipeline.pick_top_eligible()
        if picked:
            topic = picked["title_hint"]
            print(f"eligible 큐에서 선택: {topic} (score={picked['score']}, topic_id={picked['topic_id']})")
        else:
            print("eligible 큐가 비어 있습니다. topic_candidate_pipeline.py --refresh를 먼저 실행하세요.", file=sys.stderr)

    # Topic details are stored once so later resume calls need not repeat them.
    for key, value in (("topic", topic), ("topic_json", args.topic_json)):
        if value:
            settings[key] = value
    if args.allow_no_pubmed:
        settings["allow_no_pubmed"] = True
    job_state.update(work_dir, settings=settings)

    base_dir = default_base_dir()
    result = pipeline_flow.advance(
        work_dir, base_dir,
        make_runner(args.job_id, settings, base_dir),
        mode=args.mode,
        on_event=report_event,
    )

    if result.status == pipeline_flow.STATUS_GATE:
        print(f"\n게이트: {result.gate} (JOB_ID={args.job_id})")
        if result.gate == "script_review":
            print(script_review.render_text(
                script_review.build_bundle(work_dir), script_limit=1200
            ))
        print(f"\n승인: run_pipeline.py approve --job-id {args.job_id}")
        return EXIT_GATE
    if result.status == pipeline_flow.STATUS_FAILED:
        print(f"\n중단: {result.stage} — {result.reason}", file=sys.stderr)
        return EXIT_FAILED
    return EXIT_DONE


def cmd_approve(args):
    work_dir = resolve_work_dir(args.job_id)
    gate = pipeline_flow.approve(work_dir)
    if gate is None:
        print("대기 중인 게이트가 없습니다.")
    else:
        print(f"{gate} 게이트를 승인했습니다. advance로 이어서 진행하세요.")
    return EXIT_DONE


def cmd_status(args):
    work_dir = resolve_work_dir(args.job_id)
    state = job_state.load(work_dir)
    print(f"JOB_ID: {args.job_id}")
    print(f"모드: {state['mode']}")
    print(f"완료 단계: {state['stage'] or '-'}")
    print(f"대기 게이트: {state['awaiting'] or '-'}")
    if state.get("attempts"):
        print(f"재시도: {state['attempts']}")
    if state.get("last_error"):
        print(f"마지막 오류: {state['last_error']}")
    return EXIT_GATE if state.get("awaiting") else EXIT_DONE


def cmd_rewind(args):
    work_dir = resolve_work_dir(args.job_id)
    previous = pipeline_flow.rewind_to(work_dir, args.stage)
    print(f"{args.stage} 단계부터 다시 실행합니다. (직전 완료: {previous or '-'})")
    return EXIT_DONE


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="브레인50 파이프라인 실행/재개")
    parser.add_argument("--job-id", default=os.environ.get("JOB_ID"), required=False)
    subparsers = parser.add_subparsers(dest="command", required=True)

    advance = subparsers.add_parser("advance", help="게이트나 완료까지 진행")
    advance.add_argument("--mode", choices=job_state.MODES, default=None,
                         help="기본값: 저장된 모드, 없으면 review")
    advance.add_argument("--topic", help="script 단계에 넘길 주제")
    advance.add_argument("--topic-json", help="topic_plan.json 경로")
    advance.add_argument("--from-eligible", action="store_true",
                          help="eligible 큐(Phase 1 topic_candidate_pipeline) 최고점 1건을 topic으로 사용하고 consumed 처리")
    advance.add_argument("--allow-no-pubmed", action="store_true")
    advance.set_defaults(func=cmd_advance)

    approve = subparsers.add_parser("approve", help="현재 게이트 승인")
    approve.set_defaults(func=cmd_approve)

    status = subparsers.add_parser("status", help="현재 상태 확인")
    status.set_defaults(func=cmd_status)

    rewind = subparsers.add_parser("rewind", help="지정 단계부터 다시 실행")
    rewind.add_argument("--stage", required=True, choices=pipeline_flow.STAGE_NAMES)
    rewind.set_defaults(func=cmd_rewind)

    args = parser.parse_args(argv)
    if not args.job_id:
        parser.error("--job-id 또는 JOB_ID 환경변수가 필요합니다.")
    return args


def main(argv=None):
    args = parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
