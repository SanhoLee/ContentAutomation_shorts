"""CLI entrypoint for the Instagram card content skeleton.

Standalone by design: reads `video_meta.json`/`scenes.json` from an existing
job's WORK_DIR and produces card images + a metadata JSON. Not wired into
`slack_bot.py` orchestration, so it can never block or slow
the YouTube pipeline — run it manually against any JOB_ID.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from card_content import build_card_set, env_int
from card_render import render_card_set
from publish import publish_stub


def resolve_work_dir(args: argparse.Namespace) -> Path:
    if args.work_dir:
        return Path(args.work_dir)
    env_work_dir = os.environ.get("WORK_DIR")
    if env_work_dir:
        return Path(env_work_dir)
    raise SystemExit("WORK_DIR을 --work-dir 인자나 환경변수로 지정해주세요.")


def main() -> None:
    parser = argparse.ArgumentParser(description="Instagram 카드 콘텐츠 생성 (스켈레톤)")
    parser.add_argument("--job-id", default=os.environ.get("JOB_ID"))
    parser.add_argument("--work-dir", default=None)
    parser.add_argument("--max-cards", type=int, default=env_int("INSTAGRAM_MAX_CARDS", 8))
    args = parser.parse_args()

    work_dir = resolve_work_dir(args)
    texts = build_card_set(work_dir, max_cards=args.max_cards)
    if not texts:
        print(f"⚠️  {work_dir}에서 카드로 쓸 텍스트를 찾지 못했습니다 (video_meta.json/scenes.json 확인).")

    output_dir = work_dir / "instagram_cards"
    image_paths = render_card_set(texts, output_dir)

    caption = texts[-1] if texts else ""
    successful_paths = [path for path in image_paths if path is not None]
    publish_result = publish_stub(successful_paths, caption)

    cards = [
        {"index": i + 1, "text": text, "image_path": str(path) if path else None}
        for i, (text, path) in enumerate(zip(texts, image_paths, strict=False))
    ]
    payload = {
        "job_id": args.job_id,
        "cards": cards,
        "caption": caption,
        "publish": publish_result,
    }
    (work_dir / "instagram_cards.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(f"완료. {len(cards)}장의 카드 생성 -> {output_dir}, {work_dir / 'instagram_cards.json'}")


if __name__ == "__main__":
    main()
