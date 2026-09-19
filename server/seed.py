"""
Наполнение базы демонстрационными данными.

Данные подобраны не случайно: расстановка сотрудников и их квалификации
рассчитаны так, чтобы каждый контрольный сценарий из требований задания
воспроизводился на реальных графах без ручной подготовки. Жюри
разворачивает проект и сразу видит все интересные случаи.

Аэропортов два, и это не украшение: §5 требует шага выбора аэропорта,
а при единственном аэропорте этот экран никогда не показывается.
Диспетчер Смирнова привязана к обоим, начальник смены Белов — только
к Шереметьево: на нём проверяется запрет чужого аэропорта.

Сценарии в данных Шереметьево (UUEE):
  1. Разное расположение — на вызов у стоянки 31 претендуют сотрудники
     из двух техцентров и с перрона, побеждает не тот, кто ближе по прямой.
  2. Разная специализация — у стоянки 31 стоит Волков с допуском только
     на B777; на вызов к A320 он не годится, хотя он ближе всех.
  3. Истёкшая отметка — Орлов рядом, но срок действия его отметки прошёл.
  4. Спецтранспорт — Соколов на машине ТМ-01 приедет к стоянке 31 за 6.2 мин.
     Морозов из того же техцентра без машины: пешком — 27.3 мин, но в трёхстах
     метрах стоит свободная ТМ-04, и система предлагает дойти до неё —
     9.5 мин. Это и есть «учёт наличия поблизости спецтранспорта» из §10.
  5. Все с допуском заняты — Гусев работает на стоянке 40 по настоящему
     вызову (VP-BKB, износ тормозов) и освободится позже. Вызов заведён
     в базу намеренно: занятость без вызова — это человек, «занятый»
     неизвестно чем, и очередь к нему никогда бы не продвинулась.
  6. Нет допуска ни у кого — борт A330 на стоянке 100: подходящей
     квалификации нет ни у одного сотрудника смены.
  7. Фильтр смены — Тарасов и Лебедев в ночной смене и днём не видны.

Домодедово (UUDD) — второй аэропорт, данные компактнее. Там пешком
в регламент попадают 2 стоянки из 73, поэтому спецтранспорт решает всё:
на вызов к дальней стоянке 84R Беляев едет на своей ДМ-01, а Гончаров
из того же техцентра идёт 284 метра до свободной ДМ-02 и едет на ней.
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
    VEHICLE_FREE,
    VEHICLE_IN_USE,
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
from models.vehicle import Vehicle

# Срок действия отметок: один заведомо действующий, один заведомо истёкший.
VALID_UNTIL = "2027-12-31"
EXPIRED_AT = "2026-01-10"

# --- Шереметьево ------------------------------------------------------------

SHEREMETYEVO = {
    "icao": "UUEE",
    # Борта на стоянках: номер, тип, стоянка.
    "aircraft": [
        ("VP-BZQ", "A320", "31"),
        ("VP-BES", "A321", "33"),
        ("RA-89001", "SSJ-100", "35"),
        ("VP-BKB", "B737", "40"),
        ("VP-BGB", "B777", "60"),
        ("VQ-BQX", "A330", "100"),
    ],
    # Сотрудники: ФИО, отметка, типы ВС, срок, смена, статус, где стоит.
    # Место задаётся либо номером стоянки, либо техцентром. Машин здесь нет:
    # машина — не свойство сотрудника, она описана отдельно.
    "engineers": [
        ("Соколов А. В.", "B1.1", ["A320", "A321", "SSJ-100"], VALID_UNTIL,
         SHIFT_DAY, STATUS_FREE, ("tech", "ТЦ-Запад")),
        ("Морозов Д. С.", "B1.1", ["A320", "A321"], VALID_UNTIL,
         SHIFT_DAY, STATUS_FREE, ("tech", "ТЦ-Запад")),
        ("Никитин П. А.", "B2", ["A320", "B737", "B777"], VALID_UNTIL,
         SHIFT_DAY, STATUS_FREE, ("tech", "ТЦ-Север")),
        ("Волков И. Н.", "B1.1", ["B777"], VALID_UNTIL,
         SHIFT_DAY, STATUS_FREE, ("stand", "31")),
        ("Орлов С. М.", "B1.1", ["A320", "A321"], EXPIRED_AT,
         SHIFT_DAY, STATUS_FREE, ("stand", "33")),
        ("Кузнецов В. П.", "A1", ["A320", "B737"], VALID_UNTIL,
         SHIFT_DAY, STATUS_FREE, ("stand", "60")),
        ("Гусев Р. О.", "B1.1", ["A320", "B737"], VALID_UNTIL,
         SHIFT_DAY, STATUS_BUSY, ("stand", "40")),
        ("Фомин Н. Д.", "A1", ["A320", "A321", "B737"], VALID_UNTIL,
         SHIFT_DAY, STATUS_FREE, ("tech", "ТЦ-Восток")),
        ("Зайцев М. А.", "B1.3", ["Ми-8"], VALID_UNTIL,
         SHIFT_DAY, STATUS_FREE, ("tech", "ТЦ-Север")),
        ("Егоров А. А.", "C", ["A320", "A321", "B777"], VALID_UNTIL,
         SHIFT_DAY, STATUS_FREE, ("tech", "ТЦ-Запад")),
        ("Тарасов Е. И.", "B1.1", ["A320", "A321"], VALID_UNTIL,
         SHIFT_NIGHT, STATUS_OFFLINE, ("tech", "ТЦ-Запад")),
        ("Лебедев К. Ю.", "B2", ["A320", "B777"], VALID_UNTIL,
         SHIFT_NIGHT, STATUS_OFFLINE, ("tech", "ТЦ-Север")),
    ],
    # Машины парка: позывной, тип, где. («employee», ФИО) — машина сейчас
    # у этого сотрудника и едет с ним; иначе — стоит свободной у узла графа
    # или у стоянки.
    "vehicles": [
        ("ТМ-01", "Техпомощь", ("employee", "Соколов А. В.")),
        ("ТМ-02", "Техпомощь", ("employee", "Никитин П. А.")),
        ("ТМ-03", "Техпомощь", ("employee", "Фомин Н. Д.")),
        # Свободная машина в 326 м от ТЦ-Запад: на ней держится демонстрация
        # «дойти до ближайшей машины» для пешего Морозова (сценарий 4).
        ("ТМ-04", "Техпомощь", ("node", "n5865221946")),
        ("ТМ-05", "Техпомощь", ("stand", "60")),
    ],
    # Предзагруженные вызовы: борт и код дефекта.
    "calls": [
        ("VP-BZQ", "hydraulic_leak"),
        ("VQ-BQX", "engine_start_fault"),
    ],
    # Вызов, на котором уже работает занятый сотрудник: борт, дефект,
    # исполнитель и сколько минут назад он прибыл на стоянку.
    "working_call": ("VP-BKB", "brake_wear", "Гусев Р. О.", 5),
}

# --- Домодедово -------------------------------------------------------------

DOMODEDOVO = {
    "icao": "UUDD",
    "aircraft": [
        ("VQ-BDU", "B737", "84R"),
        ("VP-BWW", "A320", "78"),
        ("RA-89012", "SSJ-100", "G12A"),
        ("VQ-BTS", "A321", "62A"),
    ],
    "engineers": [
        ("Беляев С. Н.", "B1.1", ["A320", "A321", "B737"], VALID_UNTIL,
         SHIFT_DAY, STATUS_FREE, ("tech", "ТЦ-Ремзона")),
        ("Гончаров П. И.", "B1.1", ["B737", "A320"], VALID_UNTIL,
         SHIFT_DAY, STATUS_FREE, ("tech", "ТЦ-Ремзона")),
        ("Соловьёв И. К.", "B2", ["A320", "B737", "B777"], VALID_UNTIL,
         SHIFT_DAY, STATUS_FREE, ("tech", "ТЦ-Ремзона")),
        ("Панов А. Е.", "A1", ["A320", "B737"], VALID_UNTIL,
         SHIFT_DAY, STATUS_FREE, ("stand", "G12A")),
        ("Жуков М. В.", "B1.1", ["B737", "B777"], VALID_UNTIL,
         SHIFT_DAY, STATUS_FREE, ("tech", "ТЦ-Север")),
        ("Титов Р. С.", "B1.1", ["A320", "SSJ-100"], VALID_UNTIL,
         SHIFT_NIGHT, STATUS_OFFLINE, ("tech", "ТЦ-Ремзона")),
    ],
    "vehicles": [
        ("ДМ-01", "Техпомощь", ("employee", "Беляев С. Н.")),
        # Свободная машина в 284 м от ТЦ-Ремзона: пеший Гончаров доходит
        # до неё и едет — 11.4 мин против 37.7 мин пешком до стоянки 84R.
        ("ДМ-02", "Техпомощь", ("node", "n4017304797")),
    ],
    "calls": [
        ("VQ-BDU", "hydraulic_leak"),
    ],
    "working_call": None,
}

DEMO_AIRPORTS = [SHEREMETYEVO, DOMODEDOVO]

# Учётные записи: логин, пароль, роль, ФИО, карточка сотрудника, аэропорты.
# Пароли простые намеренно: это конкурсный стенд, логины и пароли
# публикуются в README для проверки жюри.
USERS = [
    # Диспетчер привязан к обоим аэропортам — только так виден шаг выбора
    # аэропорта из §5.
    ("dispatcher", "dispatcher", ROLE_DISPATCHER, "Смирнова О. В.", None,
     ["UUEE", "UUDD"]),
    ("supervisor", "supervisor", ROLE_SHIFT_SUPERVISOR, "Белов А. Г.", None,
     ["UUEE"]),
    ("admin", "admin", ROLE_ADMIN, "Администратор системы", None,
     ["UUEE", "UUDD"]),
    ("sokolov", "engineer", ROLE_ENGINEER, "Соколов А. В.", "Соколов А. В.",
     ["UUEE"]),
    ("morozov", "engineer", ROLE_ENGINEER, "Морозов Д. С.", "Морозов Д. С.",
     ["UUEE"]),
    ("nikitin", "engineer", ROLE_ENGINEER, "Никитин П. А.", "Никитин П. А.",
     ["UUEE"]),
    ("belyaev", "engineer", ROLE_ENGINEER, "Беляев С. Н.", "Беляев С. Н.",
     ["UUDD"]),
]


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


def seed_employees(db, graph, icao, engineers):
    """Сотрудники ОТО с квалификациями и расстановкой по перрону."""
    created = {}
    for full_name, mark, types, valid_until, shift, status, place in engineers:
        node = node_coordinates(graph, place)
        employee = Employee(
            full_name=full_name,
            airport_icao=icao,
            shift=shift,
            status=status,
            lat=node["lat"],
            lon=node["lon"],
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
    for login, password, role, full_name, employee_name, airport_icaos in USERS:
        employee = employees.get(employee_name) if employee_name else None
        user = User(
            login=login,
            password_hash=hash_password(password),
            role=role,
            full_name=full_name,
            employee_id=employee.id if employee else None,
        )
        for icao in airport_icaos:
            airport = db.get(Airport, icao)
            # Аэропорта может не быть, если его граф не выгружен: учётная
            # запись всё равно создаётся, просто с меньшим доступом.
            if airport is not None:
                user.airports.append(airport)
        db.add(user)
    db.commit()


def seed_aircraft(db, graph, icao, items):
    """Борта на стоянках."""
    created = {}
    for board_number, aircraft_type, stand_ref in items:
        node = graph.find_stand(stand_ref)
        if node is None:
            continue
        aircraft = Aircraft(
            board_number=board_number,
            aircraft_type=aircraft_type,
            airport_icao=icao,
            stand_node_id=node["id"],
        )
        db.add(aircraft)
        created[board_number] = aircraft
    db.commit()
    return created


def seed_vehicles(db, graph, icao, items, employees):
    """Машины парка: одни у сотрудников, другие свободны на перроне."""
    created = 0
    for call_sign, kind, (where, ref) in items:
        driver = employees.get(ref) if where == "employee" else None
        if where == "employee":
            if driver is None:
                continue
            lat, lon = driver.lat, driver.lon
        else:
            node = graph.find_stand(ref) if where == "stand" else graph.node(ref)
            if node is None:
                continue
            lat, lon = node["lat"], node["lon"]

        db.add(
            Vehicle(
                airport_icao=icao,
                call_sign=call_sign,
                kind=kind,
                lat=lat,
                lon=lon,
                status=VEHICLE_IN_USE if driver else VEHICLE_FREE,
                employee=driver,
            )
        )
        created += 1

    db.commit()
    return created


def seed_calls(db, icao, aircraft, employees, calls, working_call):
    """Открытые вызовы, чтобы рабочий экран не был пустым при первом входе."""
    from algorithm.qualification import required_mark

    for board_number, defect_code in calls:
        board = aircraft.get(board_number)
        if board is None:
            continue
        db.add(
            Call(
                airport_icao=icao,
                aircraft_id=board.id,
                stand_node_id=board.stand_node_id,
                defect_code=defect_code,
                required_mark=str(required_mark(defect_code, board.aircraft_type)),
            )
        )

    if working_call is not None:
        seed_working_call(db, icao, aircraft, employees, working_call)
    db.commit()


def seed_working_call(db, icao, aircraft, employees, working_call):
    """
    Вызов, на котором уже работает занятый сотрудник.

    Время освобождения сотрудника выводится из вызова (прибыл N минут
    назад плюс норматив работ), а не задаётся отдельно: два независимых
    числа рано или поздно разошлись бы.
    """
    from algorithm.qualification import required_mark

    board_number, defect_code, full_name, arrived_min_ago = working_call
    board = aircraft.get(board_number)
    employee = employees.get(full_name)
    if board is None or employee is None:
        return

    arrived_at = utc_now() - timedelta(minutes=arrived_min_ago)
    db.add(
        Call(
            airport_icao=icao,
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


def seed_airport_data(db, airport):
    """Сотрудники, борта, машины и вызовы одного аэропорта."""
    icao = airport["icao"]
    graph = get_graph(icao)

    employees = seed_employees(db, graph, icao, airport["engineers"])
    aircraft = seed_aircraft(db, graph, icao, airport["aircraft"])
    vehicles = seed_vehicles(db, graph, icao, airport["vehicles"], employees)
    seed_calls(db, icao, aircraft, employees, airport["calls"], airport["working_call"])

    return employees, len(aircraft), vehicles


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
        available = available_icaos()

        employees = {}
        aircraft_count = 0
        vehicle_count = 0
        seeded = []

        for airport in DEMO_AIRPORTS:
            if airport["icao"] not in available:
                print(
                    f"Граф {airport['icao']} не найден, аэропорт пропущен. "
                    f"Выгрузка: python data/fetch_airport.py {airport['icao']}"
                )
                continue
            created, aircraft, vehicles = seed_airport_data(db, airport)
            employees.update(created)
            aircraft_count += aircraft
            vehicle_count += vehicles
            seeded.append(airport["icao"])

        if not seeded:
            print("Демонстрационные данные не созданы: нет ни одного графа аэропорта")
            return False

        # Учётные записи создаются после сотрудников: инженер привязывается
        # к своей карточке по идентификатору.
        seed_users(db, employees)

        if verbose:
            print(
                f"База наполнена ({', '.join(seeded)}): {len(employees)} сотрудников, "
                f"{aircraft_count} бортов, {vehicle_count} машин, "
                f"{len(USERS)} учётных записей"
            )
        return True
    finally:
        db.close()


if __name__ == "__main__":
    seed_if_empty()
