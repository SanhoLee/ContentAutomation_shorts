import json
import os
import re
import signal
import shlex
import subprocess
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path
from urllib.request import Request, urlopen

from script_runtime import speech_pace_profile

# Slack Socket Mode transport configuration.
BOT_TOKEN = os.environ.get("SLACK_BOT_TOKEN")
APP_TOKEN = os.environ.get("SLACK_APP_TOKEN")
ALLOWED_CHANNEL_ID = os.environ.get("SLACK_CHANNEL_ID")
ALLOWED_USER_ID = os.environ.get("SLACK_ALLOWED_USER_ID")
ALLOWED_CHAT_ID = None
BASE_DIR = Path(os.environ.get("BASE_DIR", Path.cwd())).resolve()
WORK_DIR_BASE = Path(os.environ.get("WORK_DIR_BASE", BASE_DIR / "data" / "work"))
OUTPUT_DIR = Path(os.environ.get("OUTPUT_DIR", BASE_DIR / "data" / "output"))
STATE_PATH = Path(os.environ.get("SLACK_STATE_PATH", BASE_DIR / "data" / "slack_state.json"))
MAX_TEXT_PREVIEW = int(os.environ.get("SLACK_MAX_TEXT_PREVIEW", "3500"))
DEFAULT_CAPTION_FONT_SIZE = os.environ.get("SLACK_DEFAULT_CAPTION_FONT_SIZE", "62")
DEFAULT_CAPTION_MARGIN_V = os.environ.get("SLACK_DEFAULT_CAPTION_MARGIN_V", "60")
DEFAULT_CAPTION_STYLE = os.environ.get("SLACK_DEFAULT_CAPTION_STYLE", os.environ.get("CAPTION_STYLE", "default"))
DEFAULT_CAPTION_MARGIN_H = os.environ.get("SLACK_DEFAULT_CAPTION_MARGIN_H", "10")
DEFAULT_WEB_RESEARCH = os.environ.get("SLACK_DEFAULT_WEB_RESEARCH", "true").lower() not in ("off", "0", "false", "no")
STATE_LOCK = threading.Lock()
ACTIVE_PROCESS_LOCK = threading.Lock()
ACTIVE_PROCESSES = {}
CANCELLED_JOB_IDS = set()
_STATE = {"chats": {}}
MAX_BLOCK_TEXT = 3000


class WorkflowCancelled(RuntimeError):
    pass


def _require_tokens():
    missing = [name for name, value in (("SLACK_BOT_TOKEN", BOT_TOKEN), ("SLACK_APP_TOKEN", APP_TOKEN)) if not value]
    if missing:
        raise SystemExit(f"{' and '.join(missing)} are required")


def _slack_client():
    try:
        from slack_sdk import WebClient
    except ImportError as exc:
        raise SystemExit("Slack dependency is missing. Run: python3 -m pip install -r requirements-slack.txt") from exc
    return WebClient(token=BOT_TOKEN)


def _thread_for(channel_id):
    return chat_state(_STATE, channel_id).get("slack_thread_ts")


def send_message(channel_id, text):
    kwargs = {"channel": str(channel_id), "text": str(text)}
    if thread_ts := _thread_for(channel_id):
        kwargs["thread_ts"] = thread_ts
    return _slack_client().chat_postMessage(**kwargs)


def send_file_or_path(channel_id, path, caption=None, as_video=False):
    path = Path(path)
    if not path.exists():
        return send_message(channel_id, f"파일을 찾지 못했습니다: {path}")
    kwargs = {"channel": str(channel_id), "file": str(path), "filename": path.name, "title": caption or path.name}
    if thread_ts := _thread_for(channel_id):
        kwargs["thread_ts"] = thread_ts
    try:
        return _slack_client().files_upload_v2(**kwargs)
    except Exception as exc:
        return send_message(channel_id, f"파일 전송 실패: {exc}\n서버에서 확인하세요: {path}")


def _blocks(text, rows):
    blocks = [{"type": "section", "text": {"type": "mrkdwn", "text": text[:MAX_BLOCK_TEXT]}}]
    for row_index, row in enumerate(rows):
        elements = [
            {"type": "button", "action_id": f"workflow_action_{row_index}_{element_index}", "text": {"type": "plain_text", "text": item["text"][:75]}, "value": item["callback_data"]}
            for element_index, item in enumerate(row)
        ]
        if elements:
            blocks.append({"type": "actions", "elements": elements})
    return blocks


def send_action_message(channel_id, text, rows):
    kwargs = {"channel": str(channel_id), "text": text[:MAX_BLOCK_TEXT], "blocks": _blocks(text, rows)}
    if thread_ts := _thread_for(channel_id):
        kwargs["thread_ts"] = thread_ts
    return _slack_client().chat_postMessage(**kwargs)


def download_slack_file(document, destination):
    url = document.get("url_private_download") or document.get("url_private")
    if not url and document.get("id"):
        info = _slack_client().files_info(file=document["id"])
        file_info = info.get("file", {})
        url = file_info.get("url_private_download") or file_info.get("url_private")
    if not url:
        raise RuntimeError("Slack 파일 다운로드 URL을 받지 못했습니다.")
    request = Request(url, headers={"Authorization": f"Bearer {BOT_TOKEN}"})
    with urlopen(request, timeout=60) as response:
        destination.write_bytes(response.read())

def editable_stage_info(stage, job_id):
    if not job_id:
        return None
    base = work_dir(job_id)
    mapping = {
        "await_script_approval": (base / "script.txt", "script.txt"),
        "await_caption_approval": (base / "subs.srt", "subs.srt"),
        "await_upload_meta_approval": (base / "video_meta.json", "video_meta.json"),
    }
    return mapping.get(stage)


WORKFLOW_STAGES = (
    "await_script_approval",
    "await_tts_approval",
    "await_caption_approval",
    "await_broll_approval",
    "await_render_config",
    "await_render_approval",
    "await_upload_meta_approval",
)
STAGE_LABELS = {
    "await_script_approval": "스크립트 확인",
    "await_tts_approval": "음성 확인",
    "await_caption_approval": "자막 확인",
    "await_broll_approval": "B-roll 확인",
    "await_render_config": "렌더 설정",
    "await_render_approval": "최종 영상 확인",
    "await_upload_meta_approval": "업로드 정보 확인",
    "await_trend_choice": "트렌드 선택",
    "await_pubmed_retry": "근거 검색 재시도",
    "running_auto": "끝까지 자동 처리",
    "running_after_review": "끝까지 자동 처리",
    "done": "완료",
    "cancelled": "취소됨",
}


def button(text, callback_data):
    return {"text": text, "callback_data": callback_data}


def log_event(level, event, **fields):
    """Write a compact, journalctl-friendly Slack event without message bodies."""
    parts = [f"[{str(level).upper()}]", str(event)]
    for key, value in fields.items():
        if value in (None, ""):
            continue
        normalized = str(value).replace("\n", " ").replace("\r", " ")[:500]
        parts.append(f"{key}={json.dumps(normalized, ensure_ascii=False)}")
    print(" ".join(parts), flush=True)


def action_request_label(data):
    exact = {
        "show_home": "콘텐츠 홈 열기",
        "start_cancel": "시작 취소 후 홈으로 이동",
        "start_reenter_topic": "제작 주제 다시 입력",
        "start_goal": "목표 기반 자동 기획 열기",
        "goal:confirm": "목표 기반 기획 실행",
        "goal:cancel": "목표 기획 취소 후 홈으로 이동",
        "open_settings": "제작 설정 열기",
        "show_status": "현재 작업 상태 확인",
        "cancel_all": "전체 작업 취소 확인",
        "cancel_confirm": "전체 작업 취소 실행",
        "cfg:root": "제작 설정 홈 열기",
        "cfg:all": "전체 설정 확인",
        "cfg:reset": "설정 초기화 확인",
        "cfg:reset_confirm": "설정 초기화 실행",
        "proceed_no_pubmed": "근거 부족 상태로 계속 진행",
        "retry_topic": "새 주제로 다시 시도",
    }
    if data in exact:
        return exact[data]
    if data.startswith("start_content:") or data.startswith("start_confirm:"):
        mode = data.split(":", 1)[1]
        mode_label = START_MODES.get(mode, {}).get("label", mode)
        suffix = "선택" if data.startswith("start_content:") else "실행 확인"
        return f"{mode_label} {suffix}"
    if data.startswith("goal:objective:"):
        objective = data.split(":", 2)[2]
        return f"{GOAL_OBJECTIVES.get(objective, {}).get('label', objective)} 목표 선택"
    if data.startswith("goal:seed:"):
        return "목표 기획 씨드 방식 선택"
    if data.startswith("goal:back:"):
        return "목표 기획 이전 단계로 이동"
    if data.startswith("cfg:cat:"):
        category_id = data.split(":", 2)[2]
        category = next((item for item in CONFIG_CATEGORIES if item[0] == category_id), None)
        return f"{category[1] if category else '설정 카테고리'} 열기"
    if data.startswith(("cfg:item:", "cfg:edit:", "cfg:keep:", "cfg:default:")):
        setting_id = data.split(":", 2)[2]
        setting = CONFIG_SETTINGS.get(setting_id, {})
        action = "값 입력" if data.startswith("cfg:edit:") else "기본값 적용" if data.startswith("cfg:default:") else "현재값 유지" if data.startswith("cfg:keep:") else "설정 열기"
        return f"{setting.get('label', setting_id)} {action}"
    if data.startswith("cfg:pick:"):
        parts = data.split(":", 3)
        setting = CONFIG_SETTINGS.get(parts[2], {}) if len(parts) > 2 else {}
        return f"{setting.get('label', parts[2] if len(parts) > 2 else '설정')} 값 선택"
    prefix_labels = {
        "approve:": "현재 단계 승인 및 다음 단계 실행",
        "edit:": "현재 산출물 수정",
        "edit_body:": "스크립트 본문 수정",
        "edit_title_menu:": "제목 수정 메뉴 열기",
        "edit_title_field:": "제목 항목 수정",
        "auto_upload:": "여기서부터 끝까지 실행 확인",
        "auto_finish:": "여기서부터 끝까지 실행 확인",
        "auto_finish_confirm:": "여기서부터 끝까지 자동 실행",
        "pick_trend:": "트렌드 선택 및 제작",
        "back:": "이전 단계로 이동",
        "render:": "선택한 설정으로 렌더링",
        "rerun:": "현재 산출물 재생성",
    }
    for prefix, label in prefix_labels.items():
        if data.startswith(prefix):
            return label
    return "선택한 버튼 작업"


def _send_recovery_error(chat_id, data, exc, label=None):
    label = label or action_request_label(data)
    text = f"요청 처리 실패: {label}\n오류: {exc}\n\n아래 버튼으로 안전하게 돌아가 다시 선택할 수 있습니다."
    rows = [[button("⌂ 홈", "show_home"), button("제작 설정", "open_settings"), button("현재 작업", "show_status")]]
    try:
        send_action_message(chat_id, text, rows)
    except Exception:
        send_message(chat_id, text)


START_MODES = {
    "review": {"label": "단계별로 검수하며 제작", "command": "/run"},
    "auto": {"label": "처음부터 끝까지 자동 제작", "command": "/run_auto"},
    "trend": {"label": "트렌드 후보에서 시작", "command": "/trend"},
}

GOAL_OBJECTIVES = {
    "subscriber_growth": {
        "label": "구독자 증가",
        "description": "조회수 대비 순 구독 전환을 우선합니다.",
    },
    "reach": {
        "label": "조회수·도달",
        "description": "도달, 초반 몰입, 새로움을 우선합니다.",
    },
    "retention": {
        "label": "평균 시청률",
        "description": "시청 유지와 반복 시청 가능성을 우선합니다.",
    },
    "share_growth": {
        "label": "공유율 강화",
        "description": "공유할 이유와 실천 가능성을 우선합니다.",
    },
    "balanced": {
        "label": "균형 성장",
        "description": "채널의 여러 성과 지표를 균형 있게 봅니다.",
    },
}


def home_button_rows():
    return [
        [button("단계별 검수 제작", "start_content:review"), button("자동 제작", "start_content:auto")],
        [button("목표 기반 자동 기획", "start_goal"), button("트렌드에서 시작", "start_content:trend")],
        [button("현재 작업", "show_status"), button("제작 설정", "open_settings"), button("⌂ 홈 새로고침", "show_home")],
    ]


def home_screen_text(job=None, notice=None):
    job = job or {}
    lines = [
        "*Brain50 콘텐츠 제작 홈*",
        "① 제작 방식이나 목표 기반 자동 기획을 선택하세요.",
        "② 주제를 입력하고 실행 전 확인하세요. 목표 기반 기획은 목표와 씨드를 선택합니다.",
        "③ 필요한 단계만 검수한 뒤 원하는 지점에서 끝까지 자동 처리할 수 있습니다.",
    ]
    if notice:
        lines.extend(("", notice))
    goal_draft = job.get("goal_draft") or {}
    draft = job.get("start_draft") or {}
    if goal_draft:
        objective = GOAL_OBJECTIVES.get(goal_draft.get("objective"), {})
        state = "씨드 입력 대기" if goal_draft.get("awaiting_seed") else "선택 진행 중"
        lines.extend(("", f"목표 기획 준비: {objective.get('label', '목표 선택')} · {state}"))
    elif draft:
        mode = START_MODES.get(draft.get("mode"), {})
        state = "실행 확인 대기" if draft.get("topic") else "주제 입력 대기"
        lines.extend(("", f"시작 준비: {mode.get('label', draft.get('mode'))} · {state}"))
    elif job.get("job_id") or job.get("stage"):
        lines.extend(("", workflow_status_text(job)))
    else:
        lines.extend(("", "현재 진행 중인 작업이 없습니다."))
    return "\n".join(lines)


def send_home_screen(chat_id, notice=None, top_level=False):
    job = _STATE.get("chats", {}).get(str(chat_id), {})
    text = home_screen_text(job, notice)
    rows = home_button_rows()
    if not top_level:
        return send_action_message(chat_id, text, rows)
    return _slack_client().chat_postMessage(
        channel=str(chat_id),
        text=text[:MAX_BLOCK_TEXT],
        blocks=_blocks(text, rows),
    )


def publish_home(user_id, client=None):
    channel_id = ALLOWED_CHANNEL_ID
    job = _STATE.get("chats", {}).get(str(channel_id), {}) if channel_id else {}
    text = home_screen_text(job, None if channel_id else "채널 작업을 시작하려면 SLACK_CHANNEL_ID를 설정하세요.")
    client = client or _slack_client()
    return client.views_publish(user_id=user_id, view={"type": "home", "blocks": _blocks(text, home_button_rows())})


def prompt_start_topic(chat_id, job):
    draft = job.get("start_draft") or {}
    mode = START_MODES.get(draft.get("mode"))
    if not mode:
        send_home_screen(chat_id, "시작 정보를 찾지 못했습니다. 제작 방식을 다시 선택하세요.")
        return
    send_action_message(
        chat_id,
        f"*새 콘텐츠 · 1/2 주제 입력*\n선택: {mode['label']}\n\n제작할 주제를 다음 메시지로 입력하세요. 아직 실행되지 않습니다.",
        [[button("← 홈으로", "start_cancel"), button("시작 취소", "start_cancel")]],
    )


def begin_start_flow(chat_id, job, mode, topic=None):
    if mode not in START_MODES:
        raise ValueError(f"알 수 없는 제작 방식입니다: {mode}")
    job.pop("goal_draft", None)
    job["start_draft"] = {"mode": mode}
    if str(topic or "").strip():
        capture_start_topic(chat_id, job, topic)
    else:
        prompt_start_topic(chat_id, job)


def capture_start_topic(chat_id, job, topic):
    draft = job.get("start_draft") or {}
    mode = START_MODES.get(draft.get("mode"))
    topic = str(topic or "").strip()
    if not mode:
        send_home_screen(chat_id, "제작 방식을 다시 선택하세요.")
        return
    if not topic:
        prompt_start_topic(chat_id, job)
        return
    draft["topic"] = topic
    job["start_draft"] = draft
    send_action_message(
        chat_id,
        "\n".join([
            "*새 콘텐츠 · 2/2 실행 확인*",
            f"방식: {mode['label']}",
            f"주제: {topic}",
            "",
            "아직 실행되지 않았습니다. 이 내용으로 시작할까요?",
            "기존 작업이 있다면 상태는 새 작업으로 교체되지만 기존 산출물은 보존됩니다.",
        ]),
        [[button("실행하기", f"start_confirm:{draft['mode']}"), button("주제 다시 입력", "start_reenter_topic")],
         [button("← 홈으로", "start_cancel"), button("시작 취소", "start_cancel")]],
    )


def confirm_start_flow(state, chat_id, job, mode):
    draft = job.get("start_draft") or {}
    topic = str(draft.get("topic") or "").strip()
    if draft.get("mode") != mode or mode not in START_MODES or not topic:
        send_home_screen(chat_id, "시작 정보가 완전하지 않습니다. 제작 방식을 다시 선택하세요.")
        return
    job.pop("start_draft", None)
    command = START_MODES[mode]["command"] + " " + topic
    if mode == "auto":
        target = lambda: handle_run_auto(chat_id, job, command)
        label = "자동 제작"
    elif mode == "trend":
        target = lambda: handle_run(chat_id, job, command, trend=True)
        label = "트렌드 조회"
    else:
        target = lambda: handle_run(chat_id, job, command, trend=False)
        label = "스크립트 생성"
    start_background_task(state, chat_id, job, label, target)


def goal_objective_rows():
    return [
        [button("구독자 증가", "goal:objective:subscriber_growth"), button("조회수·도달", "goal:objective:reach")],
        [button("평균 시청률", "goal:objective:retention"), button("공유율 강화", "goal:objective:share_growth")],
        [button("균형 성장", "goal:objective:balanced")],
        [button("← 홈으로", "goal:cancel")],
    ]


def send_goal_objective_menu(chat_id, job, notice=None):
    text = "*목표 기반 자동 기획 · 1/3 목표 선택*\n달성하고 싶은 핵심 성과를 선택하세요. 아직 실행되지 않습니다."
    if notice:
        text = notice + "\n\n" + text
    send_action_message(chat_id, text, goal_objective_rows())


def begin_goal_flow(chat_id, job):
    job.pop("start_draft", None)
    job["goal_draft"] = {}
    send_goal_objective_menu(chat_id, job)


def send_goal_seed_menu(chat_id, job):
    draft = job.get("goal_draft") or {}
    objective = GOAL_OBJECTIVES.get(draft.get("objective"))
    if not objective:
        send_goal_objective_menu(chat_id, job, "목표를 다시 선택하세요.")
        return
    send_action_message(
        chat_id,
        "\n".join([
            "*목표 기반 자동 기획 · 2/3 씨드 선택*",
            f"목표: {objective['label']}",
            f"기준: {objective['description']}",
            "",
            "채널 데이터만으로 주제를 자동 선정하거나, 아이디어 범위를 좁힐 씨드를 입력할 수 있습니다.",
        ]),
        [[button("씨드 없이 자동 선정", "goal:seed:none"), button("씨드 직접 입력", "goal:seed:input")],
         [button("← 목표 다시 선택", "goal:back:objectives"), button("시작 취소", "goal:cancel")]],
    )


def select_goal_objective(chat_id, job, objective):
    if objective not in GOAL_OBJECTIVES:
        raise ValueError(f"알 수 없는 목표입니다: {objective}")
    job["goal_draft"] = {"objective": objective}
    send_goal_seed_menu(chat_id, job)


def prompt_goal_seed(chat_id, job):
    draft = job.get("goal_draft") or {}
    objective = GOAL_OBJECTIVES.get(draft.get("objective"))
    if not objective:
        send_goal_objective_menu(chat_id, job, "목표를 다시 선택하세요.")
        return
    draft.pop("seed", None)
    draft["awaiting_seed"] = True
    job["goal_draft"] = draft
    send_action_message(
        chat_id,
        f"*목표 기반 자동 기획 · 2/3 씨드 입력*\n목표: {objective['label']}\n\n예: 수면, 기억력, 혈당\n아이디어 범위를 좁힐 단어나 문장을 다음 메시지로 입력하세요.",
        [[button("씨드 없이 진행", "goal:seed:none"), button("← 이전", "goal:back:seed")],
         [button("시작 취소", "goal:cancel")]],
    )


def send_goal_confirmation(chat_id, job):
    draft = job.get("goal_draft") or {}
    objective = GOAL_OBJECTIVES.get(draft.get("objective"))
    if not objective or "seed" not in draft:
        send_goal_objective_menu(chat_id, job, "목표 기획 정보가 완전하지 않습니다. 다시 선택하세요.")
        return
    seed = str(draft.get("seed") or "").strip()
    send_action_message(
        chat_id,
        "\n".join([
            "*목표 기반 자동 기획 · 3/3 실행 확인*",
            f"목표: {objective['label']}",
            f"씨드: {seed or '없음 — 채널 데이터 기반 자동 선정'}",
            "",
            "실행하면 채널 성과를 분석해 주제를 선정하고 스크립트를 생성합니다.",
            "스크립트 생성 후에는 기존 승인형 검수 흐름에서 멈춥니다.",
            "기존 작업 상태는 이 버튼을 누르는 시점에 교체되며 기존 산출물은 보존됩니다.",
        ]),
        [[button("실행하기", "goal:confirm"), button("씨드 변경", "goal:back:seed")],
         [button("목표 변경", "goal:back:objectives"), button("시작 취소", "goal:cancel")]],
    )


def select_goal_seed_mode(chat_id, job, mode):
    draft = job.get("goal_draft") or {}
    if draft.get("objective") not in GOAL_OBJECTIVES:
        send_goal_objective_menu(chat_id, job, "목표를 먼저 선택하세요.")
        return
    if mode == "input":
        prompt_goal_seed(chat_id, job)
        return
    if mode != "none":
        raise ValueError(f"알 수 없는 씨드 방식입니다: {mode}")
    draft["seed"] = ""
    draft.pop("awaiting_seed", None)
    job["goal_draft"] = draft
    send_goal_confirmation(chat_id, job)


def capture_goal_seed(chat_id, job, seed):
    draft = job.get("goal_draft") or {}
    seed = str(seed or "").strip()
    if not draft.get("awaiting_seed"):
        return False
    if not seed:
        prompt_goal_seed(chat_id, job)
        return True
    draft["seed"] = seed
    draft.pop("awaiting_seed", None)
    job["goal_draft"] = draft
    send_goal_confirmation(chat_id, job)
    return True


def confirm_goal_flow(state, chat_id, job):
    draft = job.get("goal_draft") or {}
    objective = draft.get("objective")
    if objective not in GOAL_OBJECTIVES or "seed" not in draft:
        send_goal_objective_menu(chat_id, job, "목표 기획 정보가 완전하지 않습니다. 다시 선택하세요.")
        return
    seed = str(draft.get("seed") or "").strip()
    job.pop("goal_draft", None)
    command = f"/run_goal {objective}" + (f" {seed}" if seed else "")
    start_background_task(
        state, chat_id, job, "목표 기반 기획",
        lambda: handle_run_goal(chat_id, job, command),
    )


def workflow_status_text(job, detail=None):
    stage = job.get("stage")
    topic = str(job.get("topic") or "").strip()
    job_id = job.get("job_id")
    display_stage = job.get("auto_from_stage") if stage == "running_after_review" else stage
    if display_stage in WORKFLOW_STAGES:
        current = WORKFLOW_STAGES.index(display_stage)
        markers = ["✅" if i < current else "🔎" if i == current else "▫️" for i in range(len(WORKFLOW_STAGES))]
        progress = " ".join(markers)
        title = f"*콘텐츠 제작 · {current + 1}/{len(WORKFLOW_STAGES)} {STAGE_LABELS[display_stage]}*"
    elif stage == "done":
        progress = " ".join(["✅"] * len(WORKFLOW_STAGES))
        title = "*콘텐츠 제작 완료*"
    else:
        progress = ""
        title = f"*콘텐츠 제작 · {STAGE_LABELS.get(stage, stage or '대기')}*"
    lines = [title]
    if progress:
        lines.append(progress)
    if topic:
        lines.append(f"주제: {topic[:180]}")
    if job_id:
        lines.append(f"작업 ID: `{job_id}`")
    if stage == "running_after_review":
        lines.append(f"자동 처리: {job.get('auto_progress', '다음 단계 준비 중')}")
    elif job.get("busy"):
        lines.append(f"처리 중: {job['busy']}")
    if job.get("last_error"):
        lines.append(f"최근 오류: {str(job['last_error'])[:300]}")
    if detail:
        lines.extend(("", detail))
    return "\n".join(lines)


def approval_buttons(stage):
    rows = []
    if stage == "await_script_approval":
        rows.append([button("본문 수정", f"edit_body:{stage}"), button("제목 수정", f"edit_title_menu:{stage}")])
    elif stage == "await_tts_approval":
        rows.append([button("음성 재생성", f"rerun:{stage}:tts")])
    elif stage == "await_caption_approval":
        rows.append([button("자막 수정", f"edit:{stage}"), button("자막 재생성", f"rerun:{stage}:caption")])
    elif stage == "await_broll_approval":
        rows.append([button("B-roll 재생성", f"rerun:{stage}:broll")])
    elif stage == "await_render_approval":
        rows.append([button("렌더 설정 수정", f"back:{stage}:await_render_config")])
    elif stage == "await_upload_meta_approval":
        rows.append([button("업로드 정보 수정", f"edit:{stage}")])

    navigation = []
    previous = previous_stage_button(stage)
    if previous:
        navigation.append(previous)
    next_label = "업로드 ▶" if stage == "await_upload_meta_approval" else "다음 단계 ▶"
    navigation.append(button(next_label, f"approve:{stage}"))
    rows.append(navigation)
    rows.append([
        button("🚀 여기서부터 끝까지", f"auto_finish:{stage}"),
        button("↻ 상태", "show_status"),
        button("⌂ 홈", "show_home"),
        button("전체 취소", "cancel_all"),
    ])
    return rows


def previous_stage_button(stage):
    targets = {
        "await_tts_approval": "await_script_approval",
        "await_caption_approval": "await_tts_approval",
        "await_broll_approval": "await_caption_approval",
        "await_render_config": "await_broll_approval",
        "await_render_approval": "await_render_config",
        "await_upload_meta_approval": "await_render_approval",
    }
    target = targets.get(stage)
    if not target:
        return None
    return button("← 이전 단계", f"back:{stage}:{target}")


def send_approval_prompt(chat_id, stage, text):
    job = _STATE.get("chats", {}).get(str(chat_id), {"stage": stage})
    return send_action_message(chat_id, workflow_status_text(job, text), approval_buttons(stage))


def load_state():
    if not STATE_PATH.exists():
        return {"offset": 0, "chats": {}}
    return json.loads(STATE_PATH.read_text(encoding="utf-8"))


def save_state(state):
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(state, ensure_ascii=False, indent=2)
    tmp_path = STATE_PATH.with_suffix(STATE_PATH.suffix + ".tmp")
    with STATE_LOCK:
        tmp_path.write_text(payload, encoding="utf-8")
        os.replace(tmp_path, STATE_PATH)


def clear_stale_busy_flags(state):
    """Clear in-flight markers from a previous bot process.

    Long-running stages run in background threads. Those threads do not survive
    a systemd restart, but the persisted state file can still contain "busy".
    If we keep that marker, the freshly started bot blocks every command as if
    work were still running.
    """
    cleared = []
    for chat_id, job in state.get("chats", {}).items():
        if isinstance(job, dict) and job.pop("busy", None):
            cleared.append(chat_id)
    return cleared


def chat_state(state, chat_id):
    chats = state.setdefault("chats", {})
    return chats.setdefault(str(chat_id), {})


def busy_message(job):
    label = job.get("busy") or "작업"
    return f"현재 {label} 진행 중입니다. `상태` 또는 `전체 취소` 버튼은 언제든 사용할 수 있습니다."


def is_busy(job):
    return bool(job.get("busy"))


def _mark_workflow_cancelled(job):
    job["stage"] = "cancelled"
    job["cancelled_at"] = datetime.now().isoformat(timespec="seconds")
    for key in ("cancel_requested", "auto_from_stage", "auto_progress", "edit_target", "edit_stage", "title_edit_field", "title_edit_stage"):
        job.pop(key, None)


def request_workflow_cancel(chat_id, job):
    job_id = job.get("job_id")
    if not job_id and not is_busy(job):
        send_message(chat_id, "취소할 작업이 없습니다.")
        return
    if is_busy(job):
        job["cancel_requested"] = True
        if job_id:
            CANCELLED_JOB_IDS.add(job_id)
        with ACTIVE_PROCESS_LOCK:
            process = ACTIVE_PROCESSES.get(job_id)
        if process and process.poll() is None:
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except (ProcessLookupError, PermissionError):
                process.terminate()
        send_message(chat_id, "취소 요청을 받았습니다. 실행 중인 명령을 중단하고 상태를 정리합니다.")
        return
    if job_id:
        CANCELLED_JOB_IDS.discard(job_id)
    _mark_workflow_cancelled(job)
    send_message(chat_id, workflow_status_text(job, "전체 작업을 취소했습니다. 기존 산출물은 보존됩니다."))


def start_background_task(state, chat_id, job, label, target):
    if is_busy(job):
        send_message(chat_id, busy_message(job))
        return
    job["busy"] = label
    save_state(state)
    send_message(chat_id, f"진행 중입니다: {label}")
    log_event("INFO", "slack_task_queued", channel=chat_id, job_id=job.get("job_id"), task=label, stage=job.get("stage"))

    def runner():
        outcome = "completed"
        log_event("INFO", "slack_task_started", channel=chat_id, job_id=job.get("job_id"), task=label, stage=job.get("stage"))
        try:
            target()
        except WorkflowCancelled:
            outcome = "cancelled"
            current = chat_state(state, chat_id)
            _mark_workflow_cancelled(current)
            send_message(chat_id, workflow_status_text(current, "작업을 중단했습니다. 이미 생성된 산출물은 보존됩니다."))
        except Exception as exc:
            outcome = "failed"
            chat_state(state, chat_id)["last_error"] = str(exc)
            _send_recovery_error(chat_id, "show_status", exc, label=f"{label} 작업")
            log_event("ERROR", "slack_task_failed", channel=chat_id, job_id=job.get("job_id"), task=label, error=exc)
        finally:
            current = chat_state(state, chat_id)
            current.pop("busy", None)
            save_state(state)
            log_event("INFO", "slack_task_finished", channel=chat_id, job_id=current.get("job_id"), task=label, result=outcome, stage=current.get("stage"))

    threading.Thread(target=runner, daemon=True).start()


def new_job_id(prefix="slack"):
    return f"{prefix}_{datetime.now().strftime('%Y%m%d_%H%M%S_%f')}_{uuid.uuid4().hex[:8]}"


def work_dir(job_id):
    return WORK_DIR_BASE / job_id


def output_file(job_id):
    return OUTPUT_DIR / f"output_{job_id}.mp4"


def pubmed_status_path(job_id):
    return work_dir(job_id) / "pubmed_status.json"


def frame_header_path(job_id):
    return work_dir(job_id) / "frame_header.json"


def video_meta_path(job_id):
    return work_dir(job_id) / "video_meta.json"


def load_frame_header(job_id):
    header = {"title": "", "subtitle": ""}
    for path in (frame_header_path(job_id), video_meta_path(job_id)):
        if not path.exists():
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if path.name == "video_meta.json":
            data = data.get("frame_header") if isinstance(data, dict) else {}
        if not isinstance(data, dict):
            continue
        if not header["title"]:
            header["title"] = str(data.get("title") or "").strip()
        if not header["subtitle"]:
            header["subtitle"] = str(data.get("subtitle") or "").strip()
    return header


def save_frame_header(job_id, header):
    normalized = {
        "title": str(header.get("title") or "").strip(),
        "subtitle": str(header.get("subtitle") or "").strip(),
    }
    header_path = frame_header_path(job_id)
    header_path.parent.mkdir(parents=True, exist_ok=True)
    header_path.write_text(json.dumps(normalized, ensure_ascii=False, indent=2), encoding="utf-8")

    meta_path = video_meta_path(job_id)
    if meta_path.exists():
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except Exception:
            meta = None
        if isinstance(meta, dict):
            meta["frame_header"] = normalized
            meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    return normalized


def sync_frame_header_to_job(job, header):
    title = str(header.get("title") or "").strip()
    subtitle = str(header.get("subtitle") or "").strip()
    if title:
        job["frame_top_title"] = title
    if subtitle:
        job["frame_top_subtitle"] = subtitle


def read_pubmed_status(job_id):
    path = pubmed_status_path(job_id)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def pubmed_retry_message(status):
    if not status:
        return "PubMed 검색 결과를 확인하지 못했습니다."
    return "\n".join([
        "PubMed에서 관련 초록을 찾지 못했습니다.",
        f"주제: {status.get('topic', '')}",
        f"원인 추정: {status.get('message', '')}",
        "",
        "다시 시도: /retry 새 주제",
        "근거 부족을 감수하고 진행: /proceed",
    ])


def run_command(args, job_id, topic=None, extra_env=None):
    if job_id in CANCELLED_JOB_IDS:
        CANCELLED_JOB_IDS.discard(job_id)
        raise WorkflowCancelled("사용자가 작업을 취소했습니다.")
    env = os.environ.copy()
    env["JOB_ID"] = job_id
    if topic:
        env["TOPIC"] = topic
    if extra_env:
        env.update(extra_env)
    command_name = Path(args[0]).name
    log_event("INFO", "slack_command_started", job_id=job_id, command=command_name)
    process = subprocess.Popen(
        args,
        cwd=BASE_DIR,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    with ACTIVE_PROCESS_LOCK:
        ACTIVE_PROCESSES[job_id] = process
    try:
        stdout, stderr = process.communicate()
    finally:
        with ACTIVE_PROCESS_LOCK:
            if ACTIVE_PROCESSES.get(job_id) is process:
                ACTIVE_PROCESSES.pop(job_id, None)
    log_dir = work_dir(job_id)
    log_dir.mkdir(parents=True, exist_ok=True)
    log_name = f"slack_{Path(args[0]).name}_{int(time.time())}.log"
    (log_dir / log_name).write_text((stdout or "") + "\n" + (stderr or ""), encoding="utf-8")
    if job_id in CANCELLED_JOB_IDS:
        CANCELLED_JOB_IDS.discard(job_id)
        log_event("INFO", "slack_command_cancelled", job_id=job_id, command=command_name, log=log_dir / log_name)
        raise WorkflowCancelled("사용자가 작업을 취소했습니다.")
    if process.returncode != 0:
        tail = (stderr or stdout or "")[-1600:]
        hint = ""
        if "ReadTimeout" in tail and "api.anthropic.com" in tail:
            hint = "\n\n진단: Claude API 응답이 설정된 시간 안에 끝나지 않았습니다. 주제 문제가 아니라 네트워크 지연이나 응답 생성 지연일 가능성이 큽니다. 잠시 후 같은 /pick 번호를 다시 실행하거나 /retry 새 주제로 재시도하세요. 반복되면 CLAUDE_TIMEOUT 값을 더 크게 설정하세요."
        elif "api.anthropic.com" in tail:
            hint = "\n\n진단: Claude API 호출 단계에서 실패했습니다. 로그 파일의 HTTP 상태와 메시지를 확인하세요."
        log_event("ERROR", "slack_command_failed", job_id=job_id, command=command_name, return_code=process.returncode, log=log_dir / log_name)
        raise RuntimeError(f"명령 실패: {' '.join(shlex.quote(a) for a in args)}\n로그: {log_dir / log_name}{hint}\n\n{tail}")
    log_event("INFO", "slack_command_finished", job_id=job_id, command=command_name, return_code=process.returncode, log=log_dir / log_name)
    return stdout


def preview_file(path, limit=MAX_TEXT_PREVIEW):
    path = Path(path)
    if not path.exists():
        return "파일이 없습니다."
    text = path.read_text(encoding="utf-8", errors="replace")
    if len(text) > limit:
        return text[:limit] + "\n...(생략)"
    return text


def send_pubmed_notice(chat_id, job_id):
    status = read_pubmed_status(job_id)
    if not status or status.get("status") == "ok":
        return
    send_message(chat_id, "\n".join([
        "PubMed 직접 검색 결과 없이 생성했습니다.",
        f"주제: {status.get('topic', '')}",
        f"원인 추정: {status.get('message', '')}",
        "Claude는 일반 의학 지식 기반으로 조심스럽게 작성했습니다.",
        "주제가 마음에 들지 않으면 /retry 새 주제로 다시 생성할 수 있습니다.",
    ]))
    send_file_or_path(chat_id, pubmed_status_path(job_id), "pubmed_status.json")




def send_script(chat_id, job_id):
    send_pubmed_notice(chat_id, job_id)
    path = work_dir(job_id) / "script.txt"
    send_approval_prompt(
        chat_id,
        "await_script_approval",
        f"스크립트 생성 완료. 확인 후 승인하거나 수정하세요.\n\n{preview_file(path)}",
    )
    if path.exists():
        send_file_or_path(chat_id, path, "script.txt")


def send_tts(chat_id, job_id):
    path = work_dir(job_id) / "voice.wav"
    if path.exists():
        send_file_or_path(chat_id, path, "TTS 음성입니다.")
        send_approval_prompt(chat_id, "await_tts_approval", "TTS를 확인한 뒤 승인하거나 재생성하세요.")
    else:
        send_message(chat_id, f"voice.wav를 찾지 못했습니다: {path}")


def send_caption(chat_id, job_id):
    path = work_dir(job_id) / "subs.srt"
    send_approval_prompt(
        chat_id,
        "await_caption_approval",
        f"자막 생성 완료. 확인 후 승인하거나 수정하세요.\n\n{preview_file(path)}",
    )
    if path.exists():
        send_file_or_path(chat_id, path, "subs.srt")


def send_broll(chat_id, job_id):
    path = work_dir(job_id) / "broll.mp4"
    if path.exists():
        send_file_or_path(chat_id, path, "B-roll 영상입니다.", as_video=True)
        send_approval_prompt(chat_id, "await_broll_approval", "B-roll을 확인한 뒤 승인하거나 재생성하세요.")
    else:
        send_message(chat_id, f"broll.mp4를 찾지 못했습니다: {path}")


def send_render_ready(chat_id, job):
    font_size = job.get("caption_font_size")
    margin_v = job.get("caption_margin_v")
    margin_h = job.get("caption_margin_h")
    caption_style = job.get("caption_style")
    offset_x = job.get("caption_offset_x")
    offset_y = job.get("caption_offset_y")
    frame_mode = job.get("frame_mode")
    broll_fit = job.get("broll_fit_mode")
    msg = (
        "렌더 설정 확인\n"
        "현재: font=" + display_config_value(font_size) + ", margin_v=" + display_config_value(margin_v) +
        ", margin_h=" + display_config_value(margin_h) + ", style=" + display_config_value(caption_style) +
        ", offset_x=" + display_config_value(offset_x) + ", offset_y=" + display_config_value(offset_y) +
        ", frame=" + display_config_value(frame_mode) + ", broll_fit=" + display_config_value(broll_fit) + "\n"
        "조정: /render style=center-yellow frame=framed broll_fit=cover offset_y=-120\n"
        "또는 /set 으로 저장 후 재렌더"
    )
    send_action_message(
        chat_id,
        workflow_status_text(job, msg),
        [
            [button("기본 스타일",  "render:await_render_config:62:60:default"),
             button("중앙 노랑",  "render:await_render_config:72:0:center-yellow")],
            [button("← 이전 단계", "back:await_render_config:await_broll_approval"),
             button("현재 설정으로 렌더 ▶", "approve:await_render_config")],
            [button("🚀 여기서부터 끝까지", "auto_finish:await_render_config"),
             button("↻ 상태", "show_status"), button("⌂ 홈", "show_home"), button("전체 취소", "cancel_all")],
        ],
    )

def send_rendered_video(chat_id, job_id):
    path = output_file(job_id)
    if path.exists():
        send_file_or_path(chat_id, path, "최종 합성 영상입니다.", as_video=True)
        send_approval_prompt(chat_id, "await_render_approval", "최종 영상을 확인한 뒤 승인하거나 렌더 설정을 다시 조정하세요.")
    else:
        send_message(chat_id, f"렌더 결과를 찾지 못했습니다: {path}")


def send_upload_meta(chat_id, job_id):
    meta_path = work_dir(job_id) / "video_meta.json"
    if not meta_path.exists():
        send_message(chat_id, f"video_meta.json을 찾지 못했습니다: {meta_path}")
        return
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    text = (
        "YouTube 업로드 메타데이터 확인 단계입니다.\n"
        f"제목: {meta.get('title', '')}\n\n"
        f"요약: {meta.get('summary', '')}\n\n"
        f"해시태그: {meta.get('hashtags', '')}\n\n"
        f"설명:\n{meta.get('description', '')}\n\n"
        "승인하면 비공개 영상으로 업로드합니다."
    )
    send_approval_prompt(chat_id, "await_upload_meta_approval", text[:MAX_TEXT_PREVIEW])
    send_file_or_path(chat_id, meta_path, "video_meta.json")

def parse_key_values(text):
    values = {}
    for token in text.split()[1:]:
        if "=" not in token:
            continue
        key, value = token.split("=", 1)
        values[key.strip().lower()] = value.strip()
    return values


def positive_int(value, name):
    text = str(value).strip()
    if not text.isdigit() or int(text) <= 0:
        raise ValueError(f"{name}은 양의 정수로 입력하세요: {value}")
    return text


def signed_int(value, name):
    text = str(value).strip()
    if text.startswith("-"):
        digits = text[1:]
    else:
        digits = text
    if not digits.isdigit():
        raise ValueError(f"{name}은 정수로 입력하세요: {value}")
    return text


def safe_caption_style(value):
    value = str(value).strip()
    if not value or any(ch not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-" for ch in value):
        raise ValueError(f"style은 영문/숫자/_/- 만 입력하세요: {value}")
    return value


def safe_choice(value, name, choices):
    value = str(value).strip()
    if value not in choices:
        raise ValueError(f"{name}은 {', '.join(choices)} 중 하나여야 합니다: {value}")
    return value


def positive_number(value, name):
    text = str(value).strip()
    try:
        number = float(text)
    except ValueError:
        raise ValueError(f"{name}은 양수로 입력하세요: {value}")
    if number <= 0:
        raise ValueError(f"{name}은 양수로 입력하세요: {value}")
    return text


def display_config_value(value):
    return str(value) if value not in (None, "") else "config"


def display_effective_model(job, job_key, value):
    source = "override" if job_key in job else "env/default"
    return f"{value} ({source})"


def env_value(key, default="-"):
    value = os.environ.get(key)
    return default if value in (None, "") else value


# ── 설정 키: /set 메뉴 또는 key=value로 저장, run_auto/run 실행 시 자동 적용
CONFIG_CATEGORIES = (
    ("models", "AI 모델", "스크립트·조사·전략·검색 모델"),
    ("research", "조사 / 검색", "웹 검색과 사례 조사"),
    ("channel", "채널 성과", "YouTube 실데이터 동기화와 판단 강도"),
    ("audio", "음성 / 영상 길이", "TTS 목소리·속도·목표 길이"),
    ("caption", "자막", "글자·여백·스타일·위치"),
    ("frame", "프레임 / B-roll", "화면 프레임·프리셋·채널명"),
    ("system", "시스템 (읽기 전용)", "실행 환경과 API 제한값"),
)

MODEL_CHOICES = (
    ("Haiku 4.5", "claude-haiku-4-5-20251001"),
    ("Sonnet 4.6", "claude-sonnet-4-6"),
    ("Sonnet 4.5", "claude-sonnet-4-5-20250929"),
    ("Opus 4.8", "claude-opus-4-8"),
)

# callback_data의 길이 제한을 피하기 위해 짧은 id를 쓰고 실제 값은 여기서 찾는다.
CONFIG_SETTINGS = {
    "script_model": {"category": "models", "label": "스크립트 모델", "job_key": "claude_script_model", "env": "CLAUDE_SCRIPT_MODEL", "default": lambda job: env_value("CLAUDE_MODEL", "claude-sonnet-4-6"), "kind": "model", "choices": MODEL_CHOICES},
    "research_model": {"category": "models", "label": "조사 모델", "job_key": "claude_research_model", "env": "CLAUDE_RESEARCH_MODEL", "default": lambda job: _effective_setting_value(job, "script_model")[0], "kind": "model", "choices": MODEL_CHOICES},
    "strategy_model": {"category": "models", "label": "전략 모델", "job_key": "claude_strategy_model", "env": "CLAUDE_STRATEGY_MODEL", "default": "claude-haiku-4-5-20251001", "kind": "model", "choices": MODEL_CHOICES},
    "query_model": {"category": "models", "label": "검색어 모델", "job_key": "claude_query_model", "env": "CLAUDE_QUERY_MODEL", "default": lambda job: _effective_setting_value(job, "strategy_model")[0], "kind": "model", "choices": MODEL_CHOICES},
    "web": {"category": "research", "label": "웹 검색", "job_key": "web_research", "env": "ENABLE_WEB_RESEARCH", "default": DEFAULT_WEB_RESEARCH, "kind": "bool", "choices": (("켜기", True), ("끄기", False))},
    "case": {"category": "research", "label": "사례 조사", "job_key": "case_research", "env": "ENABLE_CASE_RESEARCH", "default": True, "kind": "bool", "choices": (("켜기", True), ("끄기", False))},
    "feedback_policy": {"category": "channel", "label": "판단 강도", "job_key": "youtube_feedback_strictness", "env": "YOUTUBE_FEEDBACK_STRICTNESS", "default": "balanced", "kind": "choice", "choices": (("느슨함", "loose"), ("중간", "balanced"), ("엄격함", "strict"))},
    "feedback_sync": {"category": "channel", "label": "생성 전 동기화", "job_key": "youtube_feedback_auto_sync", "env": "YOUTUBE_FEEDBACK_AUTO_SYNC", "default": True, "kind": "bool", "choices": (("켜기", True), ("끄기", False))},
    "voice": {"category": "audio", "label": "TTS 목소리", "job_key": "tts_voice", "env": "TTS_VOICE", "default": "M2", "kind": "voice", "choices": tuple((voice, voice) for voice in ("F1", "F2", "M1", "M2"))},
    "pace": {"category": "audio", "label": "말하기 속도", "job_key": "speech_pace", "env": "SPEECH_PACE", "default": "legacy", "kind": "pace", "choices": (("느리게", "slow"), ("보통", "normal"), ("빠르게", "fast"), ("매우 빠르게", "very_fast"))},
    "duration": {"category": "audio", "label": "목표 길이(초)", "job_key": "target_duration_sec", "env": "TARGET_DURATION_SEC", "default": "60", "kind": "positive_int"},
    "font_size": {"category": "caption", "label": "글자 크기", "job_key": "caption_font_size", "env": "CAPTION_FONT_SIZE", "default": DEFAULT_CAPTION_FONT_SIZE, "kind": "positive_int"},
    "margin_v": {"category": "caption", "label": "세로 여백", "job_key": "caption_margin_v", "env": "CAPTION_MARGIN_V", "default": DEFAULT_CAPTION_MARGIN_V, "kind": "positive_int"},
    "margin_h": {"category": "caption", "label": "가로 여백", "job_key": "caption_margin_h", "env": "CAPTION_MARGIN_H", "default": DEFAULT_CAPTION_MARGIN_H, "kind": "positive_int"},
    "style": {"category": "caption", "label": "자막 스타일", "job_key": "caption_style", "env": "CAPTION_STYLE", "default": DEFAULT_CAPTION_STYLE, "kind": "style", "choices": tuple((style, style) for style in ("default", "center-outline", "center-yellow", "center-white"))},
    "offset_x": {"category": "caption", "label": "가로 위치 보정", "job_key": "caption_offset_x", "env": "CAPTION_OFFSET_X", "default": "0", "kind": "signed_int"},
    "offset_y": {"category": "caption", "label": "세로 위치 보정", "job_key": "caption_offset_y", "env": "CAPTION_OFFSET_Y", "default": "0", "kind": "signed_int"},
    "frame": {"category": "frame", "label": "프레임 모드", "job_key": "frame_mode", "env": "FRAME_MODE", "default": "full", "kind": "choice", "choices": (("전체 화면", "full"), ("상하 프레임", "framed"))},
    "broll_fit": {"category": "frame", "label": "B-roll 맞춤", "job_key": "broll_fit_mode", "env": "BROLL_FIT_MODE", "default": "cover", "kind": "choice", "choices": (("채우기", "cover"), ("원본 유지", "contain"), ("블러 여백", "blur-contain"))},
    "top_preset": {"category": "frame", "label": "상단 프리셋", "job_key": "frame_top_preset", "env": "FRAME_TOP_PRESET", "default": "default", "kind": "style", "choices": (("default", "default"), ("brain50", "brain50"))},
    "bottom_preset": {"category": "frame", "label": "하단 프리셋", "job_key": "frame_bottom_preset", "env": "FRAME_BOTTOM_PRESET", "default": "default", "kind": "style", "choices": (("default", "default"), ("minimal", "minimal"))},
    "top_pct": {"category": "frame", "label": "상단 높이(%)", "job_key": "frame_top_pct", "env": "FRAME_TOP_PCT", "default": "preset", "kind": "positive_number"},
    "bottom_pct": {"category": "frame", "label": "하단 높이(%)", "job_key": "frame_bottom_pct", "env": "FRAME_BOTTOM_PCT", "default": "preset", "kind": "positive_number"},
    "channel": {"category": "frame", "label": "하단 채널명", "job_key": "frame_bottom_channel_name", "env": "FRAME_BOTTOM_CHANNEL_NAME", "default": "브레인피프티", "kind": "text"},
    "header": {"category": "frame", "label": "상단 제목", "job_key": "frame_header_text", "env": "FRAME_HEADER_TEXT", "default": "자동 생성", "kind": "text"},
}

CONFIG_INPUT_ALIASES = {
    "caption_style": "style",
    "frame_mode": "frame",
    "broll_fit_mode": "broll_fit",
    "speech_pace": "pace",
    "target_duration_sec": "duration",
    "web_research": "web",
    "case_research": "case",
    "feedback_strictness": "feedback_policy",
    "youtube_feedback_strictness": "feedback_policy",
    "youtube_feedback_auto_sync": "feedback_sync",
}

_PRESERVED_KEYS = {setting["job_key"] for setting in CONFIG_SETTINGS.values()}

SYSTEM_CONFIG_FIELDS = (
    ("ENV_NAME", "-"),
    ("CLAUDE_MODEL", "claude-sonnet-4-6"),
    ("MAX_TOKENS", "2600"),
    ("CLAUDE_HTTP_RETRIES", "1"),
    ("PUBMED_RETMAX", "3"),
    ("PUBMED_ABSTRACT_CHAR_LIMIT", "7000"),
    ("LOG_LEVEL", "-"),
)


def _env_bool(value):
    return str(value).strip().lower() not in ("off", "0", "false", "no")


def _effective_setting_value(job, setting_id):
    setting = CONFIG_SETTINGS[setting_id]
    job_key = setting["job_key"]
    if job_key in job:
        return job[job_key], "작업 override"
    env_name = setting.get("env")
    env_value_raw = os.environ.get(env_name) if env_name else None
    if env_value_raw not in (None, ""):
        value = _env_bool(env_value_raw) if setting["kind"] == "bool" else env_value_raw
        return value, "환경 설정"
    default = setting.get("default", "-")
    if callable(default):
        default = default(job)
    return default, "기본값"


def _display_setting_value(value):
    if isinstance(value, bool):
        return "켜짐" if value else "꺼짐"
    return str(value) if value not in (None, "") else "(비어 있음)"


def _validate_setting_value(setting_id, value):
    setting = CONFIG_SETTINGS[setting_id]
    kind = setting["kind"]
    if kind == "positive_int":
        return positive_int(value, setting_id)
    if kind == "signed_int":
        return signed_int(value, setting_id)
    if kind == "positive_number":
        return positive_number(value, setting_id)
    if kind == "pace":
        return speech_pace_profile(value)[0]
    if kind == "model":
        return resolve_model_alias(value)
    if kind == "style":
        return safe_caption_style(value)
    if kind == "voice":
        text = str(value).strip().upper()
        allowed = tuple(choice_value for _, choice_value in setting.get("choices", ()))
        if text not in allowed:
            raise ValueError(f"voice은 {', '.join(allowed)} 중 하나여야 합니다: {value}")
        return text
    if kind == "bool":
        if isinstance(value, bool):
            return value
        lowered = str(value).strip().lower()
        if lowered not in ("on", "off", "true", "false", "1", "0", "yes", "no"):
            raise ValueError(f"{setting_id}은 on 또는 off로 입력하세요: {value}")
        return lowered in ("on", "true", "1", "yes")
    if kind == "choice":
        allowed = tuple(choice_value for _, choice_value in setting.get("choices", ()))
        return safe_choice(value, setting_id, allowed)
    text = str(value).strip()
    if not text:
        raise ValueError(f"{setting_id}은 빈 값으로 설정할 수 없습니다.")
    return text


def _set_config_value(job, setting_id, value):
    setting = CONFIG_SETTINGS[setting_id]
    validated = _validate_setting_value(setting_id, value)
    job[setting["job_key"]] = validated
    return validated


def _category_label(category_id):
    for current_id, label, description in CONFIG_CATEGORIES:
        if current_id == category_id:
            return label, description
    return category_id, ""


def send_config_menu(chat_id, job, notice=None):
    job.pop("config_edit_key", None)
    text = "설정 상자를 선택하세요. 현재값을 확인한 뒤 버튼이나 짧은 입력으로 변경할 수 있습니다."
    if notice:
        text = notice + "\n\n" + text
    rows = []
    for category_id, label, _ in CONFIG_CATEGORIES:
        rows.append([button(label, f"cfg:cat:{category_id}")])
    rows.append([button("전체 설정 보기", "cfg:all"), button("전체 override 초기화", "cfg:reset")])
    rows.append([button("← 콘텐츠 홈", "show_home")])
    return send_action_message(chat_id, text, rows)


def send_config_category(chat_id, job, category_id, notice=None):
    job.pop("config_edit_key", None)
    label, description = _category_label(category_id)
    if category_id == "system":
        lines = [f"{label}\n{description}"]
        lines.extend(f"{name}={env_value(name, default)}" for name, default in SYSTEM_CONFIG_FIELDS)
        if notice:
            lines.insert(0, notice)
        return send_action_message(chat_id, "\n".join(lines), [[button("← 설정 상자", "cfg:root"), button("⌂ 콘텐츠 홈", "show_home")]])

    lines = [f"{label}\n{description}", ""]
    rows = []
    for setting_id, setting in CONFIG_SETTINGS.items():
        if setting["category"] != category_id:
            continue
        value, source = _effective_setting_value(job, setting_id)
        shown = _display_setting_value(value)
        lines.append(f"{setting['label']}: {shown} ({source})")
        rows.append([button(f"{setting['label']} · {shown}", f"cfg:item:{setting_id}")])
    if notice:
        lines.insert(0, notice)
    rows.append([button("← 설정 상자", "cfg:root"), button("⌂ 콘텐츠 홈", "show_home")])
    return send_action_message(chat_id, "\n".join(lines), rows)


def send_config_detail(chat_id, job, setting_id, notice=None):
    setting = CONFIG_SETTINGS[setting_id]
    job.pop("config_edit_key", None)
    value, source = _effective_setting_value(job, setting_id)
    lines = [
        setting["label"],
        f"현재값: {_display_setting_value(value)}",
        f"적용 출처: {source}",
    ]
    if notice:
        lines.insert(0, notice)
    rows = []
    choices = setting.get("choices", ())
    for index in range(0, len(choices), 2):
        row = []
        for choice_index in range(index, min(index + 2, len(choices))):
            choice_label, _ = choices[choice_index]
            row.append(button(choice_label, f"cfg:pick:{setting_id}:{choice_index}"))
        rows.append(row)
    if setting["kind"] not in ("bool", "choice", "pace", "voice") or not choices:
        prompt_label = "직접 입력" if choices else "수정"
        rows.append([button(prompt_label, f"cfg:edit:{setting_id}")])
    rows.append([
        button("현재값 유지", f"cfg:keep:{setting_id}"),
        button("기본값 사용", f"cfg:default:{setting_id}"),
    ])
    rows.append([button("← 이전 상자", f"cfg:cat:{setting['category']}"), button("설정 홈", "cfg:root")])
    return send_action_message(chat_id, "\n".join(lines), rows)


def begin_config_edit(chat_id, job, setting_id):
    setting = CONFIG_SETTINGS[setting_id]
    current, source = _effective_setting_value(job, setting_id)
    job.pop("title_edit_field", None)
    job.pop("title_edit_stage", None)
    job.pop("edit_target", None)
    job.pop("edit_stage", None)
    job["config_edit_key"] = setting_id
    kind_hint = {
        "positive_int": "양의 정수만",
        "signed_int": "음수 또는 양의 정수만",
        "positive_number": "0보다 큰 숫자만",
        "model": "모델 alias 또는 전체 모델 ID를",
        "style": "프리셋 이름을",
    }.get(setting["kind"], "새 값을")
    return send_action_message(
        chat_id,
        f"{setting['label']} 수정\n현재값: {_display_setting_value(current)} ({source})\n\n다음 메시지에 {kind_hint} 입력하고 전송하세요.",
        [[button("현재값 유지", f"cfg:keep:{setting_id}"), button("← 이전", f"cfg:item:{setting_id}")]],
    )


def apply_config_edit_message(chat_id, job, message):
    setting_id = job.get("config_edit_key")
    if not setting_id:
        return False
    text = message.get("text")
    if not text or text.startswith("/"):
        return False
    try:
        saved = _set_config_value(job, setting_id, text)
    except ValueError as exc:
        send_message(chat_id, "설정 오류: " + str(exc) + "\n값을 다시 입력하거나 '현재값 유지'를 누르세요.")
        return True
    job.pop("config_edit_key", None)
    setting = CONFIG_SETTINGS[setting_id]
    send_config_detail(chat_id, job, setting_id, f"저장했습니다: {setting['label']}={_display_setting_value(saved)}")
    return True


def _preserve_settings(job):
    return {k: v for k, v in job.items() if k in _PRESERVED_KEYS}


def _build_extra_env(job):
    env = {}
    if "caption_font_size" in job:
        env["CAPTION_FONT_SIZE"] = str(job["caption_font_size"])
    if "caption_margin_v" in job:
        env["CAPTION_MARGIN_V"] = str(job["caption_margin_v"])
    if "caption_margin_h" in job:
        env["CAPTION_MARGIN_H"] = str(job["caption_margin_h"])
    if "caption_style" in job:
        env["CAPTION_STYLE"] = str(job["caption_style"])
    if "caption_offset_x" in job:
        env["CAPTION_OFFSET_X"] = str(job["caption_offset_x"])
    if "caption_offset_y" in job:
        env["CAPTION_OFFSET_Y"] = str(job["caption_offset_y"])
    if "frame_mode" in job:
        env["FRAME_MODE"] = str(job["frame_mode"])
    if "broll_fit_mode" in job:
        env["BROLL_FIT_MODE"] = str(job["broll_fit_mode"])
    if "frame_top_preset" in job:
        env["FRAME_TOP_PRESET"] = str(job["frame_top_preset"])
    if "frame_bottom_preset" in job:
        env["FRAME_BOTTOM_PRESET"] = str(job["frame_bottom_preset"])
    if "frame_top_pct" in job:
        env["FRAME_TOP_PCT"] = str(job["frame_top_pct"])
    if "frame_bottom_pct" in job:
        env["FRAME_BOTTOM_PCT"] = str(job["frame_bottom_pct"])
    if "frame_bottom_channel_name" in job:
        env["FRAME_BOTTOM_CHANNEL_NAME"] = str(job["frame_bottom_channel_name"])
    if "frame_header_text" in job:
        env["FRAME_HEADER_TEXT"] = str(job["frame_header_text"])
    if "tts_voice" in job:
        env["TTS_VOICE"] = str(job["tts_voice"])
    if "web_research" in job:
        env["ENABLE_WEB_RESEARCH"] = "true" if job.get("web_research") else "false"
    if "case_research" in job:
        env["ENABLE_CASE_RESEARCH"] = "true" if job.get("case_research") else "false"
    if "youtube_feedback_strictness" in job:
        env["YOUTUBE_FEEDBACK_STRICTNESS"] = str(job["youtube_feedback_strictness"])
    if "youtube_feedback_auto_sync" in job:
        env["YOUTUBE_FEEDBACK_AUTO_SYNC"] = "true" if job.get("youtube_feedback_auto_sync") else "false"
    if "speech_pace" in job:
        pace, profile = speech_pace_profile(job["speech_pace"])
        env["SPEECH_PACE"] = pace
        env["ATEMPO"] = str(profile["atempo"])
    if "target_duration_sec" in job:
        env["TARGET_DURATION_SEC"] = str(job["target_duration_sec"])
    if "claude_script_model" in job:
        env["CLAUDE_SCRIPT_MODEL"] = str(job["claude_script_model"])
    if "claude_research_model" in job:
        env["CLAUDE_RESEARCH_MODEL"] = str(job["claude_research_model"])
    if "claude_strategy_model" in job:
        env["CLAUDE_STRATEGY_MODEL"] = str(job["claude_strategy_model"])
    if "claude_query_model" in job:
        env["CLAUDE_QUERY_MODEL"] = str(job["claude_query_model"])
    return env


def _settings_summary(job):
    parts = []
    if "caption_font_size" in job:
        parts.append("font=" + str(job["caption_font_size"]))
    if "caption_margin_v" in job:
        parts.append("margin_v=" + str(job["caption_margin_v"]))
    if "caption_margin_h" in job:
        parts.append("margin_h=" + str(job["caption_margin_h"]))
    if "caption_style" in job:
        parts.append("style=" + str(job["caption_style"]))
    if "caption_offset_x" in job:
        parts.append("offset_x=" + str(job["caption_offset_x"]))
    if "caption_offset_y" in job:
        parts.append("offset_y=" + str(job["caption_offset_y"]))
    if "frame_mode" in job:
        parts.append("frame=" + str(job["frame_mode"]))
    if "broll_fit_mode" in job:
        parts.append("broll_fit=" + str(job["broll_fit_mode"]))
    if "tts_voice" in job:
        parts.append("voice=" + str(job["tts_voice"]))
    if "web_research" in job:
        parts.append("web=" + ("on" if job["web_research"] else "off"))
    if "case_research" in job:
        parts.append("case=" + ("on" if job["case_research"] else "off"))
    if "youtube_feedback_strictness" in job:
        parts.append("feedback=" + str(job["youtube_feedback_strictness"]))
    if "speech_pace" in job:
        parts.append("pace=" + str(job["speech_pace"]))
    if "target_duration_sec" in job:
        parts.append("duration=" + str(job["target_duration_sec"]))
    return "설정: " + (", ".join(parts) if parts else "기본값")


def config_summary(job):
    lines = ["전체 설정"]
    for category_id, label, _ in CONFIG_CATEGORIES:
        lines.extend(("", f"[{label}]"))
        if category_id == "system":
            lines.extend(f"{name}={env_value(name, default)}" for name, default in SYSTEM_CONFIG_FIELDS)
            continue
        for setting_id, setting in CONFIG_SETTINGS.items():
            if setting["category"] != category_id:
                continue
            value, source = _effective_setting_value(job, setting_id)
            env_name = setting.get("env") or setting_id.upper()
            lines.append(f"{env_name}={_display_setting_value(value)} ({source})")
    lines.extend(("", "저장된 override:", json.dumps(_preserve_settings(job), ensure_ascii=False) if _preserve_settings(job) else "없음"))
    return "\n".join(lines)


def handle_set(chat_id, job, text):
    job.pop("config_edit_key", None)
    if text.strip().lower() in ("/set reset", "/set clear"):
        for k in list(_PRESERVED_KEYS):
            job.pop(k, None)
        send_message(chat_id, "설정 초기화 완료. 이후 실행은 기본값을 사용합니다.")
        return

    values = parse_key_values(text)
    if not values:
        send_config_menu(chat_id, job)
        return

    # Validate the complete batch before mutating the job.  A typo in the
    # second key must not leave the first key half-applied.
    validated_values = []
    for input_key, input_value in values.items():
        setting_id = CONFIG_INPUT_ALIASES.get(input_key, input_key)
        if setting_id not in CONFIG_SETTINGS:
            continue
        try:
            saved = _validate_setting_value(setting_id, input_value)
        except ValueError as exc:
            send_message(chat_id, "설정 오류: " + str(exc))
            return
        validated_values.append((setting_id, saved))

    changed = []
    for setting_id, saved in validated_values:
        job[CONFIG_SETTINGS[setting_id]["job_key"]] = saved
        changed.append(f"{setting_id}={_display_setting_value(saved)}")

    if changed:
        send_message(chat_id,
            "설정 저장:\n" + "\n".join("  " + c for c in changed) +
            "\n\n/run_auto와 /run 실행 시 자동 적용됩니다.")
    else:
        send_message(chat_id, "변경할 설정이 없습니다.\n사용법: /set font_size=62 margin_v=60 style=center-yellow offset_y=-120")




def media_duration_seconds(path):
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)],
            text=True,
            capture_output=True,
            check=True,
        )
        return max(float(result.stdout.strip()), 1.0)
    except Exception:
        return None


def render_progress_ratio(progress_path, duration):
    if not progress_path.exists() or not duration:
        return None
    try:
        lines = progress_path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return None
    seconds = None
    for line in lines:
        if line.startswith("out_time_ms=") or line.startswith("out_time_us="):
            try:
                seconds = int(line.split("=", 1)[1]) / 1_000_000
            except ValueError:
                pass
        elif line.startswith("out_time="):
            value = line.split("=", 1)[1]
            try:
                hours, minutes, rest = value.split(":")
                seconds = int(hours) * 3600 + int(minutes) * 60 + float(rest)
            except ValueError:
                pass
    if seconds is None:
        return None
    return max(0.0, min(seconds / duration, 1.0))


def start_render_progress(chat_id, job_id, stop_event):
    duration = media_duration_seconds(work_dir(job_id) / "voice.wav")
    progress_path = work_dir(job_id) / "render_progress.txt"

    def reporter():
        send_message(chat_id, "렌더링 진행률: 시작")
        sent = set()
        checkpoints = [(0.25, "25%"), (0.50, "50%"), (0.75, "75%")]
        while not stop_event.wait(2.0):
            ratio = render_progress_ratio(progress_path, duration)
            if ratio is None:
                continue
            for threshold, label in checkpoints:
                if ratio >= threshold and label not in sent:
                    send_message(chat_id, f"렌더링 진행률: {label}")
                    sent.add(label)

    thread = threading.Thread(target=reporter, daemon=True)
    thread.start()
    return thread


def run_render(chat_id, job):
    job_id = job["job_id"]
    args = [str(BASE_DIR / "sh" / "2_render.sh")]
    font_size = job.get("caption_font_size")
    margin_v = job.get("caption_margin_v")
    margin_h = job.get("caption_margin_h")
    caption_style = job.get("caption_style")
    offset_x = job.get("caption_offset_x")
    offset_y = job.get("caption_offset_y")
    frame_mode = job.get("frame_mode")
    broll_fit = job.get("broll_fit_mode")
    frame_top_preset = job.get("frame_top_preset")
    frame_bottom_preset = job.get("frame_bottom_preset")
    frame_top_pct = job.get("frame_top_pct")
    frame_bottom_pct = job.get("frame_bottom_pct")
    frame_top_title = job.get("frame_top_title", job.get("frame_header_text", ""))
    frame_top_subtitle = job.get("frame_top_subtitle", "")
    frame_bottom_channel = job.get("frame_bottom_channel_name", "")

    def add_arg(flag, value):
        if value not in (None, ""):
            args.extend([flag, str(value)])

    add_arg("--font-size", font_size)
    add_arg("--margin-v", margin_v)
    add_arg("--margin-h", margin_h)
    add_arg("--style", caption_style)
    add_arg("--offset-x", offset_x)
    add_arg("--offset-y", offset_y)
    add_arg("--frame-mode", frame_mode)
    add_arg("--broll-fit", broll_fit)
    add_arg("--frame-top-preset", frame_top_preset)
    add_arg("--frame-bottom-preset", frame_bottom_preset)
    add_arg("--frame-top-pct", frame_top_pct)
    add_arg("--frame-bottom-pct", frame_bottom_pct)
    add_arg("--top-title", frame_top_title)
    add_arg("--top-subtitle", frame_top_subtitle)
    add_arg("--bottom-channel-name", frame_bottom_channel)

    extra_env = _build_extra_env(job)
    send_message(
        chat_id,
        "렌더링 시작: font=" + display_config_value(font_size) +
        ", margin_v=" + display_config_value(margin_v) + ", margin_h=" + display_config_value(margin_h) +
        ", style=" + display_config_value(caption_style) +
        ", offset_x=" + display_config_value(offset_x) + ", offset_y=" + display_config_value(offset_y) +
        ", frame=" + display_config_value(frame_mode) + ", broll_fit=" + display_config_value(broll_fit),
    )
    stop_progress = threading.Event()
    progress_thread = start_render_progress(chat_id, job_id, stop_progress)
    try:
        run_command(args, job_id, job.get("topic"), extra_env=extra_env)
    finally:
        stop_progress.set()
        if progress_thread:
            progress_thread.join(timeout=1)
    send_message(chat_id, "렌더링 진행률: 완료")
    job["stage"] = "await_render_approval"
    send_rendered_video(chat_id, job_id)


def _run_render_silent(chat_id, job, extra_env=None):
    job_id = job["job_id"]
    args = [str(BASE_DIR / "sh" / "2_render.sh")]
    font_size = job.get("caption_font_size")
    margin_v = job.get("caption_margin_v")
    margin_h = job.get("caption_margin_h")
    caption_style = job.get("caption_style")
    offset_x = job.get("caption_offset_x")
    offset_y = job.get("caption_offset_y")
    frame_mode = job.get("frame_mode")
    broll_fit = job.get("broll_fit_mode")
    frame_top_preset = job.get("frame_top_preset")
    frame_bottom_preset = job.get("frame_bottom_preset")
    frame_top_pct = job.get("frame_top_pct")
    frame_bottom_pct = job.get("frame_bottom_pct")
    frame_top_title = job.get("frame_top_title", job.get("frame_header_text", ""))
    frame_top_subtitle = job.get("frame_top_subtitle", "")
    frame_bottom_channel = job.get("frame_bottom_channel_name", "")

    def add_arg(flag, value):
        if value not in (None, ""):
            args.extend([flag, str(value)])

    add_arg("--font-size", font_size)
    add_arg("--margin-v", margin_v)
    add_arg("--margin-h", margin_h)
    add_arg("--style", caption_style)
    add_arg("--offset-x", offset_x)
    add_arg("--offset-y", offset_y)
    add_arg("--frame-mode", frame_mode)
    add_arg("--broll-fit", broll_fit)
    add_arg("--frame-top-preset", frame_top_preset)
    add_arg("--frame-bottom-preset", frame_bottom_preset)
    add_arg("--frame-top-pct", frame_top_pct)
    add_arg("--frame-bottom-pct", frame_bottom_pct)
    add_arg("--top-title", frame_top_title)
    add_arg("--top-subtitle", frame_top_subtitle)
    add_arg("--bottom-channel-name", frame_bottom_channel)

    env = _build_extra_env(job)
    env.update(extra_env or {})
    stop_progress = threading.Event()
    progress_thread = start_render_progress(chat_id, job_id, stop_progress)
    try:
        run_command(args, job_id, job.get("topic"), extra_env=env)
    finally:
        stop_progress.set()
        if progress_thread:
            progress_thread.join(timeout=1)


def run_next_stage(chat_id, job):
    job_id = job["job_id"]
    topic = job.get("topic")
    stage = job.get("stage")

    extra_env = _build_extra_env(job)
    if stage == "await_script_approval":
        send_message(chat_id, "TTS 생성 시작")
        run_command([str(BASE_DIR / "sh" / "1_tts.sh")], job_id, topic, extra_env=extra_env)
        job["stage"] = "await_tts_approval"
        send_tts(chat_id, job_id)
    elif stage == "await_tts_approval":
        send_message(chat_id, "자막 생성 시작")
        run_command([str(BASE_DIR / "sh" / "1_caption.sh")], job_id, topic, extra_env=extra_env)
        job["stage"] = "await_caption_approval"
        send_caption(chat_id, job_id)
    elif stage == "await_caption_approval":
        send_message(chat_id, "B-roll 생성 시작")
        run_command([str(BASE_DIR / "sh" / "1_broll.sh")], job_id, topic, extra_env=extra_env)
        job["stage"] = "await_broll_approval"
        send_broll(chat_id, job_id)
    elif stage == "await_broll_approval":
        job["stage"] = "await_render_config"
        send_render_ready(chat_id, job)
    elif stage == "await_render_config":
        run_render(chat_id, job)
    elif stage == "await_render_approval":
        job["stage"] = "await_upload_meta_approval"
        send_upload_meta(chat_id, job_id)
    elif stage == "await_upload_meta_approval":
        send_message(chat_id, "YouTube 비공개 업로드 시작")
        run_command([str(BASE_DIR / "sh" / "3_upload.sh")], job_id, topic, extra_env=extra_env)
        job["stage"] = "done"
        send_message(chat_id, workflow_status_text(job, "업로드 완료. YouTube Studio에서 비공개 영상을 확인하세요."))
    else:
        send_message(chat_id, f"승인할 단계가 없습니다. 현재 단계: {stage}")

def run_remaining_to_upload(chat_id, job):
    job_id = job.get("job_id")
    topic = job.get("topic")
    if not job_id:
        send_message(chat_id, "진행 중인 작업이 없습니다.")
        return
    start_stage = job.get("stage")
    if start_stage not in WORKFLOW_STAGES:
        send_message(chat_id, f"현재 단계에서는 끝까지 자동 처리를 시작할 수 없습니다: {start_stage}")
        return

    job.pop("edit_target", None)
    job.pop("edit_stage", None)
    job.pop("title_edit_field", None)
    job.pop("title_edit_stage", None)
    if start_stage == "await_script_approval":
        header = load_frame_header(job_id)
        sync_frame_header_to_job(job, header)
    extra_env = _build_extra_env(job)
    job["approval_required"] = False
    job["auto_from_stage"] = start_stage
    job["stage"] = "running_after_review"
    job.pop("last_error", None)
    send_message(chat_id, f"{STAGE_LABELS[start_stage]} 승인 완료. 여기서부터 업로드까지 자동 진행합니다.")

    start_index = WORKFLOW_STAGES.index(start_stage)
    steps = []
    if start_index <= WORKFLOW_STAGES.index("await_script_approval"):
        steps.append(("TTS 음성 생성", lambda: run_command([str(BASE_DIR / "sh" / "1_tts.sh")], job_id, topic, extra_env=extra_env)))
    if start_index <= WORKFLOW_STAGES.index("await_tts_approval"):
        steps.append(("자막 생성", lambda: run_command([str(BASE_DIR / "sh" / "1_caption.sh")], job_id, topic, extra_env=extra_env)))
    if start_index <= WORKFLOW_STAGES.index("await_caption_approval"):
        steps.append(("B-roll 생성", lambda: run_command([str(BASE_DIR / "sh" / "1_broll.sh")], job_id, topic, extra_env=extra_env)))
    if start_index <= WORKFLOW_STAGES.index("await_render_config"):
        steps.append(("최종 영상 렌더링", lambda: _run_render_silent(chat_id, job, extra_env)))
    steps.append(("YouTube 비공개 업로드", lambda: run_command([str(BASE_DIR / "sh" / "3_upload.sh")], job_id, topic, extra_env=extra_env)))

    try:
        for index, (label, action) in enumerate(steps, start=1):
            job["auto_progress"] = f"{index}/{len(steps)} {label}"
            send_message(chat_id, f"{index}/{len(steps)} {label} 중...")
            action()
        job["stage"] = "done"
        job.pop("auto_from_stage", None)
        job.pop("auto_progress", None)
        send_message(chat_id, workflow_status_text(job, "YouTube Studio에서 비공개 영상을 확인하세요."))
    except WorkflowCancelled:
        raise
    except Exception as exc:
        job["stage"] = start_stage
        job["last_error"] = str(exc)
        job.pop("auto_from_stage", None)
        job.pop("auto_progress", None)
        raise


def run_script_generation(chat_id, job, args):
    job_id = job["job_id"]
    try:
        run_command(args, job_id, job.get("topic"), extra_env=_build_extra_env(job))
        job["stage"] = "await_script_approval"
        send_script(chat_id, job_id)
        return True
    except RuntimeError:
        status = read_pubmed_status(job_id)
        if status and status.get("status") == "no_results":
            job["stage"] = "await_pubmed_retry"
            job["pending_script_args"] = args[1:]
            send_action_message(
                chat_id,
                workflow_status_text(job, pubmed_retry_message(status)),
                [[button("근거 부족 감수하고 계속", "proceed_no_pubmed"), button("주제 바꿔 재시도", "retry_topic")],
                 [button("전체 취소", "cancel_all")]],
            )
            send_file_or_path(chat_id, pubmed_status_path(job_id), "pubmed_status.json")
            return False
        raise


def handle_run_goal(chat_id, job, text):
    parts = text.split(maxsplit=2)
    if len(parts) < 2:
        send_message(chat_id, "목표를 입력하세요. 예: /run_goal subscriber_growth 수면")
        return
    objective = parts[1].strip()
    seed = parts[2].strip() if len(parts) > 2 else ""
    job_id = new_job_id("goal")
    settings = _preserve_settings(job)
    busy = job.get("busy")
    job.clear()
    job.update({
        "job_id": job_id, "topic": seed, "objective": objective,
        "approval_required": True, "stage": "running_goal_plan",
    })
    job.update(settings)
    if busy:
        job["busy"] = busy
    plan_path = work_dir(job_id) / "topic_plan.json"
    args = [
        "python3", str(BASE_DIR / "src" / "0_topic_plan.py"), "plan",
        "--objective", objective, "--job-id", job_id, "--output", str(plan_path),
    ]
    if seed:
        args.extend(["--seed", seed])
    send_message(chat_id, f"목표 기반 기획 시작: {objective}" + (f" / 씨드: {seed}" if seed else ""))
    run_command(args, job_id, seed, extra_env=_build_extra_env(job))
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    goal = plan.get("objective") or {}
    job["topic"] = plan.get("topic") or seed
    job["plan_id"] = goal.get("plan_id")
    send_message(chat_id, "\n".join([
        f"목표: {objective}",
        f"상태: {goal.get('decision', 'manual_review')}",
        f"선정 주제: {job['topic']}",
        f"선정 이유: {goal.get('reason', '결정론 점수와 위험 검토 결과')}",
        f"주의: 확신도 {goal.get('confidence', 0):.2f}; 성과를 보장하지 않습니다.",
    ]))
    send_file_or_path(chat_id, plan_path, "topic_plan.json")
    if goal.get("decision") in ("manual_review", "rejected"):
        job["stage"] = "await_goal_review"
        send_message(chat_id, "데이터가 오래됐거나 모든 후보 점수가 낮아 자동 제작을 중단했습니다.")
        return
    run_script_generation(
        chat_id, job,
        [str(BASE_DIR / "sh" / "0_script.sh"), "--topic-json", str(plan_path)],
    )


def handle_goal_query(chat_id, job, command):
    job_id = job.get("job_id") or "goal_status"
    output = run_command(
        ["python3", str(BASE_DIR / "src" / "0_topic_plan.py"), command], job_id,
        job.get("topic"), extra_env=_build_extra_env(job),
    )
    send_message(chat_id, output[-MAX_BLOCK_TEXT:] or "목표 기획 이력이 없습니다.")


def handle_run_auto(chat_id, job, text):
    topic = text.split(maxsplit=1)[1].strip() if len(text.split(maxsplit=1)) > 1 else ""
    if not topic:
        send_message(chat_id,
            "주제를 입력하세요.\n"
            "예: /run_auto 치매 초기증상과 건망증 차이\n\n"
            "기본 설정: font_size=62 margin_v=60 web=on\n"
            "실행 전 변경: /set font_size=62 margin_v=60 margin_h=12 web=off case=off"
        )
        return

    job_id   = new_job_id("auto")
    settings = _preserve_settings(job)
    busy     = job.get("busy")
    job.clear()
    job.update({
        "job_id": job_id, "topic": topic,
        "approval_required": False, "stage": "running_auto",
    })
    job.update(settings)
    if busy:
        job["busy"] = busy

    extra_env = _build_extra_env(job)
    send_message(chat_id,
        "자동 실행 시작\n"
        "JOB_ID: " + job_id + "\n"
        "주제: " + topic + "\n" +
        _settings_summary(job)
    )

    send_message(chat_id, "1/5 스크립트 생성 중...")
    run_command(
        [str(BASE_DIR / "sh" / "0_script.sh"), "--allow-no-pubmed", topic],
        job_id, topic, extra_env=extra_env,
    )
    send_message(chat_id, "1/5 스크립트 완료")

    send_message(chat_id, "2/5 TTS 음성 생성 중...")
    run_command([str(BASE_DIR / "sh" / "1_tts.sh")], job_id, topic, extra_env=extra_env)
    send_message(chat_id, "2/5 TTS 완료")

    send_message(chat_id, "3/5 자막 생성 중...")
    run_command([str(BASE_DIR / "sh" / "1_caption.sh")], job_id, topic, extra_env=extra_env)
    send_message(chat_id, "3/5 자막 완료")

    send_message(chat_id, "4/5 B-roll 수집 중...")
    run_command([str(BASE_DIR / "sh" / "1_broll.sh")], job_id, topic, extra_env=extra_env)
    send_message(chat_id, "4/5 B-roll 완료")

    send_message(chat_id, "5/5 렌더링 중...")
    _run_render_silent(chat_id, job, extra_env)
    send_message(chat_id, "5/5 렌더링 완료")

    send_message(chat_id, "업로드 중...")
    run_command([str(BASE_DIR / "sh" / "3_upload.sh")], job_id, topic)

    job["stage"] = "done"
    send_message(chat_id, workflow_status_text(job, "YouTube Studio에서 비공개 영상을 확인하세요."))

def handle_run(chat_id, job, text, trend=False):
    topic = text.split(maxsplit=1)[1].strip() if len(text.split(maxsplit=1)) > 1 else ""
    if not topic:
        send_message(chat_id, "주제를 입력하세요. 예: /run 오메가3가 정말 뇌에 좋을까?")
        return
    job_id = new_job_id("trend" if trend else "slack")
    settings = _preserve_settings(job)
    busy = job.get("busy")
    job.clear()
    job.update({"job_id": job_id, "topic": topic, "approval_required": True})
    job.update(settings)
    if busy:
        job["busy"] = busy
    if trend:
        job["stage"] = "await_trend_choice"
        send_message(chat_id, f"트렌드 후보 조회 시작: {topic}")
        run_command([str(BASE_DIR / "sh" / "0_script.sh"), "--trend", topic], job_id, topic, extra_env=_build_extra_env(job))
        candidates_path = work_dir(job_id) / "trend_candidates.json"
        payload = json.loads(candidates_path.read_text(encoding="utf-8"))
        lines = ["후보를 선택하세요. 번호를 입력하지 않고 버튼만 누르면 됩니다."]
        rows = []
        for i, item in enumerate(payload.get("candidates", []), start=1):
            lines.append(f"{i}. {item.get('keyword')} ({', '.join(item.get('sources', []))})")
            rows.append([button(f"{i}. {str(item.get('keyword') or '')[:55]}", f"pick_trend:{i}")])
        rows.append([button("전체 취소", "cancel_all")])
        send_action_message(chat_id, workflow_status_text(job, "\n".join(lines)), rows)
        send_file_or_path(chat_id, candidates_path, "trend_candidates.json")
    else:
        job["stage"] = "await_script_approval"
        send_message(chat_id, f"스크립트 생성 시작: JOB_ID={job_id}")
        run_script_generation(chat_id, job, [str(BASE_DIR / "sh" / "0_script.sh"), topic])

def handle_retry(chat_id, job, text):
    topic = text.split(maxsplit=1)[1].strip() if len(text.split(maxsplit=1)) > 1 else ""
    if not topic:
        send_message(chat_id, "새 주제를 입력하세요. 예: /retry 오메가3 기억력")
        return
    job_id = job.get("job_id") or new_job_id("retry")
    job["job_id"] = job_id
    job["topic"] = topic
    job["approval_required"] = True
    send_message(chat_id, f"새 주제로 스크립트 생성 재시도: {topic}")
    run_script_generation(chat_id, job, [str(BASE_DIR / "sh" / "0_script.sh"), topic])


def handle_proceed(chat_id, job):
    job_id = job.get("job_id")
    if not job_id:
        send_message(chat_id, "진행 중인 작업이 없습니다.")
        return
    pending = job.get("pending_script_args")
    if not pending:
        send_message(chat_id, "근거 부족 상태에서 이어갈 명령이 없습니다.")
        return
    send_message(chat_id, "PubMed 근거 부족을 감수하고 일반 설명 중심으로 스크립트 생성을 진행합니다.")
    run_script_generation(chat_id, job, [str(BASE_DIR / "sh" / "0_script.sh"), "--allow-no-pubmed", *pending])

def handle_pick(chat_id, job, text):
    if job.get("stage") != "await_trend_choice":
        send_message(chat_id, "선택할 트렌드 후보가 없습니다. 먼저 /trend 키워드를 실행하세요.")
        return
    parts = text.split()
    if len(parts) < 2 or not parts[1].isdigit():
        send_message(chat_id, "사용법: /pick 1")
        return
    choice = parts[1]
    job_id = job["job_id"]
    send_message(chat_id, f"선택 후보로 스크립트 생성 시작: {choice}")
    run_script_generation(chat_id, job, [str(BASE_DIR / "sh" / "0_script.sh"), "--trend-choice", choice])



def handle_edit(chat_id, job):
    job_id = job.get("job_id")
    stage = job.get("stage")
    info = editable_stage_info(stage, job_id)
    if not info:
        send_message(chat_id, "현재 단계는 텍스트 파일 수정 대상이 아닙니다. 재생성이나 렌더 설정 버튼을 사용하세요.")
        return
    path, name = info
    job.pop("title_edit_field", None)
    job.pop("title_edit_stage", None)
    job["edit_target"] = str(path)
    job["edit_stage"] = stage
    send_message(
        chat_id,
        f"수정 모드입니다. 아래 {name} 파일을 열어 필요한 부분만 고친 뒤, 수정한 파일을 Slack으로 다시 보내세요. "
        "짧은 수정이면 다음 메시지에 전체 수정본을 보내도 됩니다.",
    )
    if path.exists():
        send_file_or_path(chat_id, path, f"수정용 원본: {name}")


def send_title_edit_menu(chat_id, job):
    job_id = job.get("job_id")
    if not job_id:
        send_message(chat_id, "진행 중인 작업이 없습니다.")
        return
    if job.get("stage") != "await_script_approval":
        send_message(chat_id, f"타이틀 수정은 스크립트 승인 단계에서만 가능합니다. 현재 단계: {job.get('stage')}")
        return

    job.pop("edit_target", None)
    job.pop("edit_stage", None)
    job.pop("title_edit_field", None)
    job.pop("title_edit_stage", None)
    header = load_frame_header(job_id)
    sync_frame_header_to_job(job, header)
    title = header.get("title") or "(비어 있음)"
    subtitle = header.get("subtitle") or "(비어 있음)"
    send_action_message(
        chat_id,
        workflow_status_text(job, "타이틀 수정\n"
        f"현재 주제목: {title}\n"
        f"현재 부제목: {subtitle}\n\n"
        "수정할 항목을 선택하세요. 선택 후 다음 메시지에 새 문구를 보내면 띄어쓰기 포함 그대로 저장됩니다."),
        [
            [button("주제목 수정", "edit_title_field:await_script_approval:title"),
             button("부제목 수정", "edit_title_field:await_script_approval:subtitle"),
             button("본문 수정", "edit_body:await_script_approval")],
            [button("다음 단계 ▶", "approve:await_script_approval")],
            [button("🚀 여기서부터 끝까지", "auto_finish:await_script_approval"),
             button("↻ 상태", "show_status"), button("⌂ 홈", "show_home"), button("전체 취소", "cancel_all")],
        ],
    )


def handle_title_edit_field(chat_id, job, field):
    if job.get("stage") != "await_script_approval":
        send_message(chat_id, f"이전 단계 버튼입니다. 현재 단계는 {job.get('stage')}입니다.")
        return
    if field not in ("title", "subtitle"):
        send_message(chat_id, "수정할 수 없는 타이틀 항목입니다.")
        return
    label = "주제목" if field == "title" else "부제목"
    current = load_frame_header(job["job_id"]).get(field, "")
    job["title_edit_field"] = field
    job["title_edit_stage"] = job.get("stage")
    job.pop("edit_target", None)
    job.pop("edit_stage", None)
    send_message(
        chat_id,
        f"{label} 수정 모드입니다.\n"
        f"현재값: {current or '(비어 있음)'}\n\n"
        f"다음 메시지에 새 {label}을 보내세요. 띄어쓰기 포함 전체 메시지가 그대로 저장됩니다.",
    )


def apply_title_edit_message(chat_id, job, message):
    field = job.get("title_edit_field")
    if not field:
        return False
    if message.get("document"):
        send_message(chat_id, "타이틀은 텍스트 메시지로 보내세요. 띄어쓰기 포함 입력할 수 있습니다.")
        return True
    text = message.get("text")
    if not text or text.startswith("/"):
        return False
    value = text.strip()
    if not value:
        send_message(chat_id, "빈 문구는 저장할 수 없습니다. 새 문구를 다시 보내세요.")
        return True

    job_id = job.get("job_id")
    job.pop("edit_target", None)
    job.pop("edit_stage", None)
    job.pop("title_edit_field", None)
    job.pop("title_edit_stage", None)
    header = load_frame_header(job_id)
    header[field] = value
    saved = save_frame_header(job_id, header)
    sync_frame_header_to_job(job, saved)
    job.pop("title_edit_field", None)
    job["stage"] = job.pop("title_edit_stage", job.get("stage"))

    label = "주제목" if field == "title" else "부제목"
    send_message(chat_id, f"{label}을 저장했습니다: {value}")
    send_title_edit_menu(chat_id, job)
    return True


def apply_edit_message(chat_id, job, message):
    target = job.get("edit_target")
    if not target:
        return False
    path = Path(target)
    path.parent.mkdir(parents=True, exist_ok=True)
    doc = message.get("document")
    if doc:
        download_slack_file(doc, path)
    else:
        text = message.get("text")
        if not text or text.startswith("/"):
            return False
        path.write_text(text, encoding="utf-8")
    job.pop("edit_target", None)
    job["stage"] = job.pop("edit_stage", job.get("stage"))
    send_message(chat_id, f"수정본을 저장했습니다: {path.name}")
    stage = job.get("stage")
    if stage == "await_script_approval":
        send_script(chat_id, job["job_id"])
    elif stage == "await_caption_approval":
        send_caption(chat_id, job["job_id"])
    elif stage == "await_upload_meta_approval":
        send_upload_meta(chat_id, job["job_id"])
    return True


def handle_callback(state, callback):
    chat_id = callback.get("message", {}).get("chat", {}).get("id")
    data = callback.get("data", "")
    if not chat_id:
        return
    job = chat_state(state, chat_id)
    if is_busy(job) and data not in ("cancel_all", "cancel_confirm", "show_status", "show_home"):
        send_message(chat_id, busy_message(job))
        return True
    try:
        if data in ("show_home", "start_cancel", "goal:cancel"):
            job.pop("start_draft", None)
            job.pop("goal_draft", None)
            send_home_screen(chat_id, "홈으로 돌아왔습니다." if data in ("start_cancel", "goal:cancel") else None)
        elif data == "start_goal":
            begin_goal_flow(chat_id, job)
        elif data.startswith("goal:objective:"):
            select_goal_objective(chat_id, job, data.split(":", 2)[2])
        elif data.startswith("goal:seed:"):
            select_goal_seed_mode(chat_id, job, data.split(":", 2)[2])
        elif data == "goal:back:objectives":
            job["goal_draft"] = {}
            send_goal_objective_menu(chat_id, job)
        elif data == "goal:back:seed":
            draft = job.get("goal_draft") or {}
            draft.pop("awaiting_seed", None)
            job["goal_draft"] = draft
            send_goal_seed_menu(chat_id, job)
        elif data == "goal:confirm":
            confirm_goal_flow(state, chat_id, job)
        elif data.startswith("start_content:"):
            begin_start_flow(chat_id, job, data.split(":", 1)[1])
        elif data == "start_reenter_topic":
            draft = job.get("start_draft") or {}
            draft.pop("topic", None)
            job["start_draft"] = draft
            prompt_start_topic(chat_id, job)
        elif data.startswith("start_confirm:"):
            confirm_start_flow(state, chat_id, job, data.split(":", 1)[1])
        elif data == "open_settings":
            send_config_menu(chat_id, job)
        elif data == "show_status":
            handle_status(chat_id, job)
        elif data == "cancel_all":
            send_action_message(
                chat_id,
                workflow_status_text(job, "정말 전체 작업을 취소할까요? 이미 생성된 산출물은 삭제하지 않습니다."),
                [[button("취소 확정", "cancel_confirm"), button("계속 작업", "show_status")]],
            )
        elif data == "cancel_confirm":
            request_workflow_cancel(chat_id, job)
        elif data == "cfg:root":
            send_config_menu(chat_id, job)
        elif data == "cfg:all":
            send_action_message(chat_id, config_summary(job), [[button("← 설정 상자", "cfg:root")]])
        elif data == "cfg:reset":
            send_action_message(
                chat_id,
                "저장된 설정 override를 모두 초기화할까요? 환경 설정 파일은 변경하지 않습니다.",
                [[button("초기화", "cfg:reset_confirm"), button("취소", "cfg:root")]],
            )
        elif data == "cfg:reset_confirm":
            for key in _PRESERVED_KEYS:
                job.pop(key, None)
            job.pop("config_edit_key", None)
            send_config_menu(chat_id, job, "전체 override를 초기화했습니다.")
        elif data.startswith("cfg:cat:"):
            category_id = data.split(":", 2)[2]
            if category_id not in {category[0] for category in CONFIG_CATEGORIES}:
                raise ValueError("알 수 없는 설정 카테고리입니다.")
            send_config_category(chat_id, job, category_id)
        elif data.startswith("cfg:item:"):
            setting_id = data.split(":", 2)[2]
            if setting_id not in CONFIG_SETTINGS:
                raise ValueError("알 수 없는 설정 항목입니다.")
            send_config_detail(chat_id, job, setting_id)
        elif data.startswith("cfg:edit:"):
            setting_id = data.split(":", 2)[2]
            if setting_id not in CONFIG_SETTINGS:
                raise ValueError("알 수 없는 설정 항목입니다.")
            begin_config_edit(chat_id, job, setting_id)
        elif data.startswith("cfg:pick:"):
            _, _, setting_id, choice_index_text = data.split(":", 3)
            if setting_id not in CONFIG_SETTINGS:
                raise ValueError("알 수 없는 설정 항목입니다.")
            choices = CONFIG_SETTINGS[setting_id].get("choices", ())
            choice_index = int(choice_index_text)
            if choice_index < 0 or choice_index >= len(choices):
                raise ValueError("선택할 수 없는 설정값입니다.")
            saved = _set_config_value(job, setting_id, choices[choice_index][1])
            job.pop("config_edit_key", None)
            send_config_detail(
                chat_id,
                job,
                setting_id,
                f"저장했습니다: {CONFIG_SETTINGS[setting_id]['label']}={_display_setting_value(saved)}",
            )
        elif data.startswith("cfg:keep:"):
            setting_id = data.split(":", 2)[2]
            if setting_id not in CONFIG_SETTINGS:
                raise ValueError("알 수 없는 설정 항목입니다.")
            job.pop("config_edit_key", None)
            send_config_category(chat_id, job, CONFIG_SETTINGS[setting_id]["category"], "현재값을 유지했습니다.")
        elif data.startswith("cfg:default:"):
            setting_id = data.split(":", 2)[2]
            if setting_id not in CONFIG_SETTINGS:
                raise ValueError("알 수 없는 설정 항목입니다.")
            job.pop(CONFIG_SETTINGS[setting_id]["job_key"], None)
            job.pop("config_edit_key", None)
            send_config_detail(chat_id, job, setting_id, "override를 해제하고 기본값을 사용합니다.")
        elif data.startswith("approve:"):
            expected_stage = data.split(":", 1)[1]
            if job.get("stage") != expected_stage:
                send_message(chat_id, f"이전 단계 버튼입니다. 현재 단계는 {job.get('stage')}입니다.")
                return
            start_background_task(state, chat_id, job, "현재 단계 실행", lambda: run_next_stage(chat_id, job))
        elif data.startswith("edit:"):
            expected_stage = data.split(":", 1)[1]
            if job.get("stage") != expected_stage:
                send_message(chat_id, f"이전 단계 버튼입니다. 현재 단계는 {job.get('stage')}입니다.")
                return
            handle_edit(chat_id, job)
        elif data.startswith("edit_body:"):
            expected_stage = data.split(":", 1)[1]
            if job.get("stage") != expected_stage:
                send_message(chat_id, f"이전 단계 버튼입니다. 현재 단계는 {job.get('stage')}입니다.")
                return
            handle_edit(chat_id, job)
        elif data.startswith("edit_title_menu:"):
            expected_stage = data.split(":", 1)[1]
            if job.get("stage") != expected_stage:
                send_message(chat_id, f"이전 단계 버튼입니다. 현재 단계는 {job.get('stage')}입니다.")
                return
            send_title_edit_menu(chat_id, job)
        elif data.startswith("edit_title_field:"):
            _, expected_stage, field = data.split(":", 2)
            if job.get("stage") != expected_stage:
                send_message(chat_id, f"이전 단계 버튼입니다. 현재 단계는 {job.get('stage')}입니다.")
                return
            handle_title_edit_field(chat_id, job, field)
        elif data.startswith(("auto_upload:", "auto_finish:")):
            expected_stage = data.split(":", 1)[1]
            if job.get("stage") != expected_stage:
                send_message(chat_id, f"이전 단계 버튼입니다. 현재 단계는 {job.get('stage')}입니다.")
                return
            send_action_message(
                chat_id,
                workflow_status_text(job, "현재 산출물을 승인하고 남은 검수를 건너뛴 뒤 YouTube 비공개 업로드까지 진행할까요?"),
                [[button("확인: 끝까지 실행", f"auto_finish_confirm:{expected_stage}"), button("돌아가기", "show_status")]],
            )
        elif data.startswith("auto_finish_confirm:"):
            expected_stage = data.split(":", 1)[1]
            if job.get("stage") != expected_stage:
                send_message(chat_id, f"현재 단계가 바뀌어 실행하지 않았습니다. 현재 단계: {job.get('stage')}")
                return
            start_background_task(state, chat_id, job, "끝까지 자동 처리", lambda: run_remaining_to_upload(chat_id, job))
        elif data == "proceed_no_pubmed":
            start_background_task(state, chat_id, job, "근거 부족 상태로 계속", lambda: handle_proceed(chat_id, job))
        elif data == "retry_topic":
            job["retry_topic_input"] = True
            send_message(chat_id, "새 주제를 다음 메시지로 보내주세요. 받는 즉시 스크립트를 다시 생성합니다.")
        elif data.startswith("pick_trend:"):
            choice = data.split(":", 1)[1]
            if job.get("stage") != "await_trend_choice":
                send_message(chat_id, f"이전 단계 버튼입니다. 현재 단계는 {job.get('stage')}입니다.")
                return
            start_background_task(state, chat_id, job, "선택한 트렌드로 스크립트 생성", lambda: handle_pick(chat_id, job, "/pick " + choice))
        elif data.startswith("back:"):
            _, expected_stage, target_stage = data.split(":", 2)
            if job.get("stage") != expected_stage:
                send_message(chat_id, f"이전 단계 버튼입니다. 현재 단계는 {job.get('stage')}입니다.")
                return
            go_back_to_stage(chat_id, job, target_stage)
        elif data.startswith("render:"):
            parts = data.split(":")
            _, expected_stage, font_size, margin_v = parts[:4]
            caption_style = parts[4] if len(parts) > 4 else None
            if job.get("stage") != expected_stage:
                send_message(chat_id, f"이전 단계 버튼입니다. 현재 단계는 {job.get('stage')}입니다.")
                return
            job["caption_font_size"] = positive_int(font_size, "font_size")
            job["caption_margin_v"] = positive_int(margin_v, "margin_v")
            if caption_style:
                job["caption_style"] = safe_caption_style(caption_style)
            start_background_task(state, chat_id, job, "렌더링", lambda: run_render(chat_id, job))
        elif data.startswith("rerun:"):
            _, expected_stage, target = data.split(":", 2)
            if job.get("stage") != expected_stage:
                send_message(chat_id, f"이전 단계 버튼입니다. 현재 단계는 {job.get('stage')}입니다.")
                return
            start_background_task(state, chat_id, job, f"{target} 재생성", lambda: handle_rerun(chat_id, job, "/rerun " + target))
    except Exception as exc:
        job["last_error"] = str(exc)
        _send_recovery_error(chat_id, data, exc)
        return False
    finally:
        # Inline buttons mutate the per-chat job directly.  Keep those
        # changes durable just like the /set command path.
        save_state(state)
    return True

def go_back_to_stage(chat_id, job, target_stage):
    job_id = job.get("job_id")
    if not job_id:
        send_message(chat_id, "진행 중인 작업이 없습니다.")
        return
    senders = {
        "await_script_approval": send_script,
        "await_tts_approval": send_tts,
        "await_caption_approval": send_caption,
        "await_broll_approval": send_broll,
        "await_render_config": None,
        "await_render_approval": send_rendered_video,
        "await_upload_meta_approval": send_upload_meta,
    }
    if target_stage not in senders:
        send_message(chat_id, f"돌아갈 수 없는 단계입니다: {target_stage}")
        return
    job["stage"] = target_stage
    send_message(chat_id, "이전 단계로 돌아갑니다. 확인 후 다시 승인하세요.")
    sender = senders[target_stage]
    if target_stage == "await_render_config":
        send_render_ready(chat_id, job)
    elif sender:
        sender(chat_id, job_id)


def handle_rerun(chat_id, job, text):
    parts = text.split()
    target = parts[1].lower() if len(parts) > 1 else ""
    job_id = job.get("job_id")
    if not job_id:
        send_message(chat_id, "진행 중인 작업이 없습니다.")
        return
    mapping = {
        "tts": ("1_tts.sh", "await_tts_approval", send_tts),
        "caption": ("1_caption.sh", "await_caption_approval", send_caption),
        "broll": ("1_broll.sh", "await_broll_approval", send_broll),
    }
    if target not in mapping:
        send_message(chat_id, "사용법: /rerun tts | /rerun caption | /rerun broll")
        return
    script, next_stage, sender = mapping[target]
    send_message(chat_id, f"{target} 재생성 시작")
    run_command([str(BASE_DIR / "sh" / script)], job_id, job.get("topic"))
    job["stage"] = next_stage
    sender(chat_id, job_id)


def handle_render(chat_id, job, text):
    if not job.get("job_id"):
        send_message(chat_id, "진행 중인 작업이 없습니다.")
        return
    values = parse_key_values(text)
    if "font_size" in values:
        job["caption_font_size"] = positive_int(values["font_size"], "font_size")
    if "margin_v" in values:
        job["caption_margin_v"] = positive_int(values["margin_v"], "margin_v")
    if "margin_h" in values:
        job["caption_margin_h"] = positive_int(values["margin_h"], "margin_h")
    if "style" in values:
        job["caption_style"] = safe_caption_style(values["style"])
    if "caption_style" in values:
        job["caption_style"] = safe_caption_style(values["caption_style"])
    if "offset_x" in values:
        job["caption_offset_x"] = signed_int(values["offset_x"], "offset_x")
    if "offset_y" in values:
        job["caption_offset_y"] = signed_int(values["offset_y"], "offset_y")
    frame_value = values.get("frame") or values.get("frame_mode")
    if frame_value is not None:
        job["frame_mode"] = safe_choice(frame_value, "frame", ("full", "framed"))
    broll_fit = values.get("broll_fit") or values.get("broll_fit_mode")
    if broll_fit is not None:
        job["broll_fit_mode"] = safe_choice(broll_fit, "broll_fit", ("cover", "contain", "blur-contain"))
    if "header" in values:
        job["frame_top_title"] = values["header"]
    if "top_title" in values:
        job["frame_top_title"] = values["top_title"]
    if "top_subtitle" in values:
        job["frame_top_subtitle"] = values["top_subtitle"]
    if "channel" in values:
        job["frame_bottom_channel_name"] = values["channel"]
    if "top_preset" in values:
        job["frame_top_preset"] = safe_caption_style(values["top_preset"])
    if "bottom_preset" in values:
        job["frame_bottom_preset"] = safe_caption_style(values["bottom_preset"])
    if "top_pct" in values:
        job["frame_top_pct"] = values["top_pct"]
    if "bottom_pct" in values:
        job["frame_bottom_pct"] = values["bottom_pct"]
    run_render(chat_id, job)


def handle_status(chat_id, job):
    if not job or not (job.get("job_id") or job.get("stage") or job.get("busy")):
        send_home_screen(chat_id, "현재 진행 중인 작업이 없습니다.")
        return
    stage = job.get("stage")
    if stage == "await_render_config" and not is_busy(job):
        send_render_ready(chat_id, job)
        return
    if is_busy(job):
        rows = [[button("↻ 새로고침", "show_status"), button("전체 취소", "cancel_all")]]
    elif stage in WORKFLOW_STAGES:
        rows = approval_buttons(stage)
    elif stage == "await_pubmed_retry":
        rows = [[button("근거 부족 감수하고 계속", "proceed_no_pubmed"), button("주제 바꿔 재시도", "retry_topic")], [button("전체 취소", "cancel_all")]]
    else:
        rows = []
    text = workflow_status_text(job, "현재 산출물은 유지됩니다. 이전 단계로 돌아가 수정하거나, 다음 단계 또는 끝까지 자동 처리를 선택하세요.")
    if rows:
        send_action_message(chat_id, text, rows)
    else:
        send_message(chat_id, text)


def command_specs():
    return [
        ("run", "승인형 파이프라인 시작"),
        ("set", "카테고리별 설정 메뉴 열기"),
        ("set_all", "현재 전체 설정 한 번에 보기"),
        ("run_auto", "승인 없이 전체 파이프라인 실행"),
        ("run_goal", "목표 기반 주제 기획 후 승인형 제작"),
        ("goal_status", "최근 목표 기획 상태"),
        ("goal_report", "목표 기획·가설 보고서"),
        ("trend", "트렌드 후보 조회"),
        ("pick", "트렌드 후보 선택"),
        ("approve", "현재 산출물 승인"),
        ("edit", "현재 텍스트 산출물 수정"),
        ("retry", "PubMed 실패 후 새 주제 재시도"),
        ("proceed", "PubMed 실패 후 근거 부족 상태로 진행"),
        ("rerun", "tts/caption/broll 재생성"),
        ("render", "자막 렌더 설정 변경"),
        ("app_status", "현재 상태 확인"),
        ("cancel", "전체 작업 취소"),
        ("help", "명령어 도움말"),
    ]


def help_text():
    return "\n".join([
        "콘텐츠 제작 도우미",
        "검수 화면의 버튼만으로 이전/다음/수정/끝까지 자동 처리/취소가 가능합니다.",
        "/app_status를 실행하면 현재 단계 카드와 버튼을 다시 표시합니다.",
        "",
        "시작 명령어",
        "/run 오메가3가 정말 뇌에 좋을까?",
        "/trend 오메가3",
        "/pick 1",
        "/approve",
        "/edit",
        "/retry 오메가3 기억력",
        "/proceed",
        "/rerun tts | /rerun caption | /rerun broll",
        "/render font_size=62 margin_v=60",
        "/set  <- 카테고리별 설정 메뉴",
        "/set_all  <- 현재 전체 설정 보기",
        "/set font_size=62 web=off  <- 기존 빠른 입력도 지원",
        "/set reset  <- 저장한 override 전체 초기화",
        "/run_auto 오메가3가 정말 뇌에 좋을까?",
        "/run_goal subscriber_growth 수면",
        "/goal_status | /goal_report",
        "/app_status",
        "/cancel",
        "",
        "흐름: 시작 -> 필요한 단계만 꼼꼼히 확인 -> 원하는 지점에서 끝까지 자동 처리 -> 비공개 업로드",
    ])


def handle_message(state, message):
    chat_id = message.get("chat", {}).get("id")
    text = (message.get("text") or "").strip()
    if not chat_id:
        return
    if ALLOWED_CHAT_ID and str(chat_id) != str(ALLOWED_CHAT_ID):
        send_message(chat_id, "허용되지 않은 chat_id입니다.")
        return

    job = chat_state(state, chat_id)
    try:
        if is_busy(job) and not (
            text.startswith("/app_status") or text.startswith("/cancel")
            or text.startswith("/goal_status") or text.startswith("/goal_report")
        ):
            send_message(chat_id, busy_message(job))
            return
        if (job.get("goal_draft") or {}).get("awaiting_seed") and text and not text.startswith("/"):
            capture_goal_seed(chat_id, job, text)
            return
        if job.get("start_draft") and text and not text.startswith("/"):
            capture_start_topic(chat_id, job, text)
            return
        if job.get("retry_topic_input") and text and not text.startswith("/"):
            job.pop("retry_topic_input", None)
            start_background_task(state, chat_id, job, "새 주제로 스크립트 재생성", lambda: handle_retry(chat_id, job, "/retry " + text))
            return
        if apply_config_edit_message(chat_id, job, message):
            # A plain message is the second half of a button-initiated edit.
            # Persist it before returning so a bot restart cannot lose the
            # value that was just entered.
            save_state(state)
            return
        if apply_title_edit_message(chat_id, job, message):
            return
        if apply_edit_message(chat_id, job, message):
            return
        if not text:
            send_home_screen(chat_id)
        elif text.startswith("/start") or text.startswith("/help"):
            send_home_screen(chat_id)
        elif text.startswith("/set_all") or text.startswith("/setall"):
            job.pop("config_edit_key", None)
            send_message(chat_id, config_summary(job))
            save_state(state)
        elif text.startswith("/set"):
            handle_set(chat_id, job, text)
            save_state(state)
        elif text == "/run_goal" or text.startswith("/run_goal "):
            start_background_task(state, chat_id, job, "목표 기반 기획", lambda: handle_run_goal(chat_id, job, text))
        elif text.startswith("/goal_status"):
            handle_goal_query(chat_id, job, "status")
        elif text.startswith("/goal_report"):
            handle_goal_query(chat_id, job, "report")
        elif text == "/run_auto" or text.startswith("/run_auto "):
            begin_start_flow(chat_id, job, "auto", text.partition(" ")[2])
        elif text == "/run" or text.startswith("/run "):
            begin_start_flow(chat_id, job, "review", text.partition(" ")[2])
        elif text == "/trend" or text.startswith("/trend "):
            begin_start_flow(chat_id, job, "trend", text.partition(" ")[2])
        elif text.startswith("/pick"):
            start_background_task(state, chat_id, job, "스크립트 생성", lambda: handle_pick(chat_id, job, text))
        elif text.startswith("/approve"):
            start_background_task(state, chat_id, job, "현재 단계 실행", lambda: run_next_stage(chat_id, job))
        elif text.startswith("/edit"):
            handle_edit(chat_id, job)
        elif text.startswith("/retry ") or text == "/retry":
            start_background_task(state, chat_id, job, "스크립트 재생성", lambda: handle_retry(chat_id, job, text))
        elif text.startswith("/proceed"):
            start_background_task(state, chat_id, job, "스크립트 생성", lambda: handle_proceed(chat_id, job))
        elif text.startswith("/rerun"):
            start_background_task(state, chat_id, job, "재생성", lambda: handle_rerun(chat_id, job, text))
        elif text.startswith("/render"):
            start_background_task(state, chat_id, job, "렌더링", lambda: handle_render(chat_id, job, text))
        elif text.startswith("/app_status"):
            handle_status(chat_id, job)
        elif text.startswith("/cancel"):
            request_workflow_cancel(chat_id, job)
        else:
            send_message(chat_id, help_text())
    except Exception as exc:
        job["last_error"] = str(exc)
        log_event("ERROR", "slack_message_failed", channel=chat_id, job_id=job.get("job_id"), stage=job.get("stage"), error=exc)
        _send_recovery_error(chat_id, "show_status", exc, label="메시지 또는 명령 처리")
        return False
    return True



def _allow(channel_id, user_id):
    return (not ALLOWED_CHANNEL_ID or str(channel_id) == str(ALLOWED_CHANNEL_ID)) and (not ALLOWED_USER_ID or str(user_id) == str(ALLOWED_USER_ID))


def _denied(channel_id):
    send_message(channel_id, "허용되지 않은 Slack 채널 또는 사용자입니다.")


def _event_to_message(event):
    files = event.get("files") or []
    message = {"chat": {"id": event["channel"]}, "text": event.get("text", "")}
    if files:
        message["document"] = files[0]
    return message


def _dispatch_message(event):
    channel_id, user_id = event.get("channel"), event.get("user")
    if not channel_id or event.get("bot_id") or event.get("subtype") not in (None, "file_share"):
        return
    if not _allow(channel_id, user_id):
        _denied(channel_id)
        return
    job = chat_state(_STATE, channel_id)
    job["slack_thread_ts"] = event.get("thread_ts") or event.get("ts")
    request = "파일 또는 텍스트 입력" if event.get("files") else "텍스트 입력"
    log_event("INFO", "slack_message_received", channel=channel_id, user=user_id, request=request, stage=job.get("stage"), job_id=job.get("job_id"))
    succeeded = handle_message(_STATE, _event_to_message(event))
    save_state(_STATE)
    log_event("INFO" if succeeded is not False else "ERROR", "slack_message_processed", channel=channel_id, user=user_id, result="accepted" if succeeded is not False else "failed", stage=job.get("stage"), job_id=job.get("job_id"))


def _dispatch_home_opened(event, client):
    if event.get("tab") == "home" and event.get("user"):
        publish_home(event["user"], client=client)


def _dispatch_action(body):
    channel_id = body.get("channel", {}).get("id") or ALLOWED_CHANNEL_ID
    user_id = body.get("user", {}).get("id")
    actions = body.get("actions") or []
    if not channel_id or not actions:
        return
    if not _allow(channel_id, user_id):
        _denied(channel_id)
        return
    job = chat_state(_STATE, channel_id)
    job["slack_thread_ts"] = body.get("message", {}).get("thread_ts") or body.get("message", {}).get("ts")
    data = actions[0].get("value", "")
    label = action_request_label(data)
    log_event("INFO", "slack_action_requested", channel=channel_id, user=user_id, action=data, request=label, stage=job.get("stage"), job_id=job.get("job_id"))
    try:
        send_message(channel_id, f"요청됨: {label}")
    except Exception as exc:
        # The acknowledgement is helpful context, but it must never prevent
        # the requested settings/status screen from opening.
        log_event("WARNING", "slack_action_notice_failed", channel=channel_id, user=user_id, action=data, error=exc)
    succeeded = handle_callback(_STATE, {"message": {"chat": {"id": channel_id}}, "data": data})
    save_state(_STATE)
    log_event(
        "INFO" if succeeded is not False else "ERROR",
        "slack_action_finished",
        channel=channel_id,
        user=user_id,
        action=data,
        result="handled" if succeeded is not False else "failed",
        stage=job.get("stage"),
        job_id=job.get("job_id"),
    )
    if body.get("view", {}).get("type") == "home" and user_id:
        publish_home(user_id)


def _dispatch_command(command, ack):
    ack()
    channel_id, user_id = command["channel_id"], command["user_id"]
    if not _allow(channel_id, user_id):
        _denied(channel_id)
        return
    job = chat_state(_STATE, channel_id)
    job["slack_thread_ts"] = None
    text = "/" + command["command"].rsplit("/", 1)[-1]
    if command.get("text"):
        text += " " + command["text"]
    command_name = text.split(None, 1)[0]
    log_event("INFO", "slack_slash_command_received", channel=channel_id, user=user_id, command=command_name, has_arguments=bool(command.get("text")), stage=job.get("stage"), job_id=job.get("job_id"))
    succeeded = handle_message(_STATE, {"chat": {"id": channel_id}, "text": text})
    save_state(_STATE)
    log_event("INFO" if succeeded is not False else "ERROR", "slack_slash_command_processed", channel=channel_id, user=user_id, command=command_name, result="accepted" if succeeded is not False else "failed", stage=job.get("stage"), job_id=job.get("job_id"))


def announce_startup_home():
    if not ALLOWED_CHANNEL_ID:
        print("Slack welcome screen skipped: SLACK_CHANNEL_ID is not set.")
        return False
    job = chat_state(_STATE, ALLOWED_CHANNEL_ID)
    job.pop("slack_thread_ts", None)
    send_home_screen(ALLOWED_CHANNEL_ID, "봇이 시작되었습니다. 아래에서 제작 방식을 선택하세요.", top_level=True)
    save_state(_STATE)
    return True


def main():
    global _STATE
    _require_tokens()
    _STATE = load_state()
    if clear_stale_busy_flags(_STATE):
        save_state(_STATE)
    try:
        from slack_bolt import App
        from slack_bolt.adapter.socket_mode import SocketModeHandler
    except ImportError as exc:
        raise SystemExit("Slack dependency is missing. Run: python3 -m pip install -r requirements-slack.txt") from exc

    app = App(token=BOT_TOKEN)
    app.event("message")(_dispatch_message)
    app.event("app_home_opened")(_dispatch_home_opened)
    app.action(re.compile(r"^workflow_action_\d+_\d+$"))(lambda ack, body: (ack(), _dispatch_action(body)))
    for name, _ in command_specs():
        app.command(f"/{name}")(_dispatch_command)
    try:
        announce_startup_home()
    except Exception as exc:
        print(f"Slack welcome screen failed: {exc}")
    SocketModeHandler(app, APP_TOKEN).start()


if __name__ == "__main__":
    main()
