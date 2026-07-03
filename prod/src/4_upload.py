import os
import json
import glob
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

WORK_DIR = os.environ.get("WORK_DIR", os.path.expanduser("~/brain50/data/work"))
OUTPUT_DIR = os.environ.get("OUTPUT_DIR", os.path.expanduser("~/brain50/data/output"))
ASSETS_DIR = os.environ.get("ASSETS_DIR", os.path.expanduser("~/brain50/data/assets"))
OUTPUT_FILE = os.environ.get("OUTPUT_FILE")


def resolve_video_path():
    if OUTPUT_FILE:
        if os.path.exists(OUTPUT_FILE):
            return OUTPUT_FILE
        raise Exception(f"OUTPUT_FILE이 존재하지 않습니다: {OUTPUT_FILE}")

    marker_path = os.path.join(WORK_DIR, "output_path.txt")
    if os.path.exists(marker_path):
        with open(marker_path, "r", encoding="utf-8") as f:
            candidate = f.read().strip()
        if candidate and os.path.exists(candidate):
            return candidate

    output_files = sorted(glob.glob(os.path.join(OUTPUT_DIR, "output_*.mp4")))
    if output_files:
        return output_files[-1]

    raise Exception("업로드할 영상이 없습니다.")


video_path = resolve_video_path()
print(f"업로드 대상: {video_path}")

# 영상별 메타데이터 (제목, 해시태그, 설명 인트로)
with open(os.path.join(WORK_DIR, "video_meta.json"), "r", encoding="utf-8") as f:
    video_meta = json.load(f)

title = video_meta["title"]
topic_hashtags = video_meta["hashtags"]
intro_description = video_meta["description"]
summary = video_meta.get("summary", "")

# YouTube Studio > 동영상 세부정보의 기본 언어/AI 사용 항목 자동 설정값
YOUTUBE_VIDEO_LANGUAGE = "ko"
YOUTUBE_TITLE_DESCRIPTION_LANGUAGE = "ko"
YOUTUBE_CONTAINS_SYNTHETIC_MEDIA = False

if len(title) > 100:
    title = title[:97] + "..."

# 설명 템플릿 로드 및 치환
with open(os.path.join(ASSETS_DIR, "description_template.txt"), "r", encoding="utf-8") as f:
    template = f.read()

template = template.replace("{{TOPIC_HASHTAGS}}", topic_hashtags)

# 설명란 = 인트로(아들 톤) + 본문 요약 + 고정 템플릿
summary_block = f"영상 요약\n{summary}\n\n" if summary else ""
description = f"{intro_description}\n\n{summary_block}{template}"

# 인증
creds = Credentials(
    token=None,
    refresh_token=os.environ["YOUTUBE_REFRESH_TOKEN"],
    client_id=os.environ["YOUTUBE_CLIENT_ID"],
    client_secret=os.environ["YOUTUBE_CLIENT_SECRET"],
    token_uri="https://oauth2.googleapis.com/token",
)

youtube = build("youtube", "v3", credentials=creds)

body = {
    "snippet": {
        "title": title,
        "description": description,
        "categoryId": "27",  # 27 = Education
        "tags": ["Shorts", "뇌건강", "브레인피프티"],
        "defaultLanguage": YOUTUBE_TITLE_DESCRIPTION_LANGUAGE,
        "defaultAudioLanguage": YOUTUBE_VIDEO_LANGUAGE
    },
    "status": {
        "privacyStatus": "private",  # 확인 후 public/unlisted로 변경
        "selfDeclaredMadeForKids": False,
        "containsSyntheticMedia": YOUTUBE_CONTAINS_SYNTHETIC_MEDIA
    }
}

media = MediaFileUpload(video_path, mimetype="video/mp4", resumable=True)

request = youtube.videos().insert(
    part="snippet,status",
    body=body,
    media_body=media
)

response = request.execute()
video_id = response["id"]

print(f"업로드 완료: https://youtube.com/shorts/{video_id}")
print(f"제목: {title}")
print(f"\n--- 설명란 인트로 ---\n{intro_description}")
if summary:
    print(f"\n--- 요약 ---\n{summary}")
print(f"\n동영상 언어: {YOUTUBE_VIDEO_LANGUAGE}")
print(f"제목 및 설명 언어: {YOUTUBE_TITLE_DESCRIPTION_LANGUAGE}")
print(f"AI 사용: {'예' if YOUTUBE_CONTAINS_SYNTHETIC_MEDIA else '아니요'}")
print("\n상태: private (확인 후 YouTube Studio에서 공개 전환하세요)")
