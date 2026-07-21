from __future__ import annotations

import os

from .ai_provider import normalize_ai_provider, provider_spec


SERVICE_NAME = "SOOPTimeline"
GEMINI_KEY_NAME = "gemini-api-key"
KEY_NAMES = {
    "gemini": GEMINI_KEY_NAME,
    "openai": "openai-api-key",
    "anthropic": "anthropic-api-key",
}


def get_ai_api_key(provider: str) -> str:
    normalized = normalize_ai_provider(provider)
    environment_key = os.environ.get(
        provider_spec(normalized).environment_variable,
        "",
    ).strip()
    if environment_key:
        return environment_key
    try:
        import keyring

        return (
            keyring.get_password(SERVICE_NAME, KEY_NAMES[normalized]) or ""
        ).strip()
    except Exception:
        return ""


def save_ai_api_key(provider: str, api_key: str) -> None:
    normalized = normalize_ai_provider(provider)
    try:
        import keyring
    except ImportError as error:
        raise RuntimeError("API 키 보관 모듈(keyring)이 설치되지 않았습니다.") from error

    value = api_key.strip()
    if value:
        keyring.set_password(SERVICE_NAME, KEY_NAMES[normalized], value)
    else:
        try:
            keyring.delete_password(SERVICE_NAME, KEY_NAMES[normalized])
        except keyring.errors.PasswordDeleteError:
            pass


def get_gemini_api_key() -> str:
    return get_ai_api_key("gemini")


def save_gemini_api_key(api_key: str) -> None:
    save_ai_api_key("gemini", api_key)
