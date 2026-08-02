import json
import os
import shlex
import signal
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from config_settings import (
    CONFIG_CATEGORIES,
    CONFIG_INPUT_ALIASES,
    SYSTEM_CONFIG_FIELDS,
    build_config_settings,
    display_setting_value,
    effective_setting_value,
    env_value,
    positive_int,
    positive_number,
    resolve_model_alias,
    safe_caption_style,
    safe_choice,
    set_config_value,
    signed_int,
    validate_setting_value,
)
from content_objectives import normalize_objective_type, objective_label
import job_state
import pipeline_orchestrator as _po
import pipeline_flow
import script_review
from script_runtime import speech_pace_profile

ADAPTERS_DIR = Path(__file__).resolve().parent / "adapters"
if str(ADAPTERS_DIR) not in sys.path:
    sys.path.insert(0, str(ADAPTERS_DIR))
import x_thread_adapter
import x_poster

TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
ALLOWED_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")
BASE_DIR = Path(os.environ.get("BASE_DIR", Path.cwd())).resolve()
WORK_DIR_BASE = Path(os.environ.get("WORK_DIR_BASE", BASE_DIR / "data" / "work"))
OUTPUT_DIR = Path(os.environ.get("OUTPUT_DIR", BASE_DIR / "data" / "output"))
STATE_PATH = Path(os.environ.get("TELEGRAM_STATE_PATH", BASE_DIR / "data" / "telegram_state.json"))
POLL_TIMEOUT = int(os.environ.get("TELEGRAM_POLL_TIMEOUT", "30"))
MAX_TEXT_PREVIEW = int(os.environ.get("TELEGRAM_MAX_TEXT_PREVIEW", "3500"))
POLL_ERROR_NOTIFY_INTERVAL = int(os.environ.get("TELEGRAM_POLL_ERROR_NOTIFY_INTERVAL", "1800"))
DEFAULT_CAPTION_FONT_SIZE = os.environ.get("TELEGRAM_DEFAULT_CAPTION_FONT_SIZE", "62")
DEFAULT_CAPTION_MARGIN_V = os.environ.get("TELEGRAM_DEFAULT_CAPTION_MARGIN_V", "60")
DEFAULT_CAPTION_STYLE = os.environ.get("TELEGRAM_DEFAULT_CAPTION_STYLE", os.environ.get("CAPTION_STYLE", "default"))
DEFAULT_CAPTION_MARGIN_H = os.environ.get("TELEGRAM_DEFAULT_CAPTION_MARGIN_H", "10")
DEFAULT_WEB_RESEARCH = os.environ.get("TELEGRAM_DEFAULT_WEB_RESEARCH", "true").lower() not in ("off", "0", "false", "no")

if not TOKEN:
    raise SystemExit("TELEGRAM_BOT_TOKEN is required")

API_URL = f"https://api.telegram.org/bot{TOKEN}"
STATE_LOCK = threading.Lock()
STOP_REQUESTED = False


def api(method, data=None, files=None):
    data = data or {}
    if files:
        boundary = f"----brain50{int(time.time() * 1000)}"
        body = bytearray()
        for key, value in data.items():
            body.extend(f"--{boundary}\r\n".encode())
            body.extend(f'Content-Disposition: form-data; name="{key}"\r\n\r\n'.encode())
            body.extend(str(value).encode("utf-8"))
            body.extend(b"\r\n")
        for key, path in files.items():
            path = Path(path)
            body.extend(f"--{boundary}\r\n".encode())
            body.extend(f'Content-Disposition: form-data; name="{key}"; filename="{path.name}"\r\n'.encode())
            body.extend(b"Content-Type: application/octet-stream\r\n\r\n")
            body.extend(path.read_bytes())
            body.extend(b"\r\n")
        body.extend(f"--{boundary}--\r\n".encode())
        request = Request(f"{API_URL}/{method}", data=bytes(body), headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
    else:
        encoded = urlencode(data).encode("utf-8")
        request = Request(f"{API_URL}/{method}", data=encoded)
    with urlopen(request, timeout=POLL_TIMEOUT + 10) as response:
        return json.loads(response.read().decode("utf-8"))


def send_message(chat_id, text):
    return api("sendMessage", {"chat_id": chat_id, "text": text})


def send_document(chat_id, path, caption=None):
    data = {"chat_id": chat_id}
    if caption:
        data["caption"] = caption
    return api("sendDocument", data, {"document": path})


def send_file_or_path(chat_id, path, caption=None, as_video=False):
    try:
        if as_video:
            data = {"chat_id": chat_id}
            if caption:
                data["caption"] = caption
            return api("sendVideo", data, {"video": path})
        return send_document(chat_id, path, caption)
    except Exception as exc:
        return send_message(chat_id, f"파일 전송 실패: {exc}\n서버에서 확인하세요: {path}")



def inline_keyboard(rows):
    return json.dumps({"inline_keyboard": rows}, ensure_ascii=False)


def button(text, callback_data):
    return {"text": text, "callback_data": callback_data}


def send_action_message(chat_id, text, rows):
    return api("sendMessage", {"chat_id": chat_id, "text": text, "reply_markup": inline_keyboard(rows)})


def editable_stage_info(stage, job_id):
    if not job_id:
        return None
    base = work_dir(job_id)
    mapping = {
        "await_script_approval": (base / "script.txt", "script.txt"),
        "await_caption_approval": (base / "subs.srt", "subs.srt"),
        "await_upload_meta_approval": (base / "video_meta.json", "video_meta.json"),
        "await_final_confirm": (base / "video_meta.json", "video_meta.json"),
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
        # Two ways to spend the rest of the run: never look again, or look
        # once more at the finished video.  Offered here because this is the
        # first moment the reviewer knows what the script actually says.
        rows.insert(1, [
            button("검수 후 최종 컨펌", f"review_mode:{stage}"),
            button("이후 자동 업로드", f"auto_upload:{stage}"),
        ])
    elif stage == "await_final_confirm":
        # Rewinds go through pipeline_flow so the stage order and the retry
        # budget stay defined in one place.
        rows = [
            [button("제목·설명 수정", f"edit:{stage}")],
            [button("다시 렌더", f"rewind:{stage}:render"),
             button("대본부터 다시", f"rewind:{stage}:script")],
            [button("승인(업로드)", f"approve:{stage}"), button("전체 취소", "cancel_all")],
        ]
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


def download_telegram_file(file_id, destination):
    info = api("getFile", {"file_id": file_id})
    file_path = info.get("result", {}).get("file_path")
    if not file_path:
        raise RuntimeError("텔레그램 파일 경로를 받지 못했습니다.")
    request = Request(f"https://api.telegram.org/file/bot{TOKEN}/{file_path}")
    with urlopen(request, timeout=POLL_TIMEOUT + 30) as response:
        destination.write_bytes(response.read())

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
    log_name = f"telegram_{Path(args[0]).name}_{int(time.time())}.log"
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
    return _po.preview_file(path, limit)


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
    """The script-review gate: everything needed to judge the script, together.

    Showing script.txt alone made this an uninformed approval -- the topic
    rationale, the papers behind it and the validation result each lived
    somewhere else.  In the two-gate flow nobody looks again until the
    finished video, so this one message has to carry all of it.
    """
    path = work_dir(job_id) / "script.txt"
    bundle = script_review.build_bundle(work_dir(job_id))

    flags = []
    if script_review.evidence_is_weak(bundle):
        flags.append("⚠ 논문 근거 없이 작성됨")
    if script_review.validation_failed(bundle):
        flags.append("⚠ 대본 검증 실패")
    header = "스크립트 생성 완료. 확인 후 승인하거나 수정하세요."
    if flags:
        header += "\n" + "\n".join(flags)

    body = script_review.render_text(bundle, script_limit=MAX_TEXT_PREVIEW // 2)
    send_approval_prompt(
        chat_id, "await_script_approval",
        f"{header}\n\n{body}"[:MAX_TEXT_PREVIEW],
    )
    if path.exists():
        send_file_or_path(chat_id, path, "script.txt")
    # The full status file stays attached so the reviewer can dig in.
    if script_review.evidence_is_weak(bundle):
        send_file_or_path(chat_id, pubmed_status_path(job_id), "pubmed_status.json")


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


def send_final_confirm(chat_id, job_id):
    """The second and last human gate: finished video, its metadata, and the
    X thread draft (already built by the x_thread stage) all at once.

    The six-gate flow reviewed the video and its title/description as two
    separate approvals.  Here they are one decision -- approve and it
    uploads to YouTube, then immediately posts the X thread -- so the
    reviewer sees exactly what goes out, on both platforms, in one pass.
    """
    video_path = output_file(job_id)
    if video_path.exists():
        send_file_or_path(chat_id, video_path, "최종 영상입니다.", as_video=True)
    else:
        send_message(chat_id, f"렌더 결과를 찾지 못했습니다: {video_path}")

    meta_path = work_dir(job_id) / "video_meta.json"
    meta = {}
    if meta_path.exists():
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            meta = {}
    text = (
        "최종 확인 단계입니다. 승인하면 YouTube에 비공개 업로드하고, 이어서 X 스레드도 자동 게시합니다.\n\n"
        f"제목: {meta.get('title', '')}\n\n"
        f"해시태그: {meta.get('hashtags', '')}\n\n"
        f"설명:\n{meta.get('description', '')}"
    )

    x_payload = x_thread_adapter.load_x_thread(work_dir(job_id))
    if x_payload and x_payload.get("tweets"):
        send_message(
            chat_id,
            f"X 스레드 초안 ({len(x_payload['tweets'])}개, 승인 시 업로드 직후 자동 게시):\n\n"
            + x_thread_adapter.render_text(x_payload),
        )
    else:
        send_message(chat_id, "X 스레드 초안을 만들지 못했습니다. 승인 후 /x_thread 로 직접 만들 수 있습니다.")

    send_approval_prompt(chat_id, "await_final_confirm", text[:MAX_TEXT_PREVIEW])
    if meta_path.exists():
        send_file_or_path(chat_id, meta_path, "video_meta.json")


def send_gate(chat_id, job, gate):
    """Dispatch a pipeline_flow gate to the message that presents it."""
    senders = {
        "script_review": send_script,
        "final_confirm": send_final_confirm,
    }
    sender = senders.get(gate)
    if sender is None:
        send_message(chat_id, f"알 수 없는 게이트입니다: {gate}")
        return
    sender(chat_id, job["job_id"])


def send_upload_meta(chat_id, job_id):
    meta_path = work_dir(job_id) / "video_meta.json"
    if not meta_path.exists():
        send_message(chat_id, f"video_meta.json을 찾지 못했습니다: {meta_path}")
        return
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    text = (
        "YouTube 업로드 메타데이터 확인 단계입니다.\n"
        f"제목: {meta.get('title', '')}\n\n"
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


def display_config_value(value):
    return _po.display_config_value(value)


def display_effective_model(job, job_key, value):
    source = "override" if job_key in job else "env/default"
    return f"{value} ({source})"


# ── 설정 키: /set 메뉴 또는 key=value로 저장, run_auto/run 실행 시 자동 적용
# (data table + validation logic shared with slack_bot.py via config_settings.py)
CONFIG_SETTINGS = build_config_settings(
    default_web_research=DEFAULT_WEB_RESEARCH,
    default_caption_font_size=DEFAULT_CAPTION_FONT_SIZE,
    default_caption_margin_v=DEFAULT_CAPTION_MARGIN_V,
    default_caption_margin_h=DEFAULT_CAPTION_MARGIN_H,
    default_caption_style=DEFAULT_CAPTION_STYLE,
)

_PRESERVED_KEYS = {setting["job_key"] for setting in CONFIG_SETTINGS.values()}


def _effective_setting_value(job, setting_id):
    return effective_setting_value(job, setting_id, CONFIG_SETTINGS)


def _display_setting_value(value):
    return display_setting_value(value)


def _validate_setting_value(setting_id, value):
    return validate_setting_value(setting_id, value, CONFIG_SETTINGS)


def _set_config_value(job, setting_id, value):
    return set_config_value(job, setting_id, value, CONFIG_SETTINGS)


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
    return _po.build_extra_env(job)


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
    return _po.media_duration_seconds(path)


def render_progress_ratio(progress_path, duration):
    return _po.render_progress_ratio(progress_path, duration)


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
    return _po.run_render(sys.modules[__name__], chat_id, job)


def _run_render_silent(chat_id, job, extra_env=None):
    return _po.run_render_silent(sys.modules[__name__], chat_id, job, extra_env)


def run_next_stage(chat_id, job):
    return _po.run_next_stage(sys.modules[__name__], chat_id, job)


def run_review_pipeline(chat_id, job):
    return _po.run_review_pipeline(sys.modules[__name__], chat_id, job)


def approve_review_gate(chat_id, job):
    return _po.approve_review_gate(sys.modules[__name__], chat_id, job)


def switch_to_review(chat_id, job):
    return _po.switch_to_review(sys.modules[__name__], chat_id, job)


def rewind_review(chat_id, job, target_stage):
    pipeline_flow.rewind_to(work_dir(job["job_id"]), target_stage)
    send_message(chat_id, f"{target_stage} 단계부터 다시 실행합니다.")
    return run_review_pipeline(chat_id, job)


def handle_run_review(chat_id, job, text):
    """Start a job in the two-gate flow: script review, then final confirm."""
    topic = text.split(maxsplit=1)[1].strip() if len(text.split(maxsplit=1)) > 1 else ""
    if not topic:
        send_message(chat_id,
            "주제를 입력하세요.\n"
            "예: /run_review 오메가3가 정말 뇌에 좋을까?\n\n"
            "대본 검수와 최종 승인 두 번만 확인하면 나머지는 자동 진행합니다."
        )
        return

    job_id = new_job_id("review")
    settings = _preserve_settings(job)
    busy = job.get("busy")
    job.clear()
    job.update({
        "job_id": job_id, "topic": topic,
        "approval_required": True, "mode": job_state.MODE_REVIEW,
    })
    job.update(settings)
    if busy:
        job["busy"] = busy

    send_message(chat_id,
        "2게이트 실행 시작 (대본 검수 → 최종 승인)\n"
        "JOB_ID: " + job_id + "\n"
        "주제: " + topic + "\n" +
        _settings_summary(job)
    )
    return run_review_pipeline(chat_id, job)

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
    send_message(chat_id, "스크립트/타이틀 승인 완료. 이후 단계를 YouTube 업로드까지 자동 진행합니다.")
    return _po.run_to_completion(sys.modules[__name__], chat_id, job)


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


def handle_run_goal(chat_id, job, text):
    parts = text.split(maxsplit=2)
    if len(parts) < 2:
        send_message(chat_id, "목표를 입력하세요. 예: /run_goal subscriber_growth 수면")
        return
    try:
        objective = normalize_objective_type(parts[1])
    except ValueError as exc:
        send_message(chat_id, f"목표 입력 오류: {exc}")
        return
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
        "python3", str(BASE_DIR / "src" / "common" / "0_topic_plan.py"), "plan",
        "--objective", objective, "--job-id", job_id, "--output", str(plan_path),
    ]
    if seed:
        args.extend(["--seed", seed])
    send_message(chat_id, f"목표 기반 기획 시작: {objective_label(objective)}" + (f" / 씨드: {seed}" if seed else ""))
    run_command(args, job_id, seed, extra_env=_build_extra_env(job))
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    goal = plan.get("objective") or {}
    planning = plan.get("planning") or {}
    job["topic"] = plan.get("topic") or seed
    job["plan_id"] = goal.get("plan_id")
    decision = goal.get("decision", "manual_review")
    topic_label = "선정 주제" if decision not in ("manual_review", "rejected") else "최상위 검토 후보"
    closest = str(goal.get("closest_existing_title") or "").strip()
    details = [
        f"목표: {objective_label(objective)}",
        f"상태: {decision}",
        f"{topic_label}: {job['topic'] or '(없음)'}",
        f"판정 이유: {goal.get('reason', '점수와 위험 검토 결과')}",
        f"중복도: {float(goal.get('duplicate_similarity') or 0):.2f} / 차단 기준 {float(goal.get('duplicate_threshold') or planning.get('duplicate_threshold') or 0):.2f}",
        f"제외된 중복 후보: {int(planning.get('duplicates_rejected') or 0)}개",
        f"AI 단계: Planner {planning.get('planner_status', '-')} / Critic {planning.get('critic_status', '-')}",
        f"Claude 비용: ${float(planning.get('claude_cost_usd') or 0):.6f}",
        f"주의: 확신도 {float(goal.get('confidence') or 0):.2f}; 성과를 보장하지 않습니다.",
    ]
    if closest:
        details.insert(5, f"가장 가까운 기존 제목: {closest}")
    send_message(chat_id, "\n".join(details))
    send_file_or_path(chat_id, plan_path, "topic_plan.json")
    if int(planning.get("candidate_count") or 0) == 0:
        job["stage"] = "await_goal_review"
        send_message(chat_id, f"이 씨드로는 만들 수 있는 후보가 없어 자동 제작을 중단했습니다.\n{goal.get('reason', '수동 검토가 필요합니다.')}")
        return
    if decision in ("manual_review", "rejected"):
        send_message(chat_id, "확신도가 낮거나 위험 신호가 있지만, 목표 기반 자동 제작은 계속 진행합니다.")
    run_script_generation(
        chat_id, job,
        [str(BASE_DIR / "sh" / "common" / "0_script.sh"), "--topic-json", str(plan_path)],
    )


def handle_goal_query(chat_id, job, command):
    job_id = job.get("job_id") or "goal_status"
    output = run_command(
        ["python3", str(BASE_DIR / "src" / "common" / "0_topic_plan.py"), command], job_id,
        job.get("topic"), extra_env=_build_extra_env(job),
    )
    send_message(chat_id, output[-MAX_TEXT_PREVIEW:] or "목표 기획 이력이 없습니다.")


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
        # stage stays None: nothing has run yet, so the flow starts at script.
        "approval_required": False, "stage": None,
        # evidence_probe now widens the query before giving up, so a miss here
        # means the literature really is absent -- publishing anyway is how an
        # unsourced video shipped. Set ALLOW_NO_PUBMED=1 to accept that risk.
        "allow_no_pubmed": os.environ.get("ALLOW_NO_PUBMED", "").strip().lower() in ("1", "true", "yes"),
    })
    job.update(settings)
    if busy:
        job["busy"] = busy

    send_message(chat_id,
        "자동 실행 시작\n"
        "JOB_ID: " + job_id + "\n"
        "주제: " + topic + "\n" +
        _settings_summary(job)
    )

    return _po.run_to_completion(sys.modules[__name__], chat_id, job)

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
        run_command([str(BASE_DIR / "sh" / "common" / "0_script.sh"), "--trend", topic], job_id, topic, extra_env=_build_extra_env(job))
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
        run_script_generation(chat_id, job, [str(BASE_DIR / "sh" / "common" / "0_script.sh"), topic])

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
    run_script_generation(chat_id, job, [str(BASE_DIR / "sh" / "common" / "0_script.sh"), topic])


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
    run_script_generation(chat_id, job, [str(BASE_DIR / "sh" / "common" / "0_script.sh"), "--allow-no-pubmed", *pending])

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
    run_script_generation(chat_id, job, [str(BASE_DIR / "sh" / "common" / "0_script.sh"), "--trend-choice", choice])



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
        download_telegram_file(doc["file_id"], path)
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
    try:
        api("answerCallbackQuery", {"callback_query_id": callback.get("id", "")})
    except Exception:
        pass
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
            if job.get("mode") == job_state.MODE_REVIEW:
                start_background_task(state, chat_id, job, "승인 후 진행",
                                      lambda: approve_review_gate(chat_id, job))
            else:
                start_background_task(state, chat_id, job, "현재 단계 실행", lambda: run_next_stage(chat_id, job))
        elif data.startswith("review_mode:"):
            expected_stage = data.split(":", 1)[1]
            if job.get("stage") != expected_stage:
                send_message(chat_id, f"이전 단계 버튼입니다. 현재 단계는 {job.get('stage')}입니다.")
                return
            start_background_task(state, chat_id, job, "검수 후 최종 컨펌",
                                  lambda: switch_to_review(chat_id, job))
        elif data.startswith("rewind:"):
            _, expected_stage, target_stage = data.split(":", 2)
            if job.get("stage") != expected_stage:
                send_message(chat_id, f"이전 단계 버튼입니다. 현재 단계는 {job.get('stage')}입니다.")
                return
            start_background_task(state, chat_id, job, f"{target_stage} 단계부터 재실행",
                                  lambda: rewind_review(chat_id, job, target_stage))
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
        "tts": ("youtube/1_tts.sh", "await_tts_approval", send_tts),
        "caption": ("youtube/1_caption.sh", "await_caption_approval", send_caption),
        "broll": ("youtube/1_broll.sh", "await_broll_approval", send_broll),
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


def handle_x_thread(chat_id, job):
    if not job.get("job_id"):
        send_message(chat_id, "진행 중인 작업이 없습니다.")
        return
    payload = x_thread_adapter.build_x_thread(work_dir(job["job_id"]))
    if payload is None:
        send_message(chat_id, "content_package.json이 없습니다. 먼저 스크립트 생성을 완료하세요.")
        return
    send_message(
        chat_id,
        f"X 스레드 초안 {len(payload['tweets'])}개:\n\n{x_thread_adapter.render_text(payload)}\n\n"
        "게시하려면 /x_post 를 입력하세요.",
    )


def handle_x_post(chat_id, job):
    if not job.get("job_id"):
        send_message(chat_id, "진행 중인 작업이 없습니다.")
        return
    try:
        payload = x_poster.post_thread(work_dir(job["job_id"]))
    except RuntimeError as exc:
        send_message(chat_id, f"X 게시 실패: {exc}")
        return
    send_message(
        chat_id,
        f"X 게시 완료: {payload.get('thread_url')} ({len(payload.get('tweet_ids') or [])}개 트윗)",
    )


def command_specs():
    return [
        ("run", "승인형 파이프라인 시작"),
        ("run_review", "대본 검수 + 최종 승인 2회만 확인"),
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
        ("status", "현재 상태 확인"),
        ("x_thread", "X(트위터) 스레드 초안 생성"),
        ("x_post", "X(트위터)에 실제 게시"),
        ("cancel", "전체 작업 취소"),
        ("help", "명령어 도움말"),
    ]


def register_bot_commands():
    commands = json.dumps([
        {"command": command, "description": description}
        for command, description in command_specs()
    ], ensure_ascii=False)
    return api("setMyCommands", {"commands": commands})


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
        "/x_thread  <- X 스레드 초안 생성/미리보기",
        "/x_post  <- 미리 만든 X 스레드를 실제 게시",
        "/set  <- 카테고리별 설정 메뉴",
        "/set_all  <- 현재 전체 설정 보기",
        "/set font_size=62 web=off  <- 기존 빠른 입력도 지원",
        "/set reset  <- 저장한 override 전체 초기화",
        "/run_review 오메가3가 정말 뇌에 좋을까?  <- 대본 검수 + 최종 승인 2회만",
        "/run_auto 오메가3가 정말 뇌에 좋을까?",
        "/run_goal subscriber_growth 수면",
        "/goal_status | /goal_report",
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
        if is_busy(job) and not (
            text.startswith("/status") or text.startswith("/goal_status")
            or text.startswith("/goal_report")
        ):
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
        elif text == "/run_goal" or text.startswith("/run_goal "):
            start_background_task(state, chat_id, job, "목표 기반 기획", lambda: handle_run_goal(chat_id, job, text))
        elif text.startswith("/goal_status"):
            handle_goal_query(chat_id, job, "status")
        elif text.startswith("/goal_report"):
            handle_goal_query(chat_id, job, "report")
        elif text.startswith("/run_review"):
            start_background_task(state, chat_id, job, "2게이트 실행", lambda: handle_run_review(chat_id, job, text))
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
        elif text.startswith("/x_thread"):
            start_background_task(state, chat_id, job, "X 스레드 초안 생성", lambda: handle_x_thread(chat_id, job))
        elif text.startswith("/x_post"):
            start_background_task(state, chat_id, job, "X 게시", lambda: handle_x_post(chat_id, job))
        elif text.startswith("/status"):
            handle_status(chat_id, job)
        elif text.startswith("/cancel"):
            job.clear()
            send_message(chat_id, "전체 작업을 취소했습니다.")
        else:
            send_message(chat_id, help_text())
    except Exception as exc:
        send_message(chat_id, f"오류: {exc}")


def startup_message():
    return "\n".join([
        "Brain50 Telegram bot started.",
        f"BASE_DIR: {BASE_DIR}",
        "",
        help_text(),
    ])


def shutdown_message(signum=None):
    label = f"signal {signum}" if signum else "shutdown"
    return f"Brain50 Telegram bot stopped. bye bye. ({label})"


def request_shutdown(signum, frame):
    global STOP_REQUESTED
    STOP_REQUESTED = True
    if ALLOWED_CHAT_ID:
        try:
            send_message(ALLOWED_CHAT_ID, shutdown_message(signum))
        except Exception:
            pass



def is_transient_poll_error(exc):
    text = str(exc).lower()
    transient_markers = (
        "timed out",
        "timeout",
        "connection reset by peer",
        "remote end closed connection without response",
        "temporarily unavailable",
        "connection aborted",
        "network is unreachable",
    )
    return any(marker in text for marker in transient_markers)


def poll_error_backoff(consecutive_errors):
    return min(60, 5 + max(consecutive_errors - 1, 0) * 5)

def poll_updates(offset):
    params = {"timeout": POLL_TIMEOUT, "offset": offset}
    query = urlencode(params)
    with urlopen(f"{API_URL}/getUpdates?{query}", timeout=POLL_TIMEOUT + 10) as response:
        return json.loads(response.read().decode("utf-8"))


def main():
    state = load_state()
    stale_busy_chats = clear_stale_busy_flags(state)
    if stale_busy_chats:
        save_state(state)
        print(f"[INFO] cleared stale busy flags on startup: {stale_busy_chats}", flush=True)
    send_to = ALLOWED_CHAT_ID
    signal.signal(signal.SIGTERM, request_shutdown)
    signal.signal(signal.SIGINT, request_shutdown)
    try:
        register_bot_commands()
    except Exception:
        pass
    if send_to:
        send_message(send_to, startup_message())
    consecutive_poll_errors = 0
    last_poll_error_notice_at = 0
    while not STOP_REQUESTED:
        try:
            data = poll_updates(state.get("offset", 0))
            consecutive_poll_errors = 0
            for update in data.get("result", []):
                state["offset"] = update["update_id"] + 1
                callback = update.get("callback_query")
                if callback:
                    handle_callback(state, callback)
                    save_state(state)
                message = update.get("message") or update.get("edited_message")
                if message:
                    handle_message(state, message)
                    save_state(state)
            save_state(state)
        except Exception as exc:
            consecutive_poll_errors += 1
            if is_transient_poll_error(exc):
                print(f"[WARN] transient polling error: {exc}", flush=True)
            else:
                now = time.time()
                if send_to and now - last_poll_error_notice_at >= POLL_ERROR_NOTIFY_INTERVAL:
                    try:
                        send_message(send_to, f"Bot polling error: {exc}")
                        last_poll_error_notice_at = now
                    except Exception:
                        pass
                print(f"[ERROR] polling error: {exc}", flush=True)
            time.sleep(poll_error_backoff(consecutive_poll_errors))


if __name__ == "__main__":
    main()
