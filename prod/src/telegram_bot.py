import json
import os
import shlex
import signal
import subprocess
import threading
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen

TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
ALLOWED_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")
BASE_DIR = Path(os.environ.get("BASE_DIR", Path.cwd())).resolve()
WORK_DIR_BASE = Path(os.environ.get("WORK_DIR_BASE", BASE_DIR / "data" / "work"))
OUTPUT_DIR = Path(os.environ.get("OUTPUT_DIR", BASE_DIR / "data" / "output"))
STATE_PATH = Path(os.environ.get("TELEGRAM_STATE_PATH", BASE_DIR / "data" / "telegram_state.json"))
POLL_TIMEOUT = int(os.environ.get("TELEGRAM_POLL_TIMEOUT", "30"))
MAX_TEXT_PREVIEW = int(os.environ.get("TELEGRAM_MAX_TEXT_PREVIEW", "3500"))
POLL_ERROR_NOTIFY_INTERVAL = int(os.environ.get("TELEGRAM_POLL_ERROR_NOTIFY_INTERVAL", "1800"))

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
    }
    return mapping.get(stage)


def approval_buttons(stage):
    rows = [[button("승인", f"approve:{stage}"), button("전체 취소", "cancel_all")]]
    previous = previous_stage_button(stage)
    if previous:
        rows.insert(0, [previous])
    if stage in ("await_script_approval", "await_caption_approval", "await_upload_meta_approval"):
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
    font_size = job.get("caption_font_size", os.environ.get("CAPTION_FONT_SIZE", "20"))
    margin_v = job.get("caption_margin_v", os.environ.get("CAPTION_MARGIN_V", "55"))
    send_action_message(
        chat_id,
        "렌더 설정 확인 단계입니다.\n"
        f"현재값: font_size={font_size}, margin_v={margin_v}\n"
        "값 조정 후 렌더: /render font_size=22 margin_v=55",
        [
            [button("B-roll로 돌아가기", "back:await_render_config:await_broll_approval")],
            [button("현재값으로 렌더", "approve:await_render_config")],
            [button("font 22 / margin 180", "render:await_render_config:22:180"), button("font 24 / margin 160", "render:await_render_config:24:160")],
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
    if not str(value).isdigit() or int(value) <= 0:
        raise ValueError(f"{name}은 양의 정수로 입력하세요: {value}")
    return str(value)




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
    font_size = str(job.get("caption_font_size", os.environ.get("CAPTION_FONT_SIZE", "20")))
    margin_v = str(job.get("caption_margin_v", os.environ.get("CAPTION_MARGIN_V", "55")))
    args += ["--font-size", font_size, "--margin-v", margin_v]
    send_message(chat_id, f"렌더링 시작: font_size={font_size}, margin_v={margin_v}")
    stop_progress = threading.Event()
    progress_thread = start_render_progress(chat_id, job_id, stop_progress)
    try:
        run_command(args, job_id, job.get("topic"))
    finally:
        stop_progress.set()
        if progress_thread:
            progress_thread.join(timeout=1)
    send_message(chat_id, "렌더링 진행률: 완료")
    job["stage"] = "await_render_approval"
    send_rendered_video(chat_id, job_id)


def run_next_stage(chat_id, job):
    job_id = job["job_id"]
    topic = job.get("topic")
    stage = job.get("stage")

    if stage == "await_script_approval":
        send_message(chat_id, "TTS 생성 시작")
        run_command([str(BASE_DIR / "sh" / "1_tts.sh")], job_id, topic)
        job["stage"] = "await_tts_approval"
        send_tts(chat_id, job_id)
    elif stage == "await_tts_approval":
        send_message(chat_id, "자막 생성 시작")
        run_command([str(BASE_DIR / "sh" / "1_caption.sh")], job_id, topic)
        job["stage"] = "await_caption_approval"
        send_caption(chat_id, job_id)
    elif stage == "await_caption_approval":
        send_message(chat_id, "B-roll 생성 시작")
        run_command([str(BASE_DIR / "sh" / "1_broll.sh")], job_id, topic)
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
        run_command([str(BASE_DIR / "sh" / "3_upload.sh")], job_id, topic)
        job["stage"] = "done"
        send_message(chat_id, "업로드 완료. YouTube Studio에서 비공개 영상을 확인하세요.")
    else:
        send_message(chat_id, f"승인할 단계가 없습니다. 현재 단계: {stage}")

def run_script_generation(chat_id, job, args):
    job_id = job["job_id"]
    try:
        run_command(args, job_id, job.get("topic"))
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
        send_message(chat_id, "주제를 입력하세요. 예: /run_auto 오메가3가 정말 뇌에 좋을까?")
        return
    job_id = new_job_id("auto")
    busy = job.get("busy")
    job.clear()
    job.update({"job_id": job_id, "topic": topic, "approval_required": False, "stage": "running_auto"})
    if busy:
        job["busy"] = busy
    send_message(chat_id, f"자동 실행 시작: JOB_ID={job_id}")
    run_command([str(BASE_DIR / "run.sh"), topic, job_id], job_id, topic)
    job["stage"] = "done"
    send_message(chat_id, "자동 실행 완료. YouTube Studio에서 비공개 영상을 확인하세요.")

def handle_run(chat_id, job, text, trend=False):
    topic = text.split(maxsplit=1)[1].strip() if len(text.split(maxsplit=1)) > 1 else ""
    if not topic:
        send_message(chat_id, "주제를 입력하세요. 예: /run 오메가3가 정말 뇌에 좋을까?")
        return
    job_id = new_job_id("trend" if trend else "tg")
    busy = job.get("busy")
    job.clear()
    job.update({"job_id": job_id, "topic": topic, "approval_required": True})
    if busy:
        job["busy"] = busy
    if trend:
        job["stage"] = "await_trend_choice"
        send_message(chat_id, f"트렌드 후보 조회 시작: {topic}")
        run_command([str(BASE_DIR / "sh" / "0_script.sh"), "--trend", topic], job_id, topic)
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
    job["edit_target"] = str(path)
    job["edit_stage"] = stage
    send_message(
        chat_id,
        f"수정 모드입니다. 아래 {name} 파일을 열어 필요한 부분만 고친 뒤, 수정한 파일을 텔레그램으로 다시 보내세요. "
        "짧은 수정이면 다음 메시지에 전체 수정본을 보내도 됩니다.",
    )
    if path.exists():
        send_file_or_path(chat_id, path, f"수정용 원본: {name}")


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
        if data.startswith("approve:"):
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
        elif data.startswith("back:"):
            _, expected_stage, target_stage = data.split(":", 2)
            if job.get("stage") != expected_stage:
                send_message(chat_id, f"이전 단계 버튼입니다. 현재 단계는 {job.get('stage')}입니다.")
                return
            go_back_to_stage(chat_id, job, target_stage)
        elif data.startswith("render:"):
            _, expected_stage, font_size, margin_v = data.split(":")
            if job.get("stage") != expected_stage:
                send_message(chat_id, f"이전 단계 버튼입니다. 현재 단계는 {job.get('stage')}입니다.")
                return
            job["caption_font_size"] = positive_int(font_size, "font_size")
            job["caption_margin_v"] = positive_int(margin_v, "margin_v")
            start_background_task(state, chat_id, job, "렌더링", lambda: run_render(chat_id, job))
        elif data.startswith("rerun:"):
            _, expected_stage, target = data.split(":", 2)
            if job.get("stage") != expected_stage:
                send_message(chat_id, f"이전 단계 버튼입니다. 현재 단계는 {job.get('stage')}입니다.")
                return
            start_background_task(state, chat_id, job, f"{target} 재생성", lambda: handle_rerun(chat_id, job, "/rerun " + target))
    except Exception as exc:
        send_message(chat_id, f"오류: {exc}")

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
    run_render(chat_id, job)


def handle_status(chat_id, job):
    if not job:
        send_message(chat_id, "진행 중인 작업이 없습니다.")
        return
    send_message(chat_id, json.dumps(job, ensure_ascii=False, indent=2))


def command_specs():
    return [
        ("run", "승인형 파이프라인 시작"),
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
        "/render font_size=22 margin_v=55",
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
        if apply_edit_message(chat_id, job, message):
            return
        if not text:
            send_message(chat_id, help_text())
        elif text.startswith("/start") or text.startswith("/help"):
            send_message(chat_id, help_text())
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
