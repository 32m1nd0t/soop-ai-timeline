from __future__ import annotations

import os

SERVICE_NAME = "SOOPTimeline"
GEMINI_KEY_NAME = "gemini-api-key"


def get_gemini_api_key() -> str:
    environment_key = os.environ.get("GEMINI_API_KEY", "").strip()
    if environment_key:
        return environment_key
    try:
        import keyring

        return (keyring.get_password(SERVICE_NAME, GEMINI_KEY_NAME) or "").strip()
    except Exception:
        return ""


def save_gemini_api_key(api_key: str) -> None:
    try:
        import keyring
    except ImportError as error:
        raise RuntimeError("API 키 보관 모듈(keyring)이 설치되지 않았습니다.") from error

    value = api_key.strip()
    if value:
        keyring.set_password(SERVICE_NAME, GEMINI_KEY_NAME, value)
    else:
        try:
            keyring.delete_password(SERVICE_NAME, GEMINI_KEY_NAME)
        except keyring.errors.PasswordDeleteError:
            pass
