import json
import os
import shlex
import subprocess
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

if not TOKEN:
    raise SystemExit("TELEGRAM_BOT_TOKEN is required")

API_URL = f"https://api.telegram.org/bot{TOKEN}"


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


def load_state():
    if not STATE_PATH.exists():
        return {"offset": 0, "chats": {}}
    return json.loads(STATE_PATH.read_text(encoding="utf-8"))


def save_state(state):
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def chat_state(state, chat_id):
    chats = state.setdefault("chats", {})
    return chats.setdefault(str(chat_id), {})


def new_job_id(prefix="tg"):
    return f"{prefix}_{datetime.now().strftime('%Y%m%d_%H%M%S')}"


def work_dir(job_id):
    return WORK_DIR_BASE / job_id


def output_file(job_id):
    return OUTPUT_DIR / f"output_{job_id}.mp4"


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
        raise RuntimeError(f"명령 실패: {' '.join(shlex.quote(a) for a in args)}\n로그: {log_dir / log_name}\n\n{result.stderr[-1200:]}")
    return result.stdout


def preview_file(path, limit=MAX_TEXT_PREVIEW):
    path = Path(path)
    if not path.exists():
        return "파일이 없습니다."
    text = path.read_text(encoding="utf-8", errors="replace")
    if len(text) > limit:
        return text[:limit] + "\n...(생략)"
    return text


def send_script(chat_id, job_id):
    path = work_dir(job_id) / "script.txt"
    send_message(chat_id, f"스크립트 생성 완료. 확인 후 /approve 또는 /cancel\n\n{preview_file(path)}")
    if path.exists():
        send_file_or_path(chat_id, path, "script.txt")


def send_tts(chat_id, job_id):
    path = work_dir(job_id) / "voice.wav"
    if path.exists():
        send_file_or_path(chat_id, path, "TTS 음성입니다. 확인 후 /approve 또는 /rerun tts")
    else:
        send_message(chat_id, f"voice.wav를 찾지 못했습니다: {path}")


def send_caption(chat_id, job_id):
    path = work_dir(job_id) / "subs.srt"
    send_message(chat_id, f"자막 생성 완료. 수정이 필요하면 서버에서 subs.srt를 고친 뒤 /approve 하세요.\n\n{preview_file(path)}")
    if path.exists():
        send_file_or_path(chat_id, path, "subs.srt")


def send_broll(chat_id, job_id):
    path = work_dir(job_id) / "broll.mp4"
    if path.exists():
        send_file_or_path(chat_id, path, "B-roll 확인 후 /approve 또는 /rerun broll", as_video=True)
    else:
        send_message(chat_id, f"broll.mp4를 찾지 못했습니다: {path}")


def send_render_ready(chat_id, job):
    font_size = job.get("caption_font_size", os.environ.get("CAPTION_FONT_SIZE", "20"))
    margin_v = job.get("caption_margin_v", os.environ.get("CAPTION_MARGIN_V", "200"))
    send_message(
        chat_id,
        "렌더 설정 확인 단계입니다.\n"
        f"현재값: font_size={font_size}, margin_v={margin_v}\n"
        "기본값으로 렌더: /approve\n"
        "값 조정 후 렌더: /render font_size=22 margin_v=180",
    )


def send_rendered_video(chat_id, job_id):
    path = output_file(job_id)
    if path.exists():
        send_file_or_path(chat_id, path, "최종 합성 영상입니다. 확인 후 /approve 또는 /render font_size=22 margin_v=180", as_video=True)
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
        "승인하면 비공개 영상으로 업로드합니다: /approve"
    )
    send_message(chat_id, text[:MAX_TEXT_PREVIEW])
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


def run_render(chat_id, job):
    job_id = job["job_id"]
    args = [str(BASE_DIR / "sh" / "2_render.sh")]
    font_size = str(job.get("caption_font_size", os.environ.get("CAPTION_FONT_SIZE", "20")))
    margin_v = str(job.get("caption_margin_v", os.environ.get("CAPTION_MARGIN_V", "200")))
    args += ["--font-size", font_size, "--margin-v", margin_v]
    send_message(chat_id, f"렌더링 시작: font_size={font_size}, margin_v={margin_v}")
    run_command(args, job_id, job.get("topic"))
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

def handle_run_auto(chat_id, job, text):
    topic = text.split(maxsplit=1)[1].strip() if len(text.split(maxsplit=1)) > 1 else ""
    if not topic:
        send_message(chat_id, "주제를 입력하세요. 예: /run_auto 오메가3가 정말 뇌에 좋을까?")
        return
    job_id = new_job_id("auto")
    job.clear()
    job.update({"job_id": job_id, "topic": topic, "approval_required": False, "stage": "running_auto"})
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
    job.clear()
    job.update({"job_id": job_id, "topic": topic, "approval_required": True})
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
        run_command([str(BASE_DIR / "sh" / "0_script.sh"), topic], job_id, topic)
        send_script(chat_id, job_id)


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
    run_command([str(BASE_DIR / "sh" / "0_script.sh"), "--trend-choice", choice], job_id, job.get("topic"))
    job["stage"] = "await_script_approval"
    send_script(chat_id, job_id)


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
        ("rerun", "tts/caption/broll 재생성"),
        ("render", "자막 렌더 설정 변경"),
        ("status", "현재 상태 확인"),
        ("cancel", "현재 작업 취소"),
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
        "/rerun tts | /rerun caption | /rerun broll",
        "/render font_size=22 margin_v=180",
        "/run_auto 오메가3가 정말 뇌에 좋을까?",
        "/status",
        "/cancel",
        "",
        "흐름: run/trend -> approve 반복 -> 렌더 확인 -> 메타데이터 승인 -> 비공개 업로드",
    ])


def handle_message(state, message):
    chat_id = message.get("chat", {}).get("id")
    text = (message.get("text") or "").strip()
    if not chat_id or not text:
        return
    if ALLOWED_CHAT_ID and str(chat_id) != str(ALLOWED_CHAT_ID):
        send_message(chat_id, "허용되지 않은 chat_id입니다.")
        return

    job = chat_state(state, chat_id)
    try:
        if text.startswith("/start") or text.startswith("/help"):
            send_message(chat_id, help_text())
        elif text.startswith("/run_auto "):
            handle_run_auto(chat_id, job, text)
        elif text.startswith("/run "):
            handle_run(chat_id, job, text, trend=False)
        elif text.startswith("/trend "):
            handle_run(chat_id, job, text, trend=True)
        elif text.startswith("/pick"):
            handle_pick(chat_id, job, text)
        elif text.startswith("/approve"):
            run_next_stage(chat_id, job)
        elif text.startswith("/rerun"):
            handle_rerun(chat_id, job, text)
        elif text.startswith("/render"):
            handle_render(chat_id, job, text)
        elif text.startswith("/status"):
            handle_status(chat_id, job)
        elif text.startswith("/cancel"):
            job.clear()
            send_message(chat_id, "현재 작업을 취소했습니다.")
        else:
            send_message(chat_id, help_text())
    except Exception as exc:
        send_message(chat_id, f"오류: {exc}")


def poll_updates(offset):
    params = {"timeout": POLL_TIMEOUT, "offset": offset}
    query = urlencode(params)
    with urlopen(f"{API_URL}/getUpdates?{query}", timeout=POLL_TIMEOUT + 10) as response:
        return json.loads(response.read().decode("utf-8"))


def main():
    state = load_state()
    send_to = ALLOWED_CHAT_ID
    try:
        register_bot_commands()
    except Exception:
        pass
    if send_to:
        send_message(send_to, f"Brain50 Telegram bot started: {BASE_DIR}")
    while True:
        try:
            data = poll_updates(state.get("offset", 0))
            for update in data.get("result", []):
                state["offset"] = update["update_id"] + 1
                message = update.get("message") or update.get("edited_message")
                if message:
                    handle_message(state, message)
                    save_state(state)
            save_state(state)
        except Exception as exc:
            if send_to:
                try:
                    send_message(send_to, f"Bot polling error: {exc}")
                except Exception:
                    pass
            time.sleep(5)


if __name__ == "__main__":
    main()
