"""Slack Socket Mode adapter for the existing Telegram approval workflow.

The pipeline and state transitions live in ``telegram_bot``.  This adapter only
replaces its Telegram transport with Slack Block Kit, messages, and file APIs.
"""
import os
import signal
from pathlib import Path
from urllib.request import Request, urlopen

# The shared workflow validates this variable at import time.  Slack does not
# use Telegram's API, so provide an import-only placeholder when it is absent.
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "slack-transport-placeholder")
import telegram_bot as workflow

BOT_TOKEN = os.environ.get("SLACK_BOT_TOKEN")
APP_TOKEN = os.environ.get("SLACK_APP_TOKEN")
ALLOWED_CHANNEL_ID = os.environ.get("SLACK_CHANNEL_ID")
ALLOWED_USER_ID = os.environ.get("SLACK_ALLOWED_USER_ID")
STATE_PATH = Path(os.environ.get("SLACK_STATE_PATH", workflow.BASE_DIR / "data" / "slack_state.json"))
MAX_BLOCK_TEXT = 3000


def _require_tokens():
    missing = [name for name, value in (("SLACK_BOT_TOKEN", BOT_TOKEN), ("SLACK_APP_TOKEN", APP_TOKEN)) if not value]
    if missing:
        raise SystemExit(f"{' and '.join(missing)} are required")


def _slack_client():
    # Keep module import and unit tests independent from the optional Slack SDK.
    try:
        from slack_sdk import WebClient
    except ImportError as exc:
        raise SystemExit("Slack dependency is missing. Run: python3 -m pip install -r requirements-slack.txt") from exc
    return WebClient(token=BOT_TOKEN)


def _thread_for(channel_id):
    return workflow.chat_state(_STATE, channel_id).get("slack_thread_ts")


def send_message(channel_id, text):
    kwargs = {"channel": str(channel_id), "text": str(text)}
    if thread_ts := _thread_for(channel_id):
        kwargs["thread_ts"] = thread_ts
    return _slack_client().chat_postMessage(**kwargs)


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


def _workflow_api(method, data=None, files=None):
    # handle_callback acknowledges Telegram buttons. Slack actions are already
    # acknowledged by Bolt, so this is intentionally a no-op.
    if method == "answerCallbackQuery":
        return {"ok": True}
    raise RuntimeError(f"Slack transport does not support Telegram API method: {method}")


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
    job = workflow.chat_state(_STATE, channel_id)
    job["slack_thread_ts"] = event.get("thread_ts") or event.get("ts")
    workflow.handle_message(_STATE, _event_to_message(event))
    workflow.save_state(_STATE)


def _dispatch_action(body):
    channel_id = body.get("channel", {}).get("id")
    user_id = body.get("user", {}).get("id")
    actions = body.get("actions") or []
    if not channel_id or not actions:
        return
    if not _allow(channel_id, user_id):
        _denied(channel_id)
        return
    job = workflow.chat_state(_STATE, channel_id)
    job["slack_thread_ts"] = body.get("message", {}).get("thread_ts") or body.get("message", {}).get("ts")
    workflow.handle_callback(_STATE, {"message": {"chat": {"id": channel_id}}, "data": actions[0].get("value", "")})
    workflow.save_state(_STATE)


def _dispatch_command(command, ack):
    ack()
    channel_id, user_id = command["channel_id"], command["user_id"]
    if not _allow(channel_id, user_id):
        _denied(channel_id)
        return
    job = workflow.chat_state(_STATE, channel_id)
    job["slack_thread_ts"] = None
    text = "/" + command["command"].rsplit("/", 1)[-1]
    if command.get("text"):
        text += " " + command["text"]
    workflow.handle_message(_STATE, {"chat": {"id": channel_id}, "text": text})
    workflow.save_state(_STATE)


def _configure_workflow():
    global _STATE
    workflow.STATE_PATH = STATE_PATH
    workflow.ALLOWED_CHAT_ID = None  # Slack authorization is channel/user aware.
    workflow.send_message = send_message
    workflow.send_action_message = send_action_message
    workflow.send_file_or_path = send_file_or_path
    workflow.send_document = lambda channel_id, path, caption=None: send_file_or_path(channel_id, path, caption)
    workflow.download_telegram_file = download_slack_file
    workflow.api = _workflow_api
    _STATE = workflow.load_state()
    if workflow.clear_stale_busy_flags(_STATE):
        workflow.save_state(_STATE)


def main():
    _require_tokens()
    _configure_workflow()
    try:
        from slack_bolt import App
        from slack_bolt.adapter.socket_mode import SocketModeHandler
    except ImportError as exc:
        raise SystemExit("Slack dependency is missing. Run: python3 -m pip install -r requirements-slack.txt") from exc

    app = App(token=BOT_TOKEN)
    app.event("message")(_dispatch_message)
    app.action("workflow_action")(lambda ack, body: (ack(), _dispatch_action(body)))
    for name, _ in workflow.command_specs():
        app.command(f"/{name}")(_dispatch_command)

    def shutdown(signum, frame):
        if ALLOWED_CHANNEL_ID:
            try:
                send_message(ALLOWED_CHANNEL_ID, workflow.shutdown_message(signum).replace("Telegram", "Slack"))
            except Exception:
                pass
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)
    if ALLOWED_CHANNEL_ID:
        send_message(ALLOWED_CHANNEL_ID, workflow.startup_message().replace("Telegram", "Slack"))
    SocketModeHandler(app, APP_TOKEN).start()


if __name__ == "__main__":
    main()
