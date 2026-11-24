"""
Подготовка стенда: очистка базы перед испытанием и случайные данные.

Очистка убирает всё, что накопилось в работе: вызовы, переписку,
сообщения экипажей, действия, журнал. Учётные записи, аэропорты, люди,
машины, борта и справочники остаются; полигон возвращается к геоданным
организаторов. Перед очисткой сохраняется копия базы.

Случайные данные нужны, чтобы посмотреть историю, журнал, отчёты и экран
в работе. История пишется задним числом и честно помечена в журнале;
текущие вызовы создаются теми же функциями, что и в настоящей смене.
"""

import random
import sqlite3
from datetime import timedelta
from pathlib import Path

from fastapi import HTTPException
from sqlalchemy import text

from algorithm.qualification import find_qualification, required_mark_for_category
from clock import utc_now
from constants import STATUS_FREE, STATUS_OFFLINE, VEHICLE_FREE
from graph_registry import get_graph
from models.aircraft import Aircraft
from models.call import ACTIVE_CALL_STATUSES, Call
from models.call_message import AUTHOR_DISPATCHER, AUTHOR_ENGINEER, CallMessage
from models.defect_type import DefectType
from models.employee import Employee
from models.engineer_action import EngineerAction
from models.event_log import TRIGGERS, EventLog
from models.report import REPORT_ACCEPTED, REPORT_NEW, REPORT_REJECTED, Report
from models.shift_event import ShiftEvent
from models.vehicle import Vehicle
from services import drill, journal, mentor, polygon, tracking

KEEP_BACKUPS = 5
BACKUP_MARK = ".before-clear-"
SOURCE = "случайные данные"


# --- Очистка ------------------------------------------------------------------


def backup(db):
    """Копия файла базы рядом с ней. Без копии очистка не выполняется."""
    path = db.get_bind().url.database
    if not path or path == ":memory:" or db.get_bind().dialect.name != "sqlite":
        raise HTTPException(409, "Очистка с копией возможна только для файловой базы SQLite")
    source = Path(path)
    target = source.with_name(f"{source.stem}{BACKUP_MARK}{utc_now():%Y%m%d-%H%M%S}{source.suffix}")
    origin, copy = sqlite3.connect(source), sqlite3.connect(target)
    try:
        origin.backup(copy)
    finally:
        origin.close()
        copy.close()
    old = sorted(source.parent.glob(f"{source.stem}{BACKUP_MARK}*{source.suffix}"))
    for extra in old[:-KEEP_BACKUPS]:
        extra.unlink(missing_ok=True)
    return target.name


def clear(db, shift):
    """Стереть накопленное и вернуть исходное состояние. Коммит — за вызывающим."""
    name = backup(db)
    counts = {
        "calls": db.query(Call).count(), "events": db.query(EventLog).count(), "reports": db.query(Report).count(),
    }
    # Журнал защищён триггерами от правки; на время очистки они снимаются
    # в той же транзакции: не удалась очистка — защита на месте.
    for trigger in ("event_log_no_update", "event_log_no_delete"):
        db.execute(text(f"DROP TRIGGER IF EXISTS {trigger}"))
    for model in (CallMessage, EngineerAction, ShiftEvent, Report, Call, EventLog):
        db.query(model).delete(synchronize_session=False)
    for statement in TRIGGERS:
        db.execute(text(statement))
    db.expire_all()
    db.info.pop(journal.CHAIN_KEY, None)

    for person in db.query(Employee):
        person.busy_until = person.speed_kmh = None
        person.return_vehicle = person.return_to_waiting = False
        if person.waiting_lat is not None:
            person.lat, person.lon = person.waiting_lat, person.waiting_lon
        if person.status != STATUS_OFFLINE:
            person.status = STATUS_FREE
    for vehicle in db.query(Vehicle):
        vehicle.status, vehicle.employee, vehicle.speed_kmh = VEHICLE_FREE, None, None
    db.flush()
    if db.query(Employee.id).filter_by(airport_icao=polygon.ICAO).first():
        polygon.reset(db, shift)
    tracking.forget_points()
    drill.stop_all()
    db.flush()
    return {"backup": name, **counts}


# --- Случайные данные -----------------------------------------------------------

CREW = [
    "Командир сообщает о замечании, просим осмотреть", "Горит табло, экипаж ждёт у трапа",
    "Обнаружено при внешнем осмотре", "Замечание после посадки, повторяется", "Просим подойти до вылета",
]
DISPATCHER_SAYS = ["Инженер в пути?", "Вылет через час, успеваем?", "Нужна ли вторая пара рук?", "Сообщите по готовности"]
ENGINEER_SAYS = ["Осмотрел, приступаю", "Нужно 20 минут", "Замечание подтвердилось", "Готово, закрываю"]
PROBLEMS = ["Нет доступа к отсеку, нужна стремянка", "Не подходит переходник", "Не горит освещение стоянки", "Нужен второй человек"]
UNASSIGN = ["Инженер застрял на другом борту", "Нужен допуск другой категории", "Машина не завелась", "Борт переставлен"]
OVERRIDES = ["Знает этот борт", "Ближе по факту", "По просьбе экипажа"]
CANCELS = ["Экипаж снял замечание"]


def generate(db, context, days, count):
    """Случайная история за days дней и текущая обстановка. Коммит — за вызывающим."""
    icao = context.airport_icao
    boards = db.query(Aircraft).filter_by(airport_icao=icao).all()
    people = db.query(Employee).filter_by(airport_icao=icao, archived=False).all()
    if not boards or not people:
        raise HTTPException(409, "В аэропорту нет бортов или инженеров — случайные данные строить не на чем")
    defects = db.query(DefectType).filter_by(is_builtin=True).all()
    maker = History(db, context, boards, people, defects)
    maker.build(days, count)
    # Предполагаемое расписание: часть бортов прибыла, часть ещё ждут.
    for board in boards:
        board.scheduled_arrival_at = (utc_now() + timedelta(minutes=random.randint(-180, 240))).replace(second=0, microsecond=0)
        journal.aircraft_schedule(db, board)
    live = live_situation(db, context, boards, defects)
    db.flush()
    return {"history": maker.made, "outcomes": maker.outcomes, "live": live, "events": len(maker.events)}


class History:
    """Завершённые вызовы прошлых дней — с журналом, перепиской и сообщениями."""

    def __init__(self, db, context, boards, people, defects):
        self.db, self.icao, self.user = db, context.airport_icao, context.user
        self.boards, self.people, self.defects = boards, people, defects
        self.vehicles = db.query(Vehicle).filter_by(airport_icao=self.icao, archived=False).all()
        self.graph = get_graph(self.icao)
        self.free_at = {}
        self.events = []
        self.made = 0
        self.outcomes = {"в срок": 0, "опоздание": 0, "отменён": 0, "некого назначить": 0}

    def build(self, days, count):
        now = utc_now()
        # Последние часы оставлены текущей обстановке.
        span = days * 24 * 60 - 120
        last = now - timedelta(minutes=120)
        moments = []
        for _ in range(count):
            moment = last - timedelta(minutes=random.random() * span)
            # Днём вызовов больше: половина ночных переносится на день.
            if moment.hour < 5 and random.random() < 0.5:
                moment = min(moment + timedelta(hours=random.randint(5, 10)), last)
            moments.append(moment)
        # По порядку: занятость инженера складывается из его прошлых вызовов.
        for moment in sorted(moments):
            self.call(moment)
        self.noise(now, days)
        self.write()

    def log(self, at, type_, words, call_id=None, board=None, person=None, system=False, **params):
        self.events.append(dict(
            ts=at, type_=type_, text_=words, call_id=call_id, board=board, employee=person,
            actor=None if system else journal.actor_of(self.user), params=params, airport_icao=self.icao,
        ))

    def write(self):
        """Журнал пишется по порядку времени."""
        for event in sorted(self.events, key=lambda e: e["ts"]):
            type_, words = event.pop("type_"), event.pop("text_")
            journal.record(self.db, type_, words, **event)
        self.db.flush()

    def pick(self, mark, board, moment):
        """Допущенные инженеры: сначала свободные, затем кто освободится раньше."""
        able = [p for p in self.people
                if find_qualification(p.as_algorithm_dict(), mark, board.aircraft_type, moment.date())[0]]
        random.shuffle(able)
        return sorted(able, key=lambda p: max(self.free_at.get(p.id, moment), moment))

    def call(self, start):
        board, defect = random.choice(self.boards), random.choice(self.defects)
        mark = required_mark_for_category(defect.category, board.aircraft_type)
        able = self.pick(mark, board, start)
        kind = random.choices(["ok", "late", "cancel", "swap"], [58, 18, 12, 12])[0] if able else "nobody"
        from_crew = random.random() < 0.35
        values = dict(
            airport_icao=self.icao, aircraft_id=board.id, stand_node_id=board.stand_node_id,
            defect_code=defect.code, defect_description=random.choice([None, *CREW]), required_mark=str(mark),
            created_at=start + timedelta(minutes=random.uniform(0.5, 3) if from_crew else 0),
            reported_at=start if from_crew else None, source="экипаж" if from_crew else None,
            urgent=random.random() < 0.2, aog=False, missing_tools=False, status="closed",
        )
        registered = values["created_at"]
        messages, flow = [], []

        if kind in ("cancel", "nobody"):
            at = registered + timedelta(minutes=random.uniform(4, 25))
            values.update(status="cancelled", closed_at=at)
            flow.append((registered, "new", "suggested", None))
            flow.append((at, "suggested", "cancelled", None))
            self.outcomes["отменён" if kind == "cancel" else "некого назначить"] += 1
        else:
            person = able[0]
            begin = max(registered, self.free_at.get(person.id, registered))
            assigned = begin + timedelta(minutes=random.uniform(0.3, 2.5) if kind != "late" else random.uniform(3, 8))
            swapped = None
            if kind == "swap" and len(able) > 1:
                swapped, person = person, able[1]
                taken = assigned + timedelta(minutes=random.uniform(1, 4))
                reason = random.choice(UNASSIGN)
                values.update(previous_employee_id=swapped.id, unassign_reason=reason, unassigned_at=taken)
                again = max(taken, self.free_at.get(person.id, taken)) + timedelta(minutes=random.uniform(0.5, 2))
            else:
                again = assigned
            accepted = again + timedelta(seconds=1)
            travel = random.uniform(2, 8) if kind != "late" else random.uniform(8, 18)
            arrived = accepted + timedelta(minutes=travel)
            waits = random.random() < 0.15
            work = random.uniform(8, 40) + (random.uniform(20, 60) if waits else 0)
            closed = min(arrived + timedelta(minutes=work), utc_now() - timedelta(minutes=1))
            vehicle = random.choice(self.vehicles) if self.vehicles and random.random() < 0.55 else None
            override = random.choice(OVERRIDES) if random.random() < 0.1 else None
            values.update(
                assigned_employee_id=person.id, suggested_employee_id=(able[-1] if override else person).id,
                override_reason=override, first_arrived_at=arrived, eta_at=arrived, eta_minutes=round(travel, 1),
                closed_at=closed, work_stage="repair", vehicle_id=vehicle.id if vehicle else None,
                mel_category=random.choice("AABBCCD"), aog=random.random() < 0.08, missing_tools=random.random() < 0.08,
                arrival_mode=random.choice(["auto", "auto", "manual"]), route_node_ids=self.route(person, board),
            )
            self.free_at[person.id] = closed
            flow += [(registered, "new", "suggested", None), (assigned, "suggested", "assigned", swapped or person)]
            if swapped:
                flow += [(taken, "assigned", "new", None), (again, "new", "assigned", person)]
            flow += [(accepted, "assigned", "accepted", person), (arrived, "accepted", "arrived", person),
                     (closed, "arrived", "closed", person)]
            self.outcomes["в срок" if (arrived - start).total_seconds() <= 900 else "опоздание"] += 1
            messages = self.talk(person, accepted, arrived, closed)

        call_id = self.db.execute(Call.__table__.insert().values(**values)).inserted_primary_key[0]
        self.made += 1
        for message in messages:
            self.db.add(CallMessage(call_id=call_id, **message))
        if from_crew:
            self.db.add(Report(
                airport_icao=self.icao, board_number=board.board_number, aircraft_id=board.id,
                description=values["defect_description"] or random.choice(CREW), reporter="Экипаж", source="crew-app",
                reported_at=start, received_at=start, status=REPORT_ACCEPTED, call_id=call_id,
                processed_by=self.user.full_name, processed_at=registered))
            self.log(start, "report_received", f"Сообщение экипажа: {board.board_number}", call_id, board.board_number)
        self.story(call_id, board, defect, values, flow, start, kind, len(able))
        if values.get("first_arrived_at"):
            self.db.flush()
            record = self.db.get(Call, call_id)
            mentor.review_arrival(self.db, record, record.assigned_employee, values["first_arrived_at"])

    def route(self, person, board):
        from algorithm.routing import route_from_point

        if person.lat is None:
            return None
        route = route_from_point(self.graph, person.lat, person.lon, board.stand_node_id)
        return route.node_ids if route else None

    def talk(self, person, accepted, arrived, closed):
        result = []
        if random.random() < 0.5:
            result.append(dict(author_role=AUTHOR_DISPATCHER, author_name=self.user.full_name,
                               text=random.choice(DISPATCHER_SAYS), created_at=accepted + timedelta(seconds=30)))
            result.append(dict(author_role=AUTHOR_ENGINEER, author_name=person.full_name,
                               text=random.choice(ENGINEER_SAYS), created_at=arrived + timedelta(minutes=1)))
        if random.random() < 0.15:
            fixed = random.random() < 0.8
            result.append(dict(author_role=AUTHOR_ENGINEER, author_name=person.full_name, is_problem=True,
                               text=random.choice(PROBLEMS), created_at=arrived + timedelta(minutes=2),
                               resolved_at=min(closed, arrived + timedelta(minutes=12)) if fixed else None,
                               resolved_by=self.user.full_name if fixed else None))
        return result

    def story(self, call_id, board, defect, values, flow, start, kind, able):
        """Журнал одного вызова — теми же типами событий, что пишет система."""
        number = board.board_number
        deadline = start + timedelta(minutes=15)
        self.log(values["created_at"], "call_created", f"Заявка зафиксирована: {defect.name}, источник — "
                 f"{values['source'] or 'диспетчер'}", call_id, number, неисправность=defect.name, происхождение=SOURCE)
        self.log(values["created_at"] + timedelta(seconds=1), "candidates",
                 f"Список кандидатов: подходят {able}" + ("" if able else "; подходящих нет"), call_id, number)
        for at, old, new, person in flow:
            who = person.full_name if person else None
            if new == "assigned":
                self.log(at, "reassigned" if values.get("unassigned_at") and at > values["unassigned_at"] else "assigned",
                         f"Назначен {who}", call_id, number, person, исполнитель=who,
                         переопределение=bool(values.get("override_reason")))
                if values.get("route_node_ids"):
                    self.log(at, "route_built", f"Маршрут построен: {len(values['route_node_ids'])} узл.", call_id, number, person)
            if old == "assigned" and new == "new":
                gone = self.db.get(Employee, values["previous_employee_id"])
                self.log(at, "unassigned", f"Снят исполнитель {gone.full_name}: {values['unassign_reason']}",
                         call_id, number, gone, снят=gone.full_name, причина=values["unassign_reason"])
            self.log(at, "status", f"Статус: {journal.STATUS_TITLES[old]} → {journal.STATUS_TITLES[new]}",
                     call_id, number, person, было=old, стало=new)
        arrived = values.get("first_arrived_at")
        if arrived:
            spent = (arrived - start).total_seconds() / 60
            person = self.db.get(Employee, values["assigned_employee_id"])
            self.log(arrived, "arrival", f"Прибыл к ВС через {spent:.1f} мин после фиксации", call_id, number, person,
                     затрачено_мин=round(spent, 1), норматив_мин=15)
            self.log(arrived + timedelta(seconds=1), "call_flags", "Изменено: этап работ нет → inspection", call_id, number,
                     **{"этап работ": {"было": None, "стало": "inspection"}})
            if (values["closed_at"] - arrived).total_seconds() > 45 * 60:
                for minute, old, new in ((10, "inspection", "waiting_parts"), (35, "waiting_parts", "repair")):
                    self.log(arrived + timedelta(minutes=minute), "call_flags", f"Изменено: этап работ {old} → {new}",
                             call_id, number, **{"этап работ": {"было": old, "стало": new}})
        if (arrived or values["closed_at"]) > deadline:
            self.log(deadline - timedelta(minutes=2), "deadline_risk", "Риск нарушения норматива", call_id, number, system=True)
            if arrived:
                self.log(deadline, "deadline_violation", "Нарушение норматива: инженер ещё не у борта", call_id, number, system=True)
        elif arrived:
            self.log(arrived, "deadline_met", "Норматив 15 мин выполнен", call_id, number, system=True)

    def noise(self, now, days):
        """Входы, отказы и ошибки — чтобы в журнале были все разделы."""
        for _ in range(days * 3):
            at = now - timedelta(minutes=random.uniform(60, days * 1440))
            type_, words = random.choice([
                ("login", f"Вход: {self.user.full_name}"), ("login_failed", "Неудачный вход: неверный пароль"),
                ("action_rejected", "Отклонено: вызов уже назначен"), ("data_error", "Точка отбракована: низкая точность"),
                ("user_action", "Изменена срочность вызова"),
            ])
            self.log(at, type_, words, system=type_ in ("data_error", "login_failed"), происхождение=SOURCE)
        for _ in range(days):
            board = random.choice(self.boards)
            at = now - timedelta(minutes=random.uniform(180, days * 1440))
            self.db.add(Report(
                airport_icao=self.icao, board_number=board.board_number, aircraft_id=board.id,
                description=random.choice(CREW), reporter="Экипаж", source="crew-app", reported_at=at, received_at=at,
                status=REPORT_REJECTED, resolution_note="Повторное сообщение", processed_by=self.user.full_name,
                processed_at=at + timedelta(minutes=3)))


def live_situation(db, context, boards, defects):
    """Текущая обстановка разными вызовами — обычными функциями системы."""
    from api.calls import depots_for, new_call, run_suggestion
    from services.assignment import assign_now, enqueue, mark_arrived, plan_route, remaining_waypoints

    icao, now = context.airport_icao, utc_now()
    busy = {row[0] for row in db.query(Call.aircraft_id).filter(Call.status.in_(ACTIVE_CALL_STATUSES))}
    spare = [b for b in boards if b.id not in busy]
    random.shuffle(spare)
    made = []

    def start(minutes_ago, **flags):
        if not spare:
            return None
        call = new_call(db, spare.pop(), random.choice(defects), random.choice(CREW), now - timedelta(minutes=minutes_ago), "экипаж")
        for key, value in flags.items():
            setattr(call, key, value)
        db.flush()
        return call

    def send(call):
        best = run_suggestion(db, context, call).best
        if best is None:
            return None
        person = db.get(Employee, best.employee["id"])
        call.suggested_employee_id = person.id
        plan, vehicle = plan_route(db, person, call.stand_node_id, get_graph(icao), "auto", None,
                                   depots_for(call, get_graph(icao)), remaining_waypoints(call))
        if plan is None:
            return None
        assign_now(db, call, person, plan, vehicle)
        db.flush()
        return person

    def accept(call, person):
        call.status, person.status = "accepted", "en_route"
        db.flush()

    def arrive(call, person, **flags):
        accept(call, person)
        call.status, person.status = "arrived", "busy"
        node = get_graph(icao).node(call.stand_node_id)
        person.lat, person.lon = node["lat"], node["lon"]
        mark_arrived(db, person, call, now)
        for key, value in flags.items():
            setattr(call, key, value)
        db.flush()

    def say(call, person, words, problem=False):
        db.add(CallMessage(call_id=call.id, author_role=AUTHOR_ENGINEER, author_name=person.full_name,
                           text=words, is_problem=problem, created_at=now))

    worker = None
    for title, age, flags in (("у борта, ждёт запчасти", 25, {}), ("у борта, AOG и проблема", 12, {"urgent": True}),
                              ("едет к борту", 6, {}), ("только назначен", 3, {})):
        call = start(age, **flags)
        person = send(call) if call else None
        if person is None:
            # Нет свободного с допуском: вызов остаётся ждать назначения.
            if call:
                made.append("ждёт назначения")
            continue
        if title.startswith("у борта, ждёт"):
            arrive(call, person, work_stage="waiting_parts", missing_tools=True, mel_category="C")
            worker = worker or person
        elif title.startswith("у борта"):
            arrive(call, person, aog=True)
            say(call, person, random.choice(PROBLEMS), True)
            worker = worker or person
        elif title.startswith("едет"):
            accept(call, person)
            say(call, person, "Еду, буду через несколько минут")
        made.append(title)
    if worker:
        call = start(4)
        if call:
            enqueue(db, call, worker)
            made.append("в очереди к занятому")
    for title, age in (("новый вызов", 1), ("горит: ждёт назначения 13 минут", 13)):
        if start(age):
            made.append(title)
    for _ in range(2):
        board = random.choice(boards)
        db.add(Report(airport_icao=icao, board_number=board.board_number, aircraft_id=board.id,
                      description=random.choice(CREW), reporter="Экипаж", source="crew-app",
                      reported_at=now - timedelta(minutes=2), received_at=now, status=REPORT_NEW))
    made.append("2 входящих сообщения экипажа")
    return made
