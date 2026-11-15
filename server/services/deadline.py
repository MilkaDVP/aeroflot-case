"""
Контроль регламента прибытия в реальном времени.

Пятнадцать минут на прибытие — договорное условие со штрафами, и даже
небольшое опоздание стоит обслуживающей организации денег. Раньше система
проверяла регламент один раз — в момент назначения: «прибудет в 14:12,
успевает». Если потом инженер задержался у предыдущего борта, пошёл не той
рулёжкой или потерял машину, диспетчер узнавал об этом, только когда срок
уже истёк.

Теперь каждый активный вызов оценивается заново при каждом опросе:
прогноз прибытия пересчитывается от текущих координат исполнителя тем же
способом передвижения, что был выбран при назначении, и сравнивается
с контрольным сроком. Как только становится видно, что срок будет сорван,
вызов получает уровень тревоги, и диспетчер с начальником смены видят
красное уведомление — пока ещё можно вмешаться.

Отдельно оценивается, можно ли прогнозу верить. GPS на перроне теряет
сигнал у зданий и под крыльями, связь пропадает. Прогноз по точке
двухминутной давности молча показывал бы «успевает» человеку, который
давно стоит, поэтому устаревшие координаты — сами по себе тревога.

Модуль только читает: ни вызов, ни сотрудника он не меняет.
"""

import math
from datetime import timedelta

from fastapi import HTTPException

from algorithm.routing import route_from_point, shortest_path, travel_time_minutes
from clock import to_iso_utc, utc_now
from constants import (
    DEADLINE_MARGIN_MIN,
    DEVICE_SILENT_S,
    LOCATION_GPS,
    MODE_VEHICLE,
    MODE_WALK,
    POSITION_STALE_S,
    REGULATION_ARRIVAL_LIMIT_MIN,
    UNASSIGNED_ALERT_AFTER_MIN,
    VEHICLE_IN_USE,
    VEHICLE_RESERVED,
)
from graph_registry import get_graph
from models.call import (
    STATUS_ACCEPTED,
    STATUS_ARRIVED,
    STATUS_ASSIGNED,
    STATUS_CLOSED,
    STATUS_QUEUED,
)

# Уровни контроля. Цвет в интерфейсе следует уровню, а не наоборот.
LEVEL_OK = "ok"                  # успевает с запасом
LEVEL_WARN = "warn"              # успевает впритык — жёлтый
LEVEL_LATE = "late"              # по прогнозу опоздает — красный
LEVEL_OVERDUE = "overdue"        # срок истёк, а исполнителя у борта нет
LEVEL_UNASSIGNED = "unassigned"  # время идёт, а исполнителя всё нет
LEVEL_NO_SIGNAL = "no_signal"    # координаты устарели — прогнозу верить нельзя
LEVEL_PENDING = "pending"        # только поступил, назначение ещё в норме
LEVEL_ARRIVED = "arrived"        # исполнитель у борта
LEVEL_CLOSED = "closed"          # работа завершена

# По этим уровням диспетчер и начальник смены получают красное уведомление.
ALERT_LEVELS = (LEVEL_LATE, LEVEL_OVERDUE, LEVEL_UNASSIGNED, LEVEL_NO_SIGNAL)

EN_ROUTE_STATUSES = (STATUS_ASSIGNED, STATUS_ACCEPTED)

# Что делать — подсказка к тревоге. Решение остаётся за человеком.
ADVICE_REASSIGN = (
    "Свяжитесь с исполнителем; если он не успевает — снимите его "
    "и назначьте ближайшего свободного"
)
ADVICE_ASSIGN = "Назначьте исполнителя: время прибытия уже под угрозой"
ADVICE_SIGNAL = "Свяжитесь с исполнителем по рации и уточните, где он"


class Control:
    """Оценка одного вызова: уровень, объяснение и цифры, на которых она основана."""

    def __init__(self, level, reason, deadline_at, now):
        self.level = level
        self.reason = reason
        self.deadline_at = deadline_at
        self.minutes_left = minutes_until(deadline_at, now)
        self.forecast_at = None
        self.margin_min = None
        self.delay_min = None
        self.position_age_s = None
        self.advice = None

    @property
    def alert(self):
        return self.level in ALERT_LEVELS

    def as_dict(self):
        """Представление для API. Время — с явным часовым поясом."""
        return {
            "level": self.level,
            "alert": self.alert,
            "reason": self.reason,
            "advice": self.advice,
            "deadline_at": to_iso_utc(self.deadline_at),
            "minutes_left": self.minutes_left,
            "forecast_at": to_iso_utc(self.forecast_at),
            "margin_min": self.margin_min,
            "delay_min": self.delay_min,
            "position_age_s": self.position_age_s,
        }


def minutes_until(deadline, now):
    """
    Минуты до срока, округлённые до десятых.

    И текст причины, и поле minutes_left строятся из одного и того же числа.
    Раньше текст считался по точному значению, а клиент — по округлённому:
    при 10,98 мин в одной плашке стояло «ост. 11 мин» и «до срока 10 мин».
    """
    return round((deadline - now).total_seconds() / 60, 1)


def deadline_of(call):
    """
    Контрольный срок прибытия.

    Отсчёт — от сообщения экипажа либо от регистрации (см. started_at).
    """
    return started_at(call) + timedelta(minutes=REGULATION_ARRIVAL_LIMIT_MIN)


def started_at(call):
    """
    Момент, с которого идёт отсчёт по вызову.

    Сообщение экипажа, если вызов создан из входящей заявки: время, пока
    заявка ждала разбора в диспетчерской, уже потрачено из пятнадцати минут.
    Для вызова, заведённого диспетчером напрямую, — момент регистрации.
    """
    return call.reported_at or call.created_at


def assess(call, now=None):
    """Оценивает вызов на момент now."""
    now = now or utc_now()
    deadline = deadline_of(call)

    if call.status == STATUS_CLOSED:
        return Control(LEVEL_CLOSED, "Вызов закрыт", deadline, now)
    if call.status == STATUS_ARRIVED:
        return assess_arrived(call, deadline, now)
    if call.assigned_employee is None:
        return assess_unassigned(call, deadline, now)
    if call.status == STATUS_QUEUED:
        return assess_queued(call, deadline, now)
    if call.status in EN_ROUTE_STATUSES:
        return assess_en_route(call, deadline, now)
    return Control(LEVEL_PENDING, "Ожидает назначения", deadline, now)


def assess_arrived(call, deadline, now):
    """Исполнитель у борта: для разбора смены — успел или нет."""
    control = Control(LEVEL_ARRIVED, "Исполнитель у борта", deadline, now)
    if call.eta_at is not None:
        margin = (deadline - call.eta_at).total_seconds() / 60
        control.margin_min = round(margin, 1)
        control.reason = (
            f"Прибыл в срок, запас {minutes_text(margin)}"
            if margin >= 0
            else f"Прибыл с опозданием на {overrun_text(-margin)}"
        )
    return control


def assess_unassigned(call, deadline, now):
    """
    Исполнителя нет.

    Через несколько минут без назначения это уже тревога: даже лучшему
    кандидату нужно время на дорогу, и каждая минута поиска отнимается
    у него.
    """
    left = minutes_until(deadline, now)
    waited = (now - started_at(call)).total_seconds() / 60

    if left < 0:
        control = Control(
            LEVEL_OVERDUE,
            f"Срок прибытия истёк {overrun_text(-left)} назад, исполнитель не назначен",
            deadline,
            now,
        )
    elif waited >= UNASSIGNED_ALERT_AFTER_MIN:
        control = Control(
            LEVEL_UNASSIGNED,
            f"Вызов без исполнителя уже {minutes_text(waited)}, до срока {minutes_text(left)}",
            deadline,
            now,
        )
    else:
        return Control(
            LEVEL_PENDING, f"Ожидает назначения, до срока {minutes_text(left)}", deadline, now
        )

    control.advice = ADVICE_ASSIGN
    return control


def assess_queued(call, deadline, now):
    """
    Вызов в очереди к занятому инженеру.

    Живого прогноза здесь нет: инженер сначала закончит текущие работы.
    Время прибытия берётся из расчёта цепочки, который пересчитывается
    при каждом изменении очереди.
    """
    left = minutes_until(deadline, now)
    if left < 0:
        control = Control(
            LEVEL_OVERDUE,
            f"Срок прибытия истёк {overrun_text(-left)} назад, вызов ещё в очереди",
            deadline,
            now,
        )
        control.advice = ADVICE_REASSIGN
        return control

    if call.eta_at is None:
        return Control(LEVEL_PENDING, "В очереди, время прибытия не рассчитано", deadline, now)

    margin = (deadline - call.eta_at).total_seconds() / 60
    control = Control(margin_level(margin), queued_reason(margin), deadline, now)
    control.forecast_at = call.eta_at
    control.margin_min = round(margin, 1)
    if control.alert:
        control.advice = ADVICE_REASSIGN
    return control


def queued_reason(margin):
    """Формулировка для вызова в очереди."""
    if margin < 0:
        return f"В очереди: прибытие на {overrun_text(-margin)} позже срока"
    if margin < DEADLINE_MARGIN_MIN:
        return f"В очереди: впритык, запас {minutes_text(margin)}"
    return f"В очереди: успевает, запас {minutes_text(margin)}"


def assess_en_route(call, deadline, now):
    """
    Исполнитель назначен и едет: прогноз по его текущим координатам.

    Порядок проверок важен. Сначала — истёк ли срок: тогда никакой прогноз
    уже не поможет. Затем — можно ли верить координатам: прогноз по старой
    точке хуже отсутствия прогноза, он успокаивает. И только потом — запас.
    """
    employee = call.assigned_employee
    left = minutes_until(deadline, now)
    minutes = live_minutes(call, employee)
    age_s, problem = signal_problem(employee, now)

    if left < 0:
        control = Control(
            LEVEL_OVERDUE,
            f"Срок прибытия истёк {overrun_text(-left)} назад, исполнитель ещё в пути",
            deadline,
            now,
        )
        control.advice = ADVICE_REASSIGN
    elif minutes is None:
        control = Control(
            LEVEL_NO_SIGNAL,
            "Нет координат исполнителя или маршрута от них — прогноз невозможен",
            deadline,
            now,
        )
        control.advice = ADVICE_SIGNAL
    elif problem is not None:
        margin = (deadline - (now + timedelta(minutes=minutes))).total_seconds() / 60
        control = Control(
            LEVEL_NO_SIGNAL,
            f"{problem}: прогноз по последней точке ненадёжен ({margin_phrase(margin)})",
            deadline,
            now,
        )
        control.advice = ADVICE_SIGNAL
    else:
        margin = (deadline - (now + timedelta(minutes=minutes))).total_seconds() / 60
        control = Control(margin_level(margin), en_route_reason(margin), deadline, now)
        if control.alert:
            control.advice = ADVICE_REASSIGN

    control.position_age_s = None if age_s is None else round(age_s)
    if minutes is not None:
        forecast = now + timedelta(minutes=minutes)
        control.forecast_at = forecast
        control.margin_min = round((deadline - forecast).total_seconds() / 60, 1)
        if call.eta_at is not None:
            control.delay_min = round((forecast - call.eta_at).total_seconds() / 60, 1)
    return control


def en_route_reason(margin):
    """Формулировка для исполнителя в пути."""
    if margin < 0:
        return f"Не успевает: по прогнозу прибытие на {overrun_text(-margin)} позже срока"
    if margin < DEADLINE_MARGIN_MIN:
        return f"Впритык: запас до срока {minutes_text(margin)}"
    return f"Успевает, запас {minutes_text(margin)}"


def margin_phrase(margin):
    """Короткое «успевает / не успевает» для пояснения в скобках."""
    if margin < 0:
        return f"по ней опоздание на {overrun_text(-margin)}"
    return f"по ней запас {minutes_text(margin)}"


def margin_level(margin):
    """Уровень по запасу до срока в минутах."""
    if margin < 0:
        return LEVEL_LATE
    if margin < DEADLINE_MARGIN_MIN:
        return LEVEL_WARN
    return LEVEL_OK


def live_minutes(call, employee):
    """
    Сколько минут исполнителю ехать от текущих координат.

    Способ передвижения — тот, что выбран при назначении. Машина ещё ждёт
    (зарезервирована) — дойти до неё пешком и доехать. Машина при нём —
    ехать. Иначе — пешком. Сменить план на лету прогноз не пытается:
    он отвечает на вопрос «успеет ли по своему плану», а не «как лучше».
    """
    if employee is None or employee.lat is None or employee.lon is None:
        return None
    try:
        graph = get_graph(call.airport_icao)
    except HTTPException:
        return None

    vehicle = employee.vehicle
    if vehicle is not None and vehicle.status == VEHICLE_RESERVED and call.pickup_node_id:
        walk = route_from_point(
            graph, employee.lat, employee.lon, call.pickup_node_id, MODE_WALK, employee.speed_kmh
        )
        drive = shortest_path(graph, call.pickup_node_id, call.stand_node_id, MODE_VEHICLE)
        if walk is None or drive is None:
            return None
        return walk.minutes + travel_time_minutes(drive.distance_m, MODE_VEHICLE, vehicle.speed_kmh)

    if vehicle is not None and vehicle.status == VEHICLE_IN_USE:
        route = route_from_point(
            graph, employee.lat, employee.lon, call.stand_node_id, MODE_VEHICLE, vehicle.speed_kmh
        )
    else:
        route = route_from_point(
            graph, employee.lat, employee.lon, call.stand_node_id, MODE_WALK, employee.speed_kmh
        )
    return route.minutes if route is not None else None


def signal_problem(employee, now):
    """
    Можно ли верить координатам исполнителя.

    Возвращает возраст координат в секундах и описание проблемы (или None).
    Проверяется только настоящий GPS: точку, поставленную вручную, не
    обновляют намеренно. «Телефон не на связи» и «GPS не определяет место» —
    разные беды: в первом случае не дойдёт и вызов, во втором — дойдёт.
    """
    age = None
    if employee.location_at is not None:
        age = max((now - employee.location_at).total_seconds(), 0)

    if employee.location_source != LOCATION_GPS:
        return age, None

    silent_for = None
    if employee.seen_at is not None:
        silent_for = max((now - employee.seen_at).total_seconds(), 0)
    if silent_for is None or silent_for > DEVICE_SILENT_S:
        return age, f"Телефон исполнителя не на связи {seconds_text(silent_for)}"
    if age is None or age > POSITION_STALE_S:
        return age, f"GPS не определяет место {seconds_text(age)}"
    return age, None


def minutes_text(minutes):
    """
    Остаток, запас или ожидание: «7 мин», «меньше минуты».

    Округление вниз: система не должна обещать диспетчеру больше времени,
    чем есть. Клиент считает «ост. N мин» так же — две разные цифры в одной
    строке тревоги вызывали бы вопрос «так сколько?».
    """
    if minutes < 1:
        return "меньше минуты"
    return f"{math.floor(minutes)} мин"


def overrun_text(minutes):
    """
    Опоздание или просрочка: всегда целые минуты, округление вверх.

    Опоздание на 12 секунд — это «на 1 мин», а не «на 0 мин»: преуменьшать
    нарушение договорного срока нельзя.
    """
    return f"{max(1, math.ceil(minutes))} мин"


def seconds_text(seconds):
    """Длительность потери сигнала: секунды до двух минут, дальше минуты."""
    if seconds is None:
        return "давно"
    if seconds < 120:
        return f"{round(seconds)} с"
    return f"{round(seconds / 60)} мин"
