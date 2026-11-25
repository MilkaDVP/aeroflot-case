"""
Имитатор приближения ВС: ведёт борт на инженера точками /api/v1/track.

    python simulate_approach.py AT-0002 RA-73110

Запускать из server/ на той же машине, где работает сервер: место инженера
берётся из его базы. В конце борт возвращается на прежнее место.
"""

import argparse
import json
import math
import time
import urllib.request
from datetime import timedelta

import models  # noqa: F401 — регистрирует все таблицы
from clock import utc_now
from database import SessionLocal
from graph_registry import get_graph
from models.aircraft import Aircraft
from models.employee import Employee
from services import proximity
from services.tracking import format_time

STEP_S = 1
BEYOND_M = 60


def send(url, board, lat, lon, spd, moment=None):
    body = {"obj": board, "lat": lat, "lon": lon, "acc": 3, "spd": spd, "t": format_time(moment or utc_now())}
    request = urllib.request.Request(url + "/api/v1/track", json.dumps(body).encode(), {"Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=10) as answer:
        return json.load(answer)


def jump(url, board, lat, lon):
    for _ in range(40):
        if send(url, board, lat, lon, 0).get("applied"):
            return True
        time.sleep(1)
    return False


def approach(graph, here, distance, speed):
    """Откуда ехать, чтобы прогноз вёл борт на инженера."""
    standing = proximity.walk([here], 0)
    for degrees in range(0, 360, 10):
        ux, uy = math.cos(math.radians(degrees)), math.sin(math.radians(degrees))
        start = (here[0] - ux * distance, here[1] - uy * distance)
        paths = proximity.aircraft_paths(graph, start, (ux, uy), speed * proximity.HORIZON_S)
        if any(proximity.conflict(proximity.walk(path, speed), standing) for path in paths):
            return start, (ux, uy)
    return None


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("engineer", nargs="?", default="AT-0002", help="начало имени инженера")
    parser.add_argument("board", nargs="?", default="RA-73110")
    parser.add_argument("--speed", type=float, default=5, help="скорость борта, м/с")
    parser.add_argument("--distance", type=float, default=200, help="откуда начать, м")
    parser.add_argument("--url", default="http://localhost:8000")
    args = parser.parse_args()

    with SessionLocal() as db:
        aircraft = db.query(Aircraft).filter_by(board_number=args.board).first()
        if aircraft is None:
            raise SystemExit(f"Нет борта {args.board}")
        person = db.query(Employee).filter(
            Employee.airport_icao == aircraft.airport_icao, Employee.archived.is_(False),
            Employee.full_name.like(f"{args.engineer}%")).first()
        if person is None or person.lat is None:
            raise SystemExit(f"Нет инженера {args.engineer} или его место неизвестно")
        if person.status == "offline":
            raise SystemExit(f"{person.full_name} не на смене — предупреждения не будет")
        graph = get_graph(aircraft.airport_icao)
        here, name = proximity.to_xy(graph, person.lat, person.lon), person.full_name
        home = (aircraft.lat, aircraft.lon)
        if home[0] is None:
            node = graph.node(aircraft.stand_node_id)
            home = (node["lat"], node["lon"])

    found = approach(graph, here, args.distance, args.speed)
    if found is None:
        raise SystemExit("К этому месту по прогнозу не подъехать — переставьте инженера ближе к рулёжке")
    start, (ux, uy) = found
    seconds = int((args.distance + BEYOND_M) / args.speed / STEP_S)
    print(f"{args.board} едет к {name}: {args.distance:.0f} м, {args.speed:g} м/с, {seconds} с")
    try:
        jump(args.url, args.board, *proximity.to_ll(graph, *start))
        time.sleep(STEP_S)
        for step in range(seconds + 1):
            passed = step * STEP_S * args.speed
            lat, lon = proximity.to_ll(graph, start[0] + ux * passed, start[1] + uy * passed)
            answer = send(args.url, args.board, lat, lon, args.speed)
            print(f"  до инженера {abs(args.distance - passed):4.0f} м", "" if answer.get("applied") else answer)
            time.sleep(STEP_S)
    finally:
        # Мгновенный скачок сервер отбрасывает: повторяем, пока не примет.
        print("Возвращаю борт на место…")
        back = jump(args.url, args.board, *home)
        print("Борт на прежнем месте" if back else "Вернуть не удалось — переставьте борт на карте")


if __name__ == "__main__":
    main()
