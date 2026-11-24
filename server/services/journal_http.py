"""
Журнал на уровне запросов к API.

Что сюда попадает:
- действия пользователей — каждый успешный изменяющий запрос (кто, что,
  над чем, сколько занял);
- ошибки приёма данных — отказ на входящих данных внешних систем,
  координатах агентов и действиях инженера с телефона: неверный ключ,
  неизвестный аэропорт, нарушенный формат, недопустимое значение;
- отказы в действиях пользователей и сбои сервера.

Пароли, ключи и токены в журнал не попадают: тело запроса записывается
без них. Частые фоновые запросы — координаты GPS и продление «взятия
в работу» — при успехе не пишутся: это поток данных, а не события.
"""

import asyncio
import json
import re
import time
import traceback


MAX_BODY = 4000
SECRET_WORDS = ("password", "token", "key", "secret", "пароль")

# Входящие данные агентов и внешних систем: отказ здесь — ошибка приёма.
INTAKE_PATTERNS = (
    re.compile(r"^/api/intake/"),
    re.compile(r"/location$"),
    re.compile(r"^/api/me/"),
    re.compile(r"^/api/v1/track$"),
)
# Успех этих запросов не пишется: поток данных или явная запись в коде.
QUIET_PATTERNS = (
    re.compile(r"/location$"),
    re.compile(r"^/api/v1/track$"),
    re.compile(r"/(claim|release|read)$"),
    re.compile(r"^/api/intake/"),
    re.compile(r"^/api/auth/login$"),
    re.compile(r"/suggest$"),
    re.compile(r"^/api/ai/agent/step$"),
    # Расчёт порядка точек: ничего не меняет.
    re.compile(r"^/api/calls/\d+/waypoints/plan$"),
    re.compile(r"^/api/calls/\d+/route-preview$"),
)
CLAIM = re.compile(r"/(claim|release)$")
ENTITY = re.compile(r"^/api/(calls|employees|vehicles|aircraft|users|reports)/(\d+)")
ICAO = re.compile(r"^/api/(?:admin/)?airports/([A-Za-z0-9]{3,4})(?:/|$)")


def matches(patterns, path):
    return any(pattern.search(path) for pattern in patterns)


def sanitize(value):
    """Тело запроса без секретов и без лишнего объёма."""
    if isinstance(value, dict):
        return {
            key: ("***" if any(word in key.lower() for word in SECRET_WORDS) else sanitize(item))
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [sanitize(item) for item in value[:50]]
    if isinstance(value, str) and len(value) > 300:
        return value[:300] + "…"
    return value


def parse_json(raw):
    if not raw:
        return None
    try:
        return json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return raw[:300].decode("utf-8", "replace")


def error_detail(raw):
    data = parse_json(raw)
    if isinstance(data, dict) and "detail" in data:
        detail = data["detail"]
        if isinstance(detail, list):
            # Ошибки проверки формата: «поле: что не так».
            return "; ".join(
                f"{'.'.join(str(p) for p in item.get('loc', [])[1:]) or 'тело'}: {item.get('msg')}"
                for item in detail[:5] if isinstance(item, dict)
            )
        return str(detail)
    return str(data) if data else None


def resolve(token, path, body):
    """Инициатор, аэропорт, заявка и борт запроса — по токену, пути и телу."""
    from database import SessionLocal
    from models.aircraft import Aircraft
    from models.call import Call
    from models.employee import RouteParticipant
    from models.report import Report
    from models.user import AuthSession, User
    from models.vehicle import Vehicle

    found = {"actor": None, "airport": None, "call_id": None, "board": None, "session_airport": None}
    db = SessionLocal()
    try:
        session = db.get(AuthSession, token) if token else None
        if session is not None:
            user = session.user
            found["actor"] = {"id": user.id, "name": user.full_name, "role": user.role}
            found["session_airport"] = session.airport_icao or next(iter(user.airport_icaos), None)

        entity = ENTITY.match(path)
        if entity:
            kind, ident = entity.group(1), int(entity.group(2))
            model = {"calls": Call, "employees": RouteParticipant, "vehicles": Vehicle,
                     "aircraft": Aircraft, "users": User, "reports": Report}[kind]
            item = db.get(model, ident)
            if item is not None:
                if kind == "users":
                    icaos = item.airport_icaos
                    found["airport"] = icaos[0] if len(icaos) == 1 else None
                else:
                    found["airport"] = item.airport_icao
                if kind == "calls":
                    found["call_id"] = item.id
                    found["board"] = item.aircraft.board_number if item.aircraft else None
                elif kind == "aircraft":
                    found["board"] = item.board_number
        icao = ICAO.match(path)
        if icao:
            found["airport"] = icao.group(1).upper()
    except Exception:  # журнал не должен ронять запрос
        pass
    finally:
        db.close()

    if found["airport"] is None and isinstance(body, dict) and isinstance(body.get("airport_icao"), str):
        found["airport"] = body["airport_icao"].upper()
    return found


def write(type_, text_, found, params, duration_ms):
    from database import SessionLocal, begin_dispatch_write
    from services import journal

    db = SessionLocal()
    try:
        begin_dispatch_write(db)
        journal.record(
            db, type_, text_,
            airport_icao=found.get("airport") or found.get("session_airport"),
            call_id=found.get("call_id"), board=found.get("board"),
            actor=found.get("actor"), duration_ms=duration_ms, params=params,
        )
        db.commit()
    except Exception as error:  # журнал не должен ронять запрос
        db.rollback()
        print(f"Журнал: запись не удалась — {error}")
    finally:
        db.close()


class JournalMiddleware:
    """ASGI-прослойка: пишет действия и ошибки, не меняя сам запрос."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        path = scope.get("path", "")
        if scope["type"] != "http" or not path.startswith("/api/") or scope["method"] == "OPTIONS":
            await self.app(scope, receive, send)
            return

        method = scope["method"]
        mutating = method in ("POST", "PUT", "PATCH", "DELETE")
        intake = matches(INTAKE_PATTERNS, path)
        quiet = matches(QUIET_PATTERNS, path)
        headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope.get("headers", [])}
        auth = headers.get("authorization", "")
        token = auth[7:].strip() if auth.startswith("Bearer ") else None

        body = bytearray()
        response = {"status": None, "body": bytearray()}

        async def receive_logged():
            message = await receive()
            if message["type"] == "http.request" and len(body) < MAX_BODY:
                body.extend(message.get("body", b"")[: MAX_BODY - len(body)])
            return message

        async def send_logged(message):
            if message["type"] == "http.response.start":
                response["status"] = message["status"]
            elif message["type"] == "http.response.body" and (response["status"] or 0) >= 400:
                if len(response["body"]) < MAX_BODY:
                    response["body"].extend(message.get("body", b"")[: MAX_BODY - len(response["body"])])
            await send(message)

        # Инициатор и объект — до выполнения: после выхода или удаления
        # их уже не найти.
        found = None
        if mutating and not quiet:
            found = await asyncio.to_thread(resolve, token, path, None)

        started = time.perf_counter()
        try:
            await self.app(scope, receive_logged, send_logged)
        except Exception as error:
            duration = (time.perf_counter() - started) * 1000
            found = found or await asyncio.to_thread(resolve, token, path, None)
            await asyncio.to_thread(
                write, "server_error", f"Сбой сервера: {method} {path} — {type(error).__name__}",
                found, {"метод": method, "путь": path, "ошибка": str(error)[:500],
                        "трасса": traceback.format_exc(limit=3)[-1500:]}, duration,
            )
            raise
        duration = (time.perf_counter() - started) * 1000
        status_code = response["status"] or 0

        if status_code < 400:
            if not mutating or quiet:
                return
        elif status_code < 500:
            # Отказы чтения — не события; отказ продления «в работе» — тоже.
            if not (mutating or intake) or CLAIM.search(path):
                return

        request_body = sanitize(parse_json(bytes(body)))
        if found is None:
            found = await asyncio.to_thread(resolve, token, path, request_body)
        elif found["airport"] is None and isinstance(request_body, dict):
            airport = request_body.get("airport_icao")
            if isinstance(airport, str):
                found["airport"] = airport.upper()
        if path.startswith(("/api/intake/", "/api/v1/track")) and found["actor"] is None:
            found["actor"] = {"id": None, "name": "Внешняя система", "role": "external"}

        route = scope.get("route")
        title = getattr(route, "summary", None) or getattr(route, "name", None) or f"{method} {path}"
        params = {
            "метод": method,
            "путь": path,
            "код_ответа": status_code,
            "параметры_пути": scope.get("path_params") or None,
            "запрос": scope.get("query_string", b"").decode("latin-1") or None,
            "тело": request_body,
        }

        if status_code >= 500:
            type_, text_ = "server_error", f"Сбой сервера: {title} ({status_code})"
        elif status_code >= 400:
            detail = error_detail(bytes(response["body"])) or "без пояснения"
            params["ошибка"] = detail
            if intake:
                type_, text_ = "data_error", f"Ошибка приёма данных: {title} — {detail}"
            else:
                type_, text_ = "action_rejected", f"Отклонено: {title} — {detail}"
        else:
            type_, text_ = "user_action", title
        await asyncio.to_thread(write, type_, text_, found, params, duration)

