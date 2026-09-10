"""
Наполнение базы демонстрационными данными.

Данные подобраны не случайно: расстановка сотрудников и их квалификации
рассчитаны так, чтобы каждый контрольный сценарий из требований задания
воспроизводился на реальном графе Шереметьево без ручной подготовки.
Жюри разворачивает проект и сразу видит все интересные случаи.

Сценарии, заложенные в данные:
  1. Разное расположение — на вызов у стоянки 31 претендуют сотрудники
     из двух техцентров и с перрона, побеждает не тот, кто ближе по прямой.
  2. Разная специализация — у стоянки 31 стоит Волков с допуском только
     на B777; на вызов к A320 он не годится, хотя он ближе всех.
  3. Истёкшая отметка — Орлов рядом, но срок действия его отметки прошёл.
  4. Спецтранспорт — Соколов на машине обгоняет пешего Морозова из того же
     техцентра, время в пути отличается в четыре с лишним раза.
  5. Все с допуском заняты — Гусев работает на стоянке 40 по настоящему
     вызову (VP-BKB, износ тормозов) и освободится позже. Вызов заведён
     в базу намеренно: занятость без вызова — это человек, «занятый»
     неизвестно чем, и очередь к нему никогда бы не продвинулась.
  6. Нет допуска ни у кого — борт A330 на стоянке 100: подходящей
     квалификации нет ни у одного сотрудника смены.
  7. Фильтр смены — Тарасов и Лебедев в ночной смене и днём не видны.
"""

from datetime import timedelta

from auth.security import hash_password
from clock import utc_now
from constants import (
    DEFAULT_WORK_DURATION_MIN,
    SHIFT_DAY,
    SHIFT_NIGHT,
    STATUS_BUSY,
    STATUS_FREE,
    STATUS_OFFLINE,
    STATUSES_BUSY,
)
from database import SessionLocal
from graph_registry import available_icaos, get_graph
from models.aircraft import Aircraft
from models.airport import Airport
from models.call import STATUS_ARRIVED as CALL_ARRIVED
from models.call import Call
from models.employee import Employee
from models.user import (
    ROLE_ADMIN,
    ROLE_DISPATCHER,
    ROLE_ENGINEER,
    ROLE_SHIFT_SUPERVISOR,
    User,
)

DEMO_AIRPORT = "UUEE"

# Срок действия отметок: один заведомо действующий, один заведомо истёкший.
VALID_UNTIL = "2027-12-31"
EXPIRED_AT = "2026-01-10"

# Борта на стоянках: номер, тип, стоянка.
AIRCRAFT_ON_STANDS = [
    ("VP-BZQ", "A320", "31"),
    ("VP-BES", "A321", "33"),
    ("RA-89001", "SSJ-100", "35"),
    ("VP-BKB", "B737", "40"),
    ("VP-BGB", "B777", "60"),
    ("VQ-BQX", "A330", "100"),
]

# Сотрудники: ФИО, отметка, типы ВС, срок, смена, статус, где стоит,
# спецтранспорт. Место задаётся либо номером стоянки, либо техцентром.
ENGINEERS = [
    ("Соколов А. В.", "B1.1", ["A320", "A321", "SSJ-100"], VALID_UNTIL,
     SHIFT_DAY, STATUS_FREE, ("tech", "ТЦ-Запад"), True),
    ("Морозов Д. С.", "B1.1", ["A320", "A321"], VALID_UNTIL,
     SHIFT_DAY, STATUS_FREE, ("tech", "ТЦ-Запад"), False),
    ("Никитин П. А.", "B2", ["A320", "B737", "B777"], VALID_UNTIL,
     SHIFT_DAY, STATUS_FREE, ("tech", "ТЦ-Север"), True),
    ("Волков И. Н.", "B1.1", ["B777"], VALID_UNTIL,
     SHIFT_DAY, STATUS_FREE, ("stand", "31"), False),
    ("Орлов С. М.", "B1.1", ["A320", "A321"], EXPIRED_AT,
     SHIFT_DAY, STATUS_FREE, ("stand", "33"), False),
    ("Кузнецов В. П.", "A1", ["A320", "B737"], VALID_UNTIL,
     SHIFT_DAY, STATUS_FREE, ("stand", "60"), False),
    ("Гусев Р. О.", "B1.1", ["A320", "B737"], VALID_UNTIL,
     SHIFT_DAY, STATUS_BUSY, ("stand", "40"), False),
    ("Фомин Н. Д.", "A1", ["A320", "A321", "B737"], VALID_UNTIL,
     SHIFT_DAY, STATUS_FREE, ("tech", "ТЦ-Восток"), True),
    ("Зайцев М. А.", "B1.3", ["Ми-8"], VALID_UNTIL,
     SHIFT_DAY, STATUS_FREE, ("tech", "ТЦ-Север"), False),
    ("Егоров А. А.", "C", ["A320", "A321", "B777"], VALID_UNTIL,
     SHIFT_DAY, STATUS_FREE, ("tech", "ТЦ-Запад"), False),
    ("Тарасов Е. И.", "B1.1", ["A320", "A321"], VALID_UNTIL,
     SHIFT_NIGHT, STATUS_OFFLINE, ("tech", "ТЦ-Запад"), True),
    ("Лебедев К. Ю.", "B2", ["A320", "B777"], VALID_UNTIL,
     SHIFT_NIGHT, STATUS_OFFLINE, ("tech", "ТЦ-Север"), False),
]

# Учётные записи. Пароли простые намеренно: это конкурсный стенд,
# логины и пароли публикуются в README для проверки жюри.
USERS = [
    ("dispatcher", "dispatcher", ROLE_DISPATCHER, "Смирнова О. В.", None),
    ("supervisor", "supervisor", ROLE_SHIFT_SUPERVISOR, "Белов А. Г.", None),
    ("admin", "admin", ROLE_ADMIN, "Администратор системы", None),
    ("sokolov", "engineer", ROLE_ENGINEER, "Соколов А. В.", "Соколов А. В."),
    ("morozov", "engineer", ROLE_ENGINEER, "Морозов Д. С.", "Морозов Д. С."),
    ("nikitin", "engineer", ROLE_ENGINEER, "Никитин П. А.", "Никитин П. А."),
]

# Предзагруженные вызовы: борт и код дефекта.
DEMO_CALLS = [
    ("VP-BZQ", "hydraulic_leak"),
    ("VQ-BQX", "engine_start_fault"),
]

# Вызов, на котором уже работает Гусев: борт, дефект, исполнитель и сколько
# минут назад он прибыл на стоянку.
WORKING_DEMO_CALL = ("VP-BKB", "brake_wear", "Гусев Р. О.", 5)


def node_coordinates(graph, place):
    """Координаты по описанию места: стоянка или техцентр."""
    kind, ref = place
    if kind == "stand":
        node = graph.find_stand(ref)
    else:
        node = next(
            (item for item in graph.nodes_of_type("tech_center") if item["ref"] == ref),
            None,
        )
    if node is None:
        raise ValueError(f"в графе нет объекта {place}")
    return node


def seed_airports(db):
    """Карточки аэропортов по имеющимся на диске графам."""
    for icao in available_icaos():
        if db.get(Airport, icao) is None:
            graph = get_graph(icao)
            db.add(Airport(icao=icao, name=graph.name, city=graph.city))
    db.commit()


def seed_employees(db, graph):
    """Сотрудники ОТО с квалификациями и расстановкой по перрону."""
    created = {}
    for full_name, mark, types, valid_until, shift, status, place, vehicle in ENGINEERS:
        node = node_coordinates(graph, place)
        employee = Employee(
            full_name=full_name,
            airport_icao=DEMO_AIRPORT,
            shift=shift,
            status=status,
            lat=node["lat"],
            lon=node["lon"],
            has_vehicle=vehicle,
            qualifications=[
                {
                    "category": mark,
                    "aircraft_types": types,
                    "valid_until": valid_until,
                }
            ],
            # Точное время освобождения занятого выставит его вызов
            # в seed_calls; здесь — заведомо будущее время на случай,
            # если вызова для него не заведено.
            busy_until=(
                utc_now() + timedelta(minutes=25)
                if status in STATUSES_BUSY
                else None
            ),
        )
        db.add(employee)
        created[full_name] = employee

    db.commit()
    return created


def seed_users(db, employees):
    """Учётные записи всех ролей, включая привязку инженеров к карточкам."""
    airport = db.get(Airport, DEMO_AIRPORT)
    for login, password, role, full_name, employee_name in USERS:
        employee = employees.get(employee_name) if employee_name else None
        user = User(
            login=login,
            password_hash=hash_password(password),
            role=role,
            full_name=full_name,
            employee_id=employee.id if employee else None,
        )
        if airport:
            user.airports.append(airport)
        db.add(user)
    db.commit()


def seed_aircraft(db, graph):
    """Борта на стоянках."""
    created = {}
    for board_number, aircraft_type, stand_ref in AIRCRAFT_ON_STANDS:
        node = graph.find_stand(stand_ref)
        if node is None:
            continue
        aircraft = Aircraft(
            board_number=board_number,
            aircraft_type=aircraft_type,
            airport_icao=DEMO_AIRPORT,
            stand_node_id=node["id"],
        )
        db.add(aircraft)
        created[board_number] = aircraft
    db.commit()
    return created


def seed_calls(db, aircraft, employees):
    """Открытые вызовы, чтобы рабочий экран не был пустым при первом входе."""
    from algorithm.qualification import required_mark

    for board_number, defect_code in DEMO_CALLS:
        board = aircraft.get(board_number)
        if board is None:
            continue
        db.add(
            Call(
                airport_icao=DEMO_AIRPORT,
                aircraft_id=board.id,
                stand_node_id=board.stand_node_id,
                defect_code=defect_code,
                required_mark=str(required_mark(defect_code, board.aircraft_type)),
            )
        )

    seed_working_call(db, aircraft, employees)
    db.commit()


def seed_working_call(db, aircraft, employees):
    """
    Вызов, на котором уже работает занятый сотрудник.

    Время освобождения сотрудника выводится из вызова (прибыл N минут
    назад плюс норматив работ), а не задаётся отдельно: два независимых
    числа рано или поздно разошлись бы.
    """
    from algorithm.qualification import required_mark

    board_number, defect_code, full_name, arrived_min_ago = WORKING_DEMO_CALL
    board = aircraft.get(board_number)
    employee = employees.get(full_name)
    if board is None or employee is None:
        return

    arrived_at = utc_now() - timedelta(minutes=arrived_min_ago)
    db.add(
        Call(
            airport_icao=DEMO_AIRPORT,
            aircraft_id=board.id,
            stand_node_id=board.stand_node_id,
            defect_code=defect_code,
            required_mark=str(required_mark(defect_code, board.aircraft_type)),
            status=CALL_ARRIVED,
            created_at=arrived_at - timedelta(minutes=6),
            suggested_employee_id=employee.id,
            assigned_employee_id=employee.id,
            eta_minutes=0.0,
            eta_at=arrived_at,
            route_node_ids=[board.stand_node_id],
        )
    )
    employee.busy_until = arrived_at + timedelta(minutes=DEFAULT_WORK_DURATION_MIN)


def seed_if_empty(verbose=True):
    """
    Наполняет базу, если она пуста.

    Повторный запуск ничего не делает: перезаписывать данные, с которыми
    уже работали во время демонстрации, недопустимо.
    """
    db = SessionLocal()
    try:
        if db.query(User).count() > 0:
            return False

        seed_airports(db)
        if DEMO_AIRPORT not in available_icaos():
            print(
                f"Граф {DEMO_AIRPORT} не найден — демонстрационные данные не созданы. "
                f"Выполните: python data/fetch_airport.py {DEMO_AIRPORT}"
            )
            return False

        graph = get_graph(DEMO_AIRPORT)
        employees = seed_employees(db, graph)
        seed_users(db, employees)
        aircraft = seed_aircraft(db, graph)
        seed_calls(db, aircraft, employees)

        if verbose:
            print(
                f"База наполнена: {len(employees)} сотрудников, "
                f"{len(aircraft)} бортов, {len(USERS)} учётных записей"
            )
        return True
    finally:
        db.close()


if __name__ == "__main__":
    seed_if_empty()
