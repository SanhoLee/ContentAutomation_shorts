"""Instagram Graph API integration boundary — stubbed until real credentials exist.

Publishing a carousel needs a Business/Creator account behind a Facebook
Page, a Page access token, and each image reachable at a public HTTPS URL
(the Graph API fetches images by URL, it does not accept direct upload for
this endpoint). None of that is available yet, so the real calls below raise
`NotImplementedError` and `publish_stub()` is the only function the skeleton
CLI actually calls.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class InstagramCredentials:
    access_token: str
    business_account_id: str


def load_instagram_credentials() -> InstagramCredentials | None:
    access_token = os.environ.get("INSTAGRAM_ACCESS_TOKEN")
    business_account_id = os.environ.get("INSTAGRAM_BUSINESS_ACCOUNT_ID")
    if not access_token or not business_account_id:
        return None
    return InstagramCredentials(access_token=access_token, business_account_id=business_account_id)


def create_child_media_container(image_url: str, credentials: InstagramCredentials) -> str:
    raise NotImplementedError(
        "Instagram Graph API 연동 대기 중 — 공개 이미지 호스팅 및 실제 계정 정보 필요"
    )


def create_carousel_container(child_ids: list[str], caption: str, credentials: InstagramCredentials) -> str:
    raise NotImplementedError(
        "Instagram Graph API 연동 대기 중 — 공개 이미지 호스팅 및 실제 계정 정보 필요"
    )


def publish_carousel(container_id: str, credentials: InstagramCredentials) -> dict[str, Any]:
    raise NotImplementedError(
        "Instagram Graph API 연동 대기 중 — 공개 이미지 호스팅 및 실제 계정 정보 필요"
    )


def publish_stub(image_paths: list[Path], caption: str) -> dict[str, Any]:
    """The only publish path the skeleton actually exercises today.

    No network call — lets the whole card-generation flow be tested end to
    end without an Instagram account.
    """
    return {
        "status": "stubbed",
        "image_paths": [str(p) for p in image_paths],
        "caption": caption,
        "would_publish": True,
    }
