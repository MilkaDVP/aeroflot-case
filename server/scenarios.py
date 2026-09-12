"""
Контрольные сценарии подбора: отчёт для проверки по §10 задания.

Запуск (из каталога server):
    python scenarios.py            — вывод в консоль
    python scenarios.py --md ../docs/scenarios.md

Скрипт не требует запущенного сервера: он создаёт временную базу, наполняет
её тем же seed.py, что и боевой стенд, и прогоняет сценарии на настоящих
графах аэропортов. То есть проверяется ровно та конфигурация, которую
увидит жюри.

Что считается «интуитивным» выбором. Диспетчер, работающий по бумажным
данным, знает, кто сегодня на смене, и примерно представляет, кто где
находится. Он выбирает ближайшего по прямой — без проверки срока действия
отметки, без сверки типа ВС и не зная, где стоят свободные машины.
Именно это и моделируется: ближайший свободный сотрудник смены по
расстоянию по прямой, а его время в пути считается честно — по графу,
с его собственной машиной, если она при нём.
"""

import argparse
import os
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

# Адрес базы выставляется до импорта модулей сервера: database.py читает
# переменную окружения при импорте и позже уже не перечитывает.
_TEMP_DB = Path(tempfile.mkdtemp(prefix="aeroflot-scenarios-")) / "scenarios.db"
os.environ.setdefault("DATABASE_URL", f"sqlite:///{_TEMP_DB.as_posix()}")

from algorithm.dispatch import suggest  # noqa: E402
from algorithm.geo import haversine_m  # noqa: E402
from algorithm.qualification import find_qualification, required_mark  # noqa: E402
from algorithm.transport import plan_for  # noqa: E402
from clock import to_iso_utc, utc_now, utc_today  # noqa: E402
from constants import (  # noqa: E402
    REGULATION_ARRIVAL_LIMIT_MIN,
    STATUS_BUSY,
    STATUS_FREE,
    VEHICLE_FREE,
)
from database import SessionLocal, create_all  # noqa: E402
from graph_registry import get_graph  # noqa: E402
from models.aircraft import Aircraft  # noqa: E402
from models.employee import Employee  # noqa: E402
from models.vehicle import Vehicle  # noqa: E402
from seed import seed_if_empty  # noqa: E402

# Сценарии: аэропорт, борт, дефект, смена и что именно проверяется.
SCENARIOS = [
    {
        "title": "Разное расположение и допуск",
        "airport": "UUEE",
        "board": "VP-BZQ",
        "defect": "hydraulic_leak",
        "shift": "day",
        "checks": "ближайшие по прямой — без допуска на A320 и с истёкшей отметкой",
    },
    {
        "title": "Спецтранспорт: ближайший с машиной занят",
        "airport": "UUEE",
        "board": "VP-BES",
        "defect": "hydraulic_leak",
        "shift": "day",
        "busy": ["Соколов А. В."],
        "checks": "пеший кандидат доходит до свободной машины парка и едет на ней",
    },
    {
        "title": "Простой дефект, категория A",
        "airport": "UUEE",
        "board": "VP-BES",
        "defect": "cabin_light_fault",
        "shift": "day",
        "checks": "для работ уровня A годится и отметка B1 по своей области",
    },
    {
        "title": "Авионика: нужна отметка B2",
        "airport": "UUEE",
        "board": "VP-BKB",
        "defect": "nav_system_error",
        "shift": "day",
        "checks": "механики с B1 не подходят, хотя стоят ближе",
    },
    {
        "title": "Другой тип ВС: B777",
        "airport": "UUEE",
        "board": "VP-BGB",
        "defect": "hydraulic_leak",
        "shift": "day",
        "checks": "единственный с допуском на B777 идёт пешком — система "
        "показывает превышение, а не молчит",
    },
    {
        "title": "Внештатная: допуска нет ни у кого",
        "airport": "UUEE",
        "board": "VQ-BQX",
        "defect": "engine_start_fault",
        "shift": "day",
        "checks": "система не молчит, а сообщает об отсутствии кандидатов",
    },
    {
        "title": "Ночная смена",
        "airport": "UUEE",
        "board": "VP-BZQ",
        "defect": "hydraulic_leak",
        "shift": "night",
        "checks": "дневные сотрудники не рассматриваются, ночные не на смене",
    },
    {
        "title": "Домодедово: дальняя стоянка",
        "airport": "UUDD",
        "board": "VQ-BDU",
        "defect": "hydraulic_leak",
        "shift": "day",
        "checks": "пешком регламент недостижим, решают машины парка",
    },
    {
        "title": "Домодедово: простой дефект",
        "airport": "UUDD",
        "board": "VP-BWW",
        "defect": "cabin_light_fault",
        "shift": "day",
        "checks": "второй аэропорт со своим составом смены",
    },
]


def mark_busy(employees, names):
    """
    Помечает названных сотрудников занятыми — прямо в словарях сценария.

    Нужно, чтобы проверить ситуацию «ближайшие с машиной уже на вызове»,
    не портя общую тестовую базу: изменение живёт только внутри сценария.
    """
    if not names:
        return employees

    busy_until = to_iso_utc(utc_now() + timedelta(minutes=20))
    for employee in employees:
        if employee["full_name"] in names:
            employee["status"] = STATUS_BUSY
            employee["busy_until"] = busy_until
    return employees


def load_state(db, icao):
    """Сотрудники, борта и свободные машины одного аэропорта."""
    employees = [
        employee.as_algorithm_dict()
        for employee in db.query(Employee).filter(Employee.airport_icao == icao)
    ]
    aircraft = {
        item.board_number: item
        for item in db.query(Aircraft).filter(Aircraft.airport_icao == icao).all()
    }
    vehicles = [
        vehicle.as_algorithm_dict()
        for vehicle in db.query(Vehicle).filter(
            Vehicle.airport_icao == icao, Vehicle.status == VEHICLE_FREE
        )
    ]
    return employees, aircraft, vehicles


def nearest_by_straight_line(employees, shift, stand):
    """
    Ближайший по прямой свободный сотрудник смены.

    Считается без ключа-лямбды: §11 запрещает вложенные функции, поэтому
    минимум ищется обычным перебором.
    """
    nearest = None
    nearest_m = None

    for employee in employees:
        if employee["shift"] != shift or employee["status"] != STATUS_FREE:
            continue
        if employee["lat"] is None or employee["lon"] is None:
            continue
        distance = haversine_m(
            employee["lat"], employee["lon"], stand["lat"], stand["lon"]
        )
        if nearest_m is None or distance < nearest_m:
            nearest = employee
            nearest_m = distance

    return nearest, nearest_m


def intuitive_choice(graph, employees, shift, board, mark):
    """
    Кого выбрал бы диспетчер по бумажным данным: ближайшего по прямой.

    Допуск не проверяется — в этом и суть сравнения. Время в пути считается
    честно, по графу: если человек не годится, диспетчер узнает об этом,
    только когда тот уже приедет.
    """
    stand = graph.node(board.stand_node_id)
    nearest, straight_m = nearest_by_straight_line(employees, shift, stand)
    if nearest is None:
        return None

    # Свободные машины парка в расчёт не идут: об их местоположении
    # диспетчер с бумагами не знает.
    plan = plan_for(graph, nearest, board.stand_node_id, [])
    qualification, reason = find_qualification(
        nearest, mark, board.aircraft_type, utc_today()
    )

    return {
        "employee": nearest,
        "straight_m": straight_m,
        "minutes": plan.minutes if plan else None,
        "legal": qualification is not None,
        "reason": reason,
    }


def run_scenario(db, scenario):
    """Прогоняет один сценарий и возвращает строку отчёта."""
    graph = get_graph(scenario["airport"])
    employees, aircraft, vehicles = load_state(db, scenario["airport"])
    employees = mark_busy(employees, scenario.get("busy"))
    board = aircraft[scenario["board"]]

    mark = required_mark(scenario["defect"], board.aircraft_type)
    payload = {
        "aircraft_type": board.aircraft_type,
        "defect_code": scenario["defect"],
        "stand_node_id": board.stand_node_id,
    }

    result = suggest(graph, payload, employees, scenario["shift"], vehicles=vehicles)

    return {
        "scenario": scenario,
        "board": board,
        "mark": str(mark),
        "stand_ref": graph.node(board.stand_node_id)["ref"],
        "result": result,
        # Время расчёта берётся то же, что уходит в API: его измеряет suggest.
        "elapsed_ms": result.elapsed_ms,
        "intuitive": intuitive_choice(graph, employees, scenario["shift"], board, mark),
    }


def system_cell(row):
    """Колонка «решение системы»."""
    best = row["result"].best
    if best is None:
        return "— (нет кандидатов)"

    transport = "пешком"
    if best.route.vehicle and best.route.pickup:
        transport = f"пешком → {best.route.vehicle['call_sign']}"
    elif best.route.vehicle:
        transport = best.route.vehicle["call_sign"]

    verdict = "в регламенте" if best.within_regulation else "ПРЕВЫШЕНИЕ"
    return f"{best.employee['full_name']} · {best.minutes:.1f} мин · {transport} · {verdict}"


def intuitive_cell(row):
    """Колонка «интуитивный выбор»."""
    choice = row["intuitive"]
    if choice is None:
        return "— (некого выбрать)"

    parts = [
        f"{choice['employee']['full_name']} · {choice['straight_m']:.0f} м по прямой"
    ]
    if choice["minutes"] is not None:
        parts.append(f"{choice['minutes']:.1f} мин")
    if choice["legal"]:
        parts.append("допуск есть")
    else:
        parts.append(f"**НЕДОПУСТИМО**: {choice['reason']}")
    return " · ".join(parts)


def free_at(item):
    """Время освобождения занятого сотрудника — часы и минуты UTC."""
    value = item.get("busy_until")
    if not value:
        return "неизвестно"
    return datetime.fromisoformat(value).strftime("%H:%M UTC")


def explain(row):
    """
    Полный разбор одного сценария: кого и почему система отсеяла.

    Диспетчер отвечает за назначение лично, поэтому отчёт показывает
    не только выбранного, но и основания отказа по каждому — иначе
    решение системы нечем оспорить.
    """
    result = row["result"]
    lines = [
        "",
        f"## Разбор сценария 1: {row['scenario']['title']}",
        "",
        f"Требуемая отметка: **{row['mark']}**. Ответ системы: "
        f"«{result.message}»",
        "",
        "Отсеяны с указанием причины:",
        "",
    ]
    for item in result.rejected:
        lines.append(f"- {item['full_name']} — {item['reason']}")

    if result.busy:
        lines += ["", "Заняты, но допуск есть:", ""]
        for item in result.busy:
            lines.append(f"- {item['full_name']} — освободится {free_at(item)}")

    lines += [
        "",
        "Кандидаты с допуском, в порядке времени прибытия (первый и назначается):",
        "",
    ]
    for candidate in result.candidates:
        lines.append(
            f"- {candidate.employee['full_name']} — {candidate.minutes:.1f} мин"
        )
    return lines


def summarise(rows):
    """Итоговые числа: чем именно система лучше бумажного выбора."""
    with_candidates = [row for row in rows if row["result"].best is not None]
    illegal = [
        row
        for row in rows
        if row["intuitive"] is not None and not row["intuitive"]["legal"]
    ]
    saved = []
    for row in with_candidates:
        choice = row["intuitive"]
        if choice is None or choice["minutes"] is None or not choice["legal"]:
            continue
        saved.append(choice["minutes"] - row["result"].best.minutes)

    return {
        "total": len(rows),
        "with_candidates": len(with_candidates),
        "illegal": len(illegal),
        "in_regulation": sum(
            1 for row in with_candidates if row["result"].best.within_regulation
        ),
        "max_elapsed_ms": max(row["elapsed_ms"] for row in rows),
        "saved": saved,
    }


def render(rows):
    """Собирает отчёт в markdown."""
    summary = summarise(rows)
    lines = [
        "# Контрольные сценарии подбора",
        "",
        "Отчёт создаётся скриптом `server/scenarios.py` на временной базе,",
        "наполненной тем же `seed.py`, что и стенд, и на настоящих графах",
        "аэропортов из OpenStreetMap. Повторить: `python scenarios.py --md ../docs/scenarios.md`.",
        "",
        "**«Интуитивный» выбор** — как выбирает диспетчер по бумажным данным:",
        "ближайший по прямой свободный сотрудник смены, без проверки типа ВС",
        "и срока действия отметки и без знания, где стоят свободные машины.",
        "Время в пути для него считается честно, по графу.",
        "",
        f"Регламент прибытия: **{REGULATION_ARRIVAL_LIMIT_MIN} мин**.",
        "",
        "| № | Сценарий | Борт, стоянка | Требуется | Решение системы | Интуитивный выбор |",
        "|---|---|---|---|---|---|",
    ]

    for number, row in enumerate(rows, start=1):
        scenario = row["scenario"]
        lines.append(
            f"| {number} | {scenario['title']}<br><sub>{scenario['checks']}</sub> "
            f"| {row['board'].board_number} · {row['board'].aircraft_type} · ст. {row['stand_ref']} "
            f"<br><sub>{scenario['airport']}, смена {scenario['shift']}</sub> "
            f"| {row['mark']} | {system_cell(row)} | {intuitive_cell(row)} |"
        )

    lines += explain(rows[0])
    lines += [
        "",
        "## Что показывают сценарии",
        "",
        f"- Сценариев: **{summary['total']}**, из них с подходящим кандидатом: "
        f"{summary['with_candidates']}.",
        f"- Интуитивный выбор оказался **юридически недопустимым в "
        f"{summary['illegal']} сценариях из {summary['total']}**: у ближайшего "
        "по прямой сотрудника нет действующей отметки на этот тип ВС или вид работ.",
        f"- Система уложилась в регламент в {summary['in_regulation']} случаях "
        f"из {summary['with_candidates']} (в остальных честно сообщила о превышении).",
        f"- Максимальное время расчёта: **{summary['max_elapsed_ms']:.0f} мс** "
        "при ограничении задания 10 000 мс.",
    ]

    if summary["saved"]:
        best_gain = max(summary["saved"])
        lines.append(
            f"- Там, где интуитивный выбор был допустим, система экономила "
            f"до **{best_gain:.1f} мин** прибытия."
        )

    lines += [
        "",
        "## Практическая оптимизация",
        "",
        "Главный выигрыш — не минуты, а недопущенные назначения. Ближайший",
        "по прямой сотрудник регулярно оказывается тем, кого назначать нельзя:",
        "отметка не на этот тип ВС или истёк срок её действия. По бумажным",
        "данным это выясняется уже у борта — время потеряно, а вызов надо",
        "передавать заново. Система отсекает такие назначения до выезда",
        "и показывает причину отказа по каждому сотруднику.",
        "",
        "Второй выигрыш — спецтранспорт. Пеший сотрудник, которому до борта",
        "полчаса, укладывается в регламент, если по дороге забрать свободную",
        "машину парка. Вручную такой вариант не просчитывают: нужно знать,",
        "где стоят все свободные машины, и сравнить два маршрута.",
        "",
        "Третий — во внештатной ситуации система не молчит: показывает",
        "ближайшего с превышением регламента и занятых с допуском вместе",
        "со временем их освобождения, оставляя решение человеку.",
    ]
    return "\n".join(lines) + "\n"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--md", help="путь для отчёта в markdown")
    args = parser.parse_args()

    create_all()
    seed_if_empty(verbose=False)

    db = SessionLocal()
    try:
        rows = [run_scenario(db, scenario) for scenario in SCENARIOS]
    finally:
        db.close()

    report = render(rows)
    if args.md:
        path = Path(args.md)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(report, encoding="utf-8")
        print(f"Отчёт записан: {path}")
    print(report)
    return 0


if __name__ == "__main__":
    sys.exit(main())
