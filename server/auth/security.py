"""
Хранение паролей и выпуск токенов сессии.

Пароль не хранится и не восстанавливается: в базе лежит результат PBKDF2
со случайной солью. Алгоритм взят из стандартной библиотеки намеренно —
это снимает внешнюю зависимость и позволяет объяснить каждую строку.

Оговорка для защиты: для боевой эксплуатации выбрали бы Argon2id.
PBKDF2-HMAC-SHA256 со 200 000 итераций — разумный минимум, которого
достаточно для конкурсного стенда.
"""

import hashlib
import hmac
import secrets
from datetime import timedelta

from clock import utc_now

PBKDF2_ITERATIONS = 200_000
SALT_BYTES = 16
TOKEN_BYTES = 32

# Срок жизни сессии. Смена в аэропорту длится 12 часов, плюс запас
# на пересменку: за одну смену диспетчер не должен входить повторно.
SESSION_LIFETIME = timedelta(hours=14)


def hash_password(password):
    """Возвращает строку вида «итерации$соль$хеш» — всё нужное для проверки."""
    salt = secrets.token_bytes(SALT_BYTES)
    digest = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt, PBKDF2_ITERATIONS
    )
    return f"{PBKDF2_ITERATIONS}${salt.hex()}${digest.hex()}"


def verify_password(password, password_hash):
    """
    Проверяет пароль.

    Сравнение через compare_digest, а не «==»: обычное сравнение строк
    завершается на первом несовпавшем байте, и по времени ответа можно
    подбирать хеш посимвольно.
    """
    try:
        iterations, salt_hex, digest_hex = password_hash.split("$")
        digest = hashlib.pbkdf2_hmac(
            "sha256",
            password.encode("utf-8"),
            bytes.fromhex(salt_hex),
            int(iterations),
        )
    except (ValueError, AttributeError):
        return False

    return hmac.compare_digest(digest.hex(), digest_hex)


def new_token():
    """Случайный токен сессии."""
    return secrets.token_urlsafe(TOKEN_BYTES)


def session_expiry(now=None):
    """Момент истечения новой сессии."""
    return (now or utc_now()) + SESSION_LIFETIME
