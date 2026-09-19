"""
Подбор сотрудника на вызов — ядро системы.

Порядок шагов задан техническим заданием и не подлежит перестановке:
сначала отсекается всё, что запрещено (допуск), затем всё, что невозможно
(доступность), и только потом среди оставшихся ищется оптимум по времени.
Обратный порядок дал бы быстрый ответ и юридически ничтожное назначение.

Модуль намеренно не знает про базу данных и работает с обычными словарями.
Так алгоритм тестируется без поднятия сервера, а слой хранения можно
менять, не трогая логику подбора.

Сотрудник передаётся словарём:
    {
      "id", "full_name", "shift", "status",
      "lat", "lon", "speed_kmh" (необязательно — скорость ходьбы),
      "vehicle" (необязательно) — машина парка, на которой он уже едет,
      "qualifications": [{"category", "aircraft_types", "valid_until"}],
      "busy_until" (необязательно, ISO-строка) — когда освободится
    }

Вызов передаётся словарём:
    { "aircraft_type", "defect_code", "stand_node_id" }

Свободные машины парка — списком словарей { "id", "call_sign", "lat",
"lon", "speed_kmh" }: для пешего кандидата система сравнит «пешком»
с «дойти до машины и доехать».
"""

import time
from operator import attrgetter

from constants import (
    REGULATION_ARRIVAL_LIMIT_MIN,
    REGULATION_WARNING_MIN,
    STATUS_FREE,
    STATUSES_BUSY,
)
from algorithm.qualification import find_qualification, required_mark
from algorithm.transport import best_plan, drive_distances_to, prepare_vehicles
from clock import utc_today

REASON_WRONG_SHIFT = "не на этой смене"
REASON_OFFLINE = "не на смене (offline)"
REASON_NO_ROUTE = "нет маршрута до стоянки выбранным способом передвижения"
REASON_NO_POSITION = "неизвестны координаты"

MESSAGE_OK = "Кандидат найден, регламент соблюдается"
MESSAGE_OVER_LIMIT = (
    "Ни один сотрудник с допуском не успевает за {limit} мин. "
    "Ближайший прибудет через {minutes} мин — превышение регламента, "
    "требуется решение диспетчера"
)
MESSAGE_NO_CANDIDATES = (
    "Нет ни одного свободного сотрудника с действующим допуском на {aircraft_type}"
)


class Candidate:
    """Сотрудник, прошедший фильтры допуска и доступности, с его маршрутом."""

    def __init__(self, employee, qualification, plan):
        self.employee = employee
        self.qualification = qualification
        self.route = plan
        self.minutes = plan.minutes
        # Укладывается ли в регламент 15 минут.
        self.within_regulation = self.minutes <= REGULATION_ARRIVAL_LIMIT_MIN
        # Жёлтая зона: успевает, но запас меньше трёх минут.
        self.near_limit = REGULATION_WARNING_MIN <= self.minutes <= REGULATION_ARRIVAL_LIMIT_MIN

    def as_dict(self):
        """Представление для API и панели диспетчера."""
        vehicle = self.route.vehicle
        return {
            "employee_id": self.employee["id"],
            "full_name": self.employee["full_name"],
            "mark": self.qualification["category"],
            "valid_until": self.qualification["valid_until"],
            "has_vehicle": vehicle is not None,
            "vehicle_call_sign": vehicle["call_sign"] if vehicle else None,
            "pickup": self.route.pickup,
            "minutes": round(self.minutes, 1),
            "within_regulation": self.within_regulation,
            "near_limit": self.near_limit,
            "route": self.route.as_dict(),
        }


class Suggestion:
    """
    Результат подбора целиком, включая объяснение отказов.

    Диспетчер отвечает за назначение лично, поэтому получает не только
    рекомендацию, но и основания: кого система отсеяла и почему, кто занят
    и когда освободится. Молчаливый выбор без объяснения в диспетчерской
    системе неприемлем — человек не сможет его оспорить.
    """

    def __init__(self, required, candidates, rejected, busy, elapsed_ms, aircraft_type):
        self.required_mark = str(required)
        self.candidates = candidates
        self.rejected = rejected
        self.busy = busy
        self.elapsed_ms = elapsed_ms

        self.best = candidates[0] if candidates else None
        self.within_regulation = bool(self.best and self.best.within_regulation)
        self.message = build_message(self.best, aircraft_type)

    def as_dict(self):
        """Представление для API."""
        return {
            "required_mark": self.required_mark,
            "within_regulation": self.within_regulation,
            "message": self.message,
            "elapsed_ms": round(self.elapsed_ms, 1),
            "best": self.best.as_dict() if self.best else None,
            "candidates": [candidate.as_dict() for candidate in self.candidates],
            "rejected": self.rejected,
            "busy": self.busy,
        }


def build_message(best, aircraft_type):
    """Формулирует вывод для диспетчера одной строкой."""
    if best is None:
        return MESSAGE_NO_CANDIDATES.format(aircraft_type=aircraft_type)
    if best.within_regulation:
        return MESSAGE_OK
    return MESSAGE_OVER_LIMIT.format(
        limit=REGULATION_ARRIVAL_LIMIT_MIN, minutes=round(best.minutes, 1)
    )


def suggest(graph, call, employees, shift, on_date=None, vehicles=None):
    """
    Подбирает исполнителя на вызов.

    Возвращает Suggestion всегда, в том числе когда подходящих нет:
    отсутствие кандидата — это тоже информация, которую диспетчер обязан
    получить немедленно, а не по таймауту.

    Аргументы:
        graph     — граф аэропорта вызова;
        call      — вызов (тип ВС, код дефекта, узел стоянки);
        employees — сотрудники этого аэропорта;
        shift     — текущая смена, контекст сессии диспетчера;
        on_date   — дата проверки срока действия отметок;
        vehicles  — свободные машины парка.
    """
    started = time.perf_counter()
    on_date = on_date or utc_today()
    stand_id = call["stand_node_id"]

    required = required_mark(call["defect_code"], call["aircraft_type"])

    # Общие для всех кандидатов расчёты по машинам: посадка машин на граф
    # и один проход Дейкстры на транспорте от стоянки (см. transport.py).
    prepared = prepare_vehicles(graph, vehicles or [])
    drive_map = drive_distances_to(graph, stand_id) if prepared else None

    candidates = []
    rejected = []
    busy = []

    for employee in employees:
        # Шаг 1. Смена. Сотрудник не на смене не рассматривается вообще,
        # независимо от координат и квалификации.
        if employee.get("shift") != shift:
            rejected.append(rejection(employee, REASON_WRONG_SHIFT))
            continue

        # Шаг 2. Допуск. Проверяется раньше доступности: занятость временна,
        # а отсутствие допуска — непреодолимо, и это главное, что нужно
        # объяснить диспетчеру.
        qualification, reason = find_qualification(
            employee, required, call["aircraft_type"], on_date
        )
        if qualification is None:
            rejected.append(rejection(employee, reason))
            continue

        # Шаг 3. Доступность. Занятые с допуском — отдельный список:
        # именно из них диспетчер выбирает во внештатной ситуации.
        status = employee.get("status")
        if status in STATUSES_BUSY:
            busy.append(busy_entry(employee, qualification))
            continue
        if status != STATUS_FREE:
            rejected.append(rejection(employee, REASON_OFFLINE))
            continue

        if employee.get("lat") is None or employee.get("lon") is None:
            rejected.append(rejection(employee, REASON_NO_POSITION))
            continue

        # Шаг 4. Способ добраться и время в пути: своя машина, пешком
        # или пешком до ближайшей свободной машины и дальше на ней.
        plan = best_plan(graph, employee, stand_id, prepared, drive_map)
        if plan is None:
            rejected.append(rejection(employee, REASON_NO_ROUTE))
            continue

        candidates.append(Candidate(employee, qualification, plan))

    # Шаг 5. Сортировка по времени в пути. Отсева по 15 минутам здесь нет
    # намеренно: превысивших регламент нельзя скрывать от диспетчера,
    # иначе во внештатной ситуации он останется вообще без вариантов.
    # Признак within_regulation несёт каждый кандидат отдельно.
    candidates.sort(key=attrgetter("minutes"))
    busy.sort(key=busy_sort_key)

    elapsed_ms = (time.perf_counter() - started) * 1000
    return Suggestion(
        required, candidates, rejected, busy, elapsed_ms, call["aircraft_type"]
    )


def rejection(employee, reason):
    """Запись об отсеянном сотруднике для панели диспетчера."""
    return {
        "employee_id": employee["id"],
        "full_name": employee["full_name"],
        "reason": reason,
    }


def busy_entry(employee, qualification):
    """Запись о занятом сотруднике с допуском и временем освобождения."""
    return {
        "employee_id": employee["id"],
        "full_name": employee["full_name"],
        "mark": qualification["category"],
        "status": employee.get("status"),
        "busy_until": employee.get("busy_until"),
    }


def busy_sort_key(entry):
    """
    Занятые сортируются по времени освобождения, ближайшие первыми.

    Те, у кого срок освобождения неизвестен, уходят в конец списка:
    предлагать их диспетчеру раньше тех, по кому есть оценка, бессмысленно.
    """
    busy_until = entry.get("busy_until")
    if not busy_until:
        return (1, "")
    return (0, busy_until)
