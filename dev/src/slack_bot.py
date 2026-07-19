import json
import os
import shlex
import subprocess
import threading
import time
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
STOP_REQUESTED = False
_STATE = {"chats": {}}
MAX_BLOCK_TEXT = 3000


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
    for row in rows:
        elements = [
            {"type": "button", "action_id": "workflow_action", "text": {"type": "plain_text", "text": item["text"][:75]}, "value": item["callback_data"]}
            for item in row
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


def approval_buttons(stage):
    rows = [[button("승인", f"approve:{stage}"), button("전체 취소", "cancel_all")]]
    previous = previous_stage_button(stage)
    if previous:
        rows.insert(0, [previous])
    if stage == "await_script_approval":
        rows.insert(0, [
            button("본문 수정", f"edit_body:{stage}"),
            button("타이틀 수정", f"edit_title_menu:{stage}"),
        ])
        rows.insert(1, [button("이후 자동 업로드", f"auto_upload:{stage}")])
    elif stage in ("await_caption_approval", "await_upload_meta_approval"):
        rows.insert(0, [button("수정", f"edit:{stage}")])
    if stage == "await_tts_approval":
        rows.insert(0, [button("스크립트 수정", "back:await_tts_approval:await_script_approval"), button("TTS 재생성", f"rerun:{stage}:tts")])
    elif stage == "await_caption_approval":
        rows.insert(1, [button("자막 재생성", f"rerun:{stage}:caption")])
    elif stage == "await_broll_approval":
        rows.insert(0, [button("B-roll 재생성", f"rerun:{stage}:broll")])
    elif stage == "await_render_approval":
        rows.insert(0, [button("렌더 다시 조정", f"back:{stage}:await_render_config")])
    return rows


def previous_stage_button(stage):
    labels = {
        "await_tts_approval": "스크립트로 돌아가기",
        "await_caption_approval": "TTS로 돌아가기",
        "await_broll_approval": "자막으로 돌아가기",
        "await_render_config": "B-roll로 돌아가기",
        "await_render_approval": "렌더 설정으로 돌아가기",
        "await_upload_meta_approval": "최종 영상으로 돌아가기",
    }
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
    return button(labels[stage], f"back:{stage}:{target}")


def send_approval_prompt(chat_id, stage, text):
    return send_action_message(chat_id, text, approval_buttons(stage))


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
    return f"현재 {label} 진행 중입니다. 완료 메시지가 올 때까지 다른 입력은 처리하지 않습니다."


def is_busy(job):
    return bool(job.get("busy"))


def start_background_task(state, chat_id, job, label, target):
    if is_busy(job):
        send_message(chat_id, busy_message(job))
        return
    job["busy"] = label
    save_state(state)
    send_message(chat_id, f"진행 중입니다: {label}")

    def runner():
        try:
            target()
        except Exception as exc:
            send_message(chat_id, f"오류: {exc}")
        finally:
            current = chat_state(state, chat_id)
            current.pop("busy", None)
            save_state(state)

    threading.Thread(target=runner, daemon=True).start()


def new_job_id(prefix="tg"):
    return f"{prefix}_{datetime.now().strftime('%Y%m%d_%H%M%S')}"


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
    env = os.environ.copy()
    env["JOB_ID"] = job_id
    if topic:
        env["TOPIC"] = topic
    if extra_env:
        env.update(extra_env)
    result = subprocess.run(args, cwd=BASE_DIR, env=env, text=True, capture_output=True)
    log_dir = work_dir(job_id)
    log_dir.mkdir(parents=True, exist_ok=True)
    log_name = f"slack_{Path(args[0]).name}_{int(time.time())}.log"
    (log_dir / log_name).write_text((result.stdout or "") + "\n" + (result.stderr or ""), encoding="utf-8")
    if result.returncode != 0:
        tail = (result.stderr or result.stdout or "")[-1600:]
        hint = ""
        if "ReadTimeout" in tail and "api.anthropic.com" in tail:
            hint = "\n\n진단: Claude API 응답이 설정된 시간 안에 끝나지 않았습니다. 주제 문제가 아니라 네트워크 지연이나 응답 생성 지연일 가능성이 큽니다. 잠시 후 같은 /pick 번호를 다시 실행하거나 /retry 새 주제로 재시도하세요. 반복되면 CLAUDE_TIMEOUT 값을 더 크게 설정하세요."
        elif "api.anthropic.com" in tail:
            hint = "\n\n진단: Claude API 호출 단계에서 실패했습니다. 로그 파일의 HTTP 상태와 메시지를 확인하세요."
        raise RuntimeError(f"명령 실패: {' '.join(shlex.quote(a) for a in args)}\n로그: {log_dir / log_name}{hint}\n\n{tail}")
    return result.stdout


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
        msg,
        [
            [button("B-roll로 돌아가기", "back:await_render_config:await_broll_approval")],
            [button("현재값으로 렌더", "approve:await_render_config")],
            [button("기본 스타일",  "render:await_render_config:62:60:default"),
             button("중앙 노랑",  "render:await_render_config:72:0:center-yellow")],
            [button("전체 취소", "cancel_all")],
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
    return send_action_message(chat_id, text, rows)


def send_config_category(chat_id, job, category_id, notice=None):
    job.pop("config_edit_key", None)
    label, description = _category_label(category_id)
    if category_id == "system":
        lines = [f"{label}\n{description}"]
        lines.extend(f"{name}={env_value(name, default)}" for name, default in SYSTEM_CONFIG_FIELDS)
        if notice:
            lines.insert(0, notice)
        return send_action_message(chat_id, "\n".join(lines), [[button("← 설정 상자", "cfg:root")]])

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
    rows.append([button("← 설정 상자", "cfg:root")])
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
        send_message(chat_id, "업로드 완료. YouTube Studio에서 비공개 영상을 확인하세요.")
    else:
        send_message(chat_id, f"승인할 단계가 없습니다. 현재 단계: {stage}")

def run_remaining_to_upload(chat_id, job):
    job_id = job.get("job_id")
    topic = job.get("topic")
    if not job_id:
        send_message(chat_id, "진행 중인 작업이 없습니다.")
        return
    if job.get("stage") != "await_script_approval":
        send_message(chat_id, f"이후 자동 업로드는 스크립트 승인 단계에서만 가능합니다. 현재 단계: {job.get('stage')}")
        return

    job.pop("edit_target", None)
    job.pop("edit_stage", None)
    job.pop("title_edit_field", None)
    job.pop("title_edit_stage", None)
    header = load_frame_header(job_id)
    sync_frame_header_to_job(job, header)
    extra_env = _build_extra_env(job)
    job["approval_required"] = False
    job["stage"] = "running_after_script_auto"
    send_message(chat_id, "스크립트/타이틀 승인 완료. 이후 단계를 YouTube 업로드까지 자동 진행합니다.")

    send_message(chat_id, "1/5 TTS 음성 생성 중...")
    run_command([str(BASE_DIR / "sh" / "1_tts.sh")], job_id, topic, extra_env=extra_env)
    send_message(chat_id, "1/5 TTS 완료")

    send_message(chat_id, "2/5 자막 생성 중...")
    run_command([str(BASE_DIR / "sh" / "1_caption.sh")], job_id, topic, extra_env=extra_env)
    send_message(chat_id, "2/5 자막 완료")

    send_message(chat_id, "3/5 B-roll 수집 중...")
    run_command([str(BASE_DIR / "sh" / "1_broll.sh")], job_id, topic, extra_env=extra_env)
    send_message(chat_id, "3/5 B-roll 완료")

    send_message(chat_id, "4/5 렌더링 중...")
    _run_render_silent(chat_id, job, extra_env)
    send_message(chat_id, "4/5 렌더링 완료")

    send_message(chat_id, "5/5 YouTube 비공개 업로드 중...")
    run_command([str(BASE_DIR / "sh" / "3_upload.sh")], job_id, topic)
    job["stage"] = "done"
    send_message(chat_id, "완료! YouTube Studio에서 비공개 영상을 확인하세요.")


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
            send_message(chat_id, pubmed_retry_message(status))
            send_file_or_path(chat_id, pubmed_status_path(job_id), "pubmed_status.json")
            return False
        raise


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
    send_message(chat_id, "완료! YouTube Studio에서 비공개 영상을 확인하세요.")

def handle_run(chat_id, job, text, trend=False):
    topic = text.split(maxsplit=1)[1].strip() if len(text.split(maxsplit=1)) > 1 else ""
    if not topic:
        send_message(chat_id, "주제를 입력하세요. 예: /run 오메가3가 정말 뇌에 좋을까?")
        return
    job_id = new_job_id("trend" if trend else "tg")
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
        lines = ["후보를 선택하세요: /pick 번호"]
        for i, item in enumerate(payload.get("candidates", []), start=1):
            lines.append(f"{i}. {item.get('keyword')} ({', '.join(item.get('sources', []))})")
        send_message(chat_id, "\n".join(lines))
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
        f"수정 모드입니다. 아래 {name} 파일을 열어 필요한 부분만 고친 뒤, 수정한 파일을 텔레그램으로 다시 보내세요. "
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
        "타이틀 수정 단계입니다.\n"
        f"현재 주제목: {title}\n"
        f"현재 부제목: {subtitle}\n\n"
        "수정할 항목을 선택하세요. 선택 후 다음 메시지에 새 문구를 보내면 띄어쓰기 포함 그대로 저장됩니다.",
        [
            [button("주제목 수정", "edit_title_field:await_script_approval:title")],
            [button("부제목 수정", "edit_title_field:await_script_approval:subtitle")],
            [button("본문 수정", "edit_body:await_script_approval")],
            [button("이후 자동 업로드", "auto_upload:await_script_approval")],
            [button("승인", "approve:await_script_approval"), button("전체 취소", "cancel_all")],
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
    if is_busy(job):
        send_message(chat_id, busy_message(job))
        return
    try:
        if data == "cfg:root":
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
        elif data == "cancel_all":
            job.clear()
            send_message(chat_id, "전체 작업을 취소했습니다.")
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
        elif data.startswith("auto_upload:"):
            expected_stage = data.split(":", 1)[1]
            if job.get("stage") != expected_stage:
                send_message(chat_id, f"이전 단계 버튼입니다. 현재 단계는 {job.get('stage')}입니다.")
                return
            start_background_task(state, chat_id, job, "이후 자동 업로드", lambda: run_remaining_to_upload(chat_id, job))
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
        send_message(chat_id, f"오류: {exc}")
    finally:
        # Inline buttons mutate the per-chat job directly.  Keep those
        # changes durable just like the /set command path.
        save_state(state)

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
    if not job:
        send_message(chat_id, "진행 중인 작업이 없습니다.")
        return
    send_message(chat_id, json.dumps(job, ensure_ascii=False, indent=2))


def command_specs():
    return [
        ("run", "승인형 파이프라인 시작"),
        ("set", "카테고리별 설정 메뉴 열기"),
        ("set_all", "현재 전체 설정 한 번에 보기"),
        ("run_auto", "승인 없이 전체 파이프라인 실행"),
        ("trend", "트렌드 후보 조회"),
        ("pick", "트렌드 후보 선택"),
        ("approve", "현재 산출물 승인"),
        ("edit", "현재 텍스트 산출물 수정"),
        ("retry", "PubMed 실패 후 새 주제 재시도"),
        ("proceed", "PubMed 실패 후 근거 부족 상태로 진행"),
        ("rerun", "tts/caption/broll 재생성"),
        ("render", "자막 렌더 설정 변경"),
        ("status", "현재 상태 확인"),
        ("cancel", "전체 작업 취소"),
        ("help", "명령어 도움말"),
    ]


def help_text():
    return "\n".join([
        "명령어",
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
        "/status",
        "/cancel",
        "",
        "흐름: run/trend -> approve 반복 -> 렌더 확인 -> 메타데이터 승인 -> 비공개 업로드",
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
        if is_busy(job) and not text.startswith("/status"):
            send_message(chat_id, busy_message(job))
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
            send_message(chat_id, help_text())
        elif text.startswith("/start") or text.startswith("/help"):
            send_message(chat_id, help_text())
        elif text.startswith("/set_all") or text.startswith("/setall"):
            job.pop("config_edit_key", None)
            send_message(chat_id, config_summary(job))
            save_state(state)
        elif text.startswith("/set"):
            handle_set(chat_id, job, text)
            save_state(state)
        elif text.startswith("/run_auto "):
            start_background_task(state, chat_id, job, "자동 실행", lambda: handle_run_auto(chat_id, job, text))
        elif text.startswith("/run "):
            start_background_task(state, chat_id, job, "스크립트 생성", lambda: handle_run(chat_id, job, text, trend=False))
        elif text.startswith("/trend "):
            start_background_task(state, chat_id, job, "트렌드 조회", lambda: handle_run(chat_id, job, text, trend=True))
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
        elif text.startswith("/status"):
            handle_status(chat_id, job)
        elif text.startswith("/cancel"):
            job.clear()
            send_message(chat_id, "전체 작업을 취소했습니다.")
        else:
            send_message(chat_id, help_text())
    except Exception as exc:
        send_message(chat_id, f"오류: {exc}")



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
    handle_message(_STATE, _event_to_message(event))
    save_state(_STATE)


def _dispatch_action(body):
    channel_id = body.get("channel", {}).get("id")
    user_id = body.get("user", {}).get("id")
    actions = body.get("actions") or []
    if not channel_id or not actions:
        return
    if not _allow(channel_id, user_id):
        _denied(channel_id)
        return
    job = chat_state(_STATE, channel_id)
    job["slack_thread_ts"] = body.get("message", {}).get("thread_ts") or body.get("message", {}).get("ts")
    handle_callback(_STATE, {"message": {"chat": {"id": channel_id}}, "data": actions[0].get("value", "")})
    save_state(_STATE)


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
    handle_message(_STATE, {"chat": {"id": channel_id}, "text": text})
    save_state(_STATE)


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
    app.action("workflow_action")(lambda ack, body: (ack(), _dispatch_action(body)))
    for name, _ in command_specs():
        app.command(f"/{name}")(_dispatch_command)
    SocketModeHandler(app, APP_TOKEN).start()


if __name__ == "__main__":
    main()
