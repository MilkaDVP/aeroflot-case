"""
Клиент GigaChat: пояснения ИИ-диспетчера и ходы ИИ-агента.

Решения (кого отправить на какой вызов) принимает встроенный алгоритм
подбора: допуск, маршрут, 15-минутный регламент. GigaChat лишь пересказывает
готовый план диспетчеру обычным языком. Поэтому, если ключа нет или сервис
недоступен, ИИ-диспетчер работает как прежде, а пояснение пишется по шаблону.

Настройка — переменными окружения или файлом server/.env (в git не попадает):
    GIGACHAT_AUTH_KEY   ключ авторизации из личного кабинета (Basic, base64)
    GIGACHAT_SCOPE      область доступа, по умолчанию GIGACHAT_API_PERS
    GIGACHAT_MODEL      модель, по умолчанию GigaChat
    GIGACHAT_AGENT_MODEL модель ИИ-агента, по умолчанию GigaChat-2-Max;
                        недоступна — агент работает на GIGACHAT_MODEL
    GIGACHAT_ENABLED    0 — выключить GigaChat при заданном ключе
    GIGACHAT_VERIFY_SSL 0 — не проверять сертификат сервера (только локально)
    GIGACHAT_CA_BUNDLE  путь к сертификату «Минцифры» (Russian Trusted Root CA),
                        если в системе его нет; проверку сертификата
                        не отключаем — ключ уходит по этому соединению.
"""

import os
import threading
import time
import uuid
from pathlib import Path

import httpx

OAUTH_URL = "https://ngw.devices.sberbank.ru:9443/api/v2/oauth"
CHAT_URL = "https://gigachat.devices.sberbank.ru/api/v1/chat/completions"
TIMEOUT_S = 20
AGENT_TIMEOUT_S = 45
# Токен живёт 30 минут; обновляем чуть раньше, чтобы не поймать отказ.
TOKEN_MARGIN_S = 60

ENV_FILE = Path(__file__).resolve().parent.parent / ".env"

_lock = threading.Lock()
_token = {"value": None, "expires_at": 0.0}
_last_error = {"text": None}


def load_env_file(path=ENV_FILE):
    """Переменные GIGACHAT_* из server/.env, если их нет в окружении."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, value = line.split("=", 1)
        name, value = name.strip(), value.strip().strip('"').strip("'")
        if name.startswith("GIGACHAT_") and name not in os.environ:
            os.environ[name] = value


load_env_file()


def configured():
    """Ключ задан и GigaChat не выключен явно (GIGACHAT_ENABLED=0)."""
    if os.environ.get("GIGACHAT_ENABLED", "1") == "0":
        return False
    return bool(os.environ.get("GIGACHAT_AUTH_KEY"))


def last_error():
    """Почему последний запрос не удался — для подсказки администратору."""
    return _last_error["text"]


def _verify():
    """
    Проверка сертификата сервера GigaChat.

    По умолчанию включена. GIGACHAT_VERIFY_SSL=0 выключает её — так же,
    как verify_ssl_certs=False в официальной библиотеке GigaChat: удобно
    локально, когда сертификата «Минцифры» в системе нет, но соединение
    тогда можно подменить. На рабочем сервере лучше GIGACHAT_CA_BUNDLE.
    """
    bundle = os.environ.get("GIGACHAT_CA_BUNDLE")
    if bundle:
        return bundle
    return os.environ.get("GIGACHAT_VERIFY_SSL", "1") != "0"


def _access_token(client):
    with _lock:
        if _token["value"] and time.time() < _token["expires_at"] - TOKEN_MARGIN_S:
            return _token["value"]
        response = client.post(
            OAUTH_URL,
            headers={
                "Authorization": f"Basic {os.environ['GIGACHAT_AUTH_KEY']}",
                "RqUID": str(uuid.uuid4()),
                "Content-Type": "application/x-www-form-urlencoded",
                "Accept": "application/json",
            },
            data={"scope": os.environ.get("GIGACHAT_SCOPE", "GIGACHAT_API_PERS")},
        )
        response.raise_for_status()
        body = response.json()
        _token["value"] = body["access_token"]
        # expires_at приходит в миллисекундах.
        _token["expires_at"] = body.get("expires_at", 0) / 1000 or time.time() + 1800
        return _token["value"]


def chat(messages, temperature=0.1, max_tokens=900):
    """
    Ответ модели на разговор целиком или None, если GigaChat недоступен.
    """
    if not configured():
        _last_error["text"] = "GigaChat не настроен или выключен"
        return None
    base = os.environ.get("GIGACHAT_MODEL", "GigaChat")
    models = dict.fromkeys([os.environ.get("GIGACHAT_AGENT_MODEL", "GigaChat-2-Max"), base])
    try:
        with httpx.Client(timeout=AGENT_TIMEOUT_S, verify=_verify()) as client:
            token = _access_token(client)
            for model in models:
                response = client.post(
                    CHAT_URL,
                    headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
                    json={
                        "model": model,
                        "messages": messages,
                        "temperature": temperature,
                        "max_tokens": max_tokens,
                    },
                )
                # Модель недоступна по тарифу или исчерпан её лимит.
                if response.status_code not in (402, 403, 404, 429):
                    break
            response.raise_for_status()
            _last_error["text"] = None
            return response.json()["choices"][0]["message"]["content"]
    except Exception as error:  # сеть, сертификат, лимиты, формат ответа
        _last_error["text"] = f"{type(error).__name__}: {str(error)[:200]}"
        return None


def complete(system, user, temperature=0.2, max_tokens=400):
    """
    Ответ модели на один запрос или None, если GigaChat недоступен.
    Ошибка не пробрасывается: пояснение — не повод ронять экран диспетчера.
    """
    if not configured():
        _last_error["text"] = "GigaChat не настроен или выключен"
        return None
    try:
        with httpx.Client(timeout=TIMEOUT_S, verify=_verify()) as client:
            token = _access_token(client)
            response = client.post(
                CHAT_URL,
                headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
                json={
                    "model": os.environ.get("GIGACHAT_MODEL", "GigaChat"),
                    "messages": [
                        {"role": "system", "content": system},
                        {"role": "user", "content": user},
                    ],
                    "temperature": temperature,
                    "max_tokens": max_tokens,
                },
            )
            response.raise_for_status()
            _last_error["text"] = None
            return response.json()["choices"][0]["message"]["content"].strip()
    except Exception as error:  # сеть, сертификат, лимиты, формат ответа
        _last_error["text"] = f"{type(error).__name__}: {str(error)[:200]}"
        return None
