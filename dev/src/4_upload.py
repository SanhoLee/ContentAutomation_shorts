import os
import json
import glob
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

WORK_DIR = os.environ.get("WORK_DIR", os.path.expanduser("~/brain50/data/work"))
OUTPUT_DIR = os.environ.get("OUTPUT_DIR", os.path.expanduser("~/brain50/data/output"))
ASSETS_DIR = os.environ.get("ASSETS_DIR", os.path.expanduser("~/brain50/data/assets"))

# 가장 최근 생성된 output 파일 찾기
output_files = sorted(glob.glob(os.path.join(OUTPUT_DIR, "output_*.mp4")))
if not output_files:
    raise Exception("업로드할 영상이 없습니다.")
video_path = output_files[-1]
print(f"업로드 대상: {video_path}")

# 영상별 메타데이터 (제목, 해시태그, 설명 인트로)
with open(os.path.join(WORK_DIR, "video_meta.json"), "r", encoding="utf-8") as f:
    video_meta = json.load(f)

title = video_meta["title"]
topic_hashtags = video_meta["hashtags"]
intro_description = video_meta["description"]

if len(title) > 100:
    title = title[:97] + "..."

# BGM 정보
with open(os.path.join(ASSETS_DIR, "bgm_info.json"), "r", encoding="utf-8") as f:
    bgm_info = json.load(f)

# 설명 템플릿 로드 및 치환
with open(os.path.join(ASSETS_DIR, "description_template.txt"), "r", encoding="utf-8") as f:
    template = f.read()

template = template.replace("{{MUSIC_TRACK}}", bgm_info["track"])
template = template.replace("{{MUSIC_ARTIST}}", bgm_info["artist"])
template = template.replace("{{MUSIC_ID}}", bgm_info["audio_id"])
template = template.replace("{{TOPIC_HASHTAGS}}", topic_hashtags)

# 설명란 = 인트로(아들 톤) + 고정 템플릿
description = f"{intro_description}\n\n{template}"



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
        "tags": ["브레인피프티", "뇌건강", "Shorts"]
    },
    "status": {
        "privacyStatus": "private",  # 확인 후 public/unlisted로 변경
        "selfDeclaredMadeForKids": False
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
print("\n상태: private (확인 후 YouTube Studio에서 공개 전환하세요)")