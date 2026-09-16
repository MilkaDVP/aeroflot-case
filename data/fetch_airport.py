"""
Офлайн-выгрузка графа аэропорта из OpenStreetMap через Overpass API.

Запускается один раз на аэропорт, результат коммитится в репозиторий
(data/airports/<ICAO>.json). Сервер при обычной работе в интернет не ходит:
жюри разворачивает проект без доступа к сети.

Разбор OSM живёт в `server/services/osm_import.py` — тем же кодом сервер
добавляет аэропорт по коду ИКАО из интерфейса администратора. Здесь
остаётся то, что нужно только офлайн-подготовке: каталог известных границ,
ручные правки поверх выгрузки, отчёт и запись файла.

Запуск:
    python fetch_airport.py UUEE
    python fetch_airport.py UUEE --no-patch      # без ручных правок
    python fetch_airport.py ULLI --auto          # границы найти в OSM по коду
    python fetch_airport.py --list               # список известных аэропортов
"""

import argparse
import json
import sys
from pathlib import Path

# Ядро выгрузки лежит на стороне сервера: скрипт и сервер обязаны строить
# граф одинаково, иначе аэропорт, добавленный в интерфейсе, отличался бы
# от закоммиченного.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "server"))

from services.osm_import import (  # noqa: E402
    build_airport,
    build_overpass_query,
    find_aerodrome,
    finalise,
    haversine_m,
    nearest_nodes,
    project_xy,
    request_overpass,
)

# Каталог аэропортов. bbox = (юг, запад, север, восток) в градусах.
# Границы подобраны по лётному полю, без привокзальной территории и парковок.
# Для аэропорта не из этого списка есть --auto: границы возьмутся из OSM.
AIRPORTS = {
    "UUEE": {
        "name": "Шереметьево",
        "city": "Москва",
        "bbox": (55.9420, 37.3200, 55.9990, 37.4700),
    },
    "UUDD": {
        "name": "Домодедово",
        "city": "Москва",
        # Границы взяты из отношения аэродрома в OSM с небольшим запасом:
        # рамка уже лётного поля обрезала бы рулёжки по краям.
        "bbox": (55.3840, 37.8550, 55.4330, 37.9620),
    },
    "ULLI": {
        "name": "Пулково",
        "city": "Санкт-Петербург",
        "bbox": (59.7830, 30.2100, 59.8130, 30.3400),
    },
}

OUTPUT_DIR = Path(__file__).parent / "airports"
RAW_DIR = OUTPUT_DIR / "raw"
PATCH_DIR = Path(__file__).parent / "patches"


def on_retry(message):
    """Сообщения о неудачных попытках Overpass — в поток ошибок."""
    print(f"  {message}", file=sys.stderr)


# --- Ручные правки ----------------------------------------------------------


def connect_node_to_graph(node, graph_nodes, options):
    """
    Соединяет добавленный вручную узел с ближайшими узлами графа.

    Иначе техцентр останется изолированной точкой, и Дейкстра не найдёт
    из него ни одного маршрута. Число связей задаётся в файле правок:
    одна связь — тупиковый съезд, две и больше — сквозной проезд.
    """
    count = options.get("count", 2)
    vehicle_allowed = options.get("vehicle_allowed", True)
    new_edges = []

    for neighbour in nearest_nodes(node, graph_nodes, count):
        distance = haversine_m(
            node["lat"], node["lon"], neighbour["lat"], neighbour["lon"]
        )
        new_edges.append(
            {
                "from_id": node["id"],
                "to_id": neighbour["id"],
                "distance_m": round(distance, 1),
                "vehicle_allowed": vehicle_allowed,
                "points": [],
            }
        )
    return new_edges


def apply_patch(airport, patch):
    """
    Накладывает ручные правки поверх выгрузки OSM.

    Зачем это нужно: в OSM нет техцентра ОТО, часть стоянок без номеров,
    а пешеходные проходы по перрону не размечены вовсе. Правки лежат
    отдельным файлом, чтобы всегда было видно, где реальные данные,
    а где наше дополнение.
    """
    by_id = {node["id"]: node for node in airport["nodes"]}
    ref_lat = airport["ref_point"]["lat"]
    ref_lon = airport["ref_point"]["lon"]
    # Узлы из OSM запоминаем до правок: новые узлы цепляем к настоящему
    # графу, а не друг к другу.
    osm_nodes = list(airport["nodes"])

    for node in patch.get("add_nodes", []):
        x, y = project_xy(node["lat"], node["lon"], ref_lat, ref_lon)
        prepared = {
            "id": node["id"],
            "type": node["type"],
            "ref": node.get("ref"),
            "lat": node["lat"],
            "lon": node["lon"],
            "x": x,
            "y": y,
        }
        by_id[node["id"]] = prepared
        airport["nodes"].append(prepared)

        if "connect_nearest" in node:
            airport["edges"].extend(
                connect_node_to_graph(prepared, osm_nodes, node["connect_nearest"])
            )

    for change in patch.get("set_nodes", []):
        target = by_id.get(change["id"])
        if target is None:
            print(f"  ! правка узла {change['id']}: узла нет в выгрузке", file=sys.stderr)
            continue
        target.update({key: value for key, value in change.items() if key != "id"})

    for edge in patch.get("add_edges", []):
        source = by_id.get(edge["from_id"])
        target = by_id.get(edge["to_id"])
        if source is None or target is None:
            print(
                f"  ! ребро {edge['from_id']}—{edge['to_id']}: узла нет в выгрузке",
                file=sys.stderr,
            )
            continue
        distance = edge.get("distance_m")
        if distance is None:
            distance = round(
                haversine_m(source["lat"], source["lon"], target["lat"], target["lon"]), 1
            )
        airport["edges"].append(
            {
                "from_id": edge["from_id"],
                "to_id": edge["to_id"],
                "distance_m": distance,
                "vehicle_allowed": edge.get("vehicle_allowed", True),
                # Ручные связки рисуются прямой: промежуточной геометрии
                # у них нет по определению.
                "points": [],
            }
        )

    return airport


def load_patch(icao):
    """Читает файл ручных правок, если он есть."""
    path = PATCH_DIR / f"{icao}.patch.json"
    if not path.exists():
        return None
    with path.open(encoding="utf-8") as handle:
        return json.load(handle)


# --- Отчёт и запись ---------------------------------------------------------


def report(airport, raw_counts, connected_counts):
    """Печатает сводку — по ней видно, годится ли граф для маршрутизации."""
    stands = [node for node in airport["nodes"] if node["type"] == "stand"]
    tech = [node for node in airport["nodes"] if node["type"] == "tech_center"]
    dropped = raw_counts[0] - connected_counts[0]

    print(f"  из OSM:          {raw_counts[0]} узлов, {raw_counts[1]} рёбер")
    print(f"  после связности: {connected_counts[0]} узлов, {connected_counts[1]} рёбер"
          f" (отброшено обрывков: {dropped})")
    print(f"  после правок:    {len(airport['nodes'])} узлов, {len(airport['edges'])} рёбер")
    print(f"  стоянок:         {len(stands)}")
    print(f"  техцентров:      {len(tech)}")
    if not tech:
        print("  ! техцентр не задан — добавьте его в файл правок", file=sys.stderr)


def save_json(path, payload):
    """Пишет JSON с отступами: файл лежит в git, диффы должны читаться."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
        handle.write("\n")


def resolve_meta(icao, auto):
    """
    Границы и название аэропорта: из каталога либо из OSM по коду.

    Каталог главнее: рамки в нём подобраны вручную и проверены. --auto
    нужен для аэропорта, которого в каталоге ещё нет.
    """
    if icao in AIRPORTS and not auto:
        return AIRPORTS[icao]

    print(f"Ищу границы {icao} в OpenStreetMap…")
    aerodrome = find_aerodrome(icao, on_retry)
    if aerodrome is None:
        return None

    print(f"  найден: {aerodrome['name']}, рамка {aerodrome['bbox']}")
    return aerodrome


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("icao", nargs="?", help="код ИКАО аэропорта, например UUEE")
    parser.add_argument("--list", action="store_true", help="показать известные аэропорты")
    parser.add_argument("--no-patch", action="store_true", help="не накладывать ручные правки")
    parser.add_argument(
        "--auto", action="store_true", help="границы найти в OSM по коду ИКАО"
    )
    parser.add_argument(
        "--service-roads", action="store_true", help="добавить служебные проезды (highway=service)"
    )
    parser.add_argument("--raw", help="взять сырой ответ OSM из файла вместо запроса к Overpass")
    args = parser.parse_args()

    if args.list:
        for icao, meta in AIRPORTS.items():
            print(f"{icao}  {meta['name']} ({meta['city']})")
        return 0

    if not args.icao:
        parser.error("укажите код ИКАО или --list")

    icao = args.icao.upper()
    meta = resolve_meta(icao, args.auto)
    if meta is None:
        print(
            f"Аэропорт {icao} не описан в AIRPORTS и не найден в OpenStreetMap",
            file=sys.stderr,
        )
        return 1

    if args.raw:
        print(f"Читаю сырой ответ из {args.raw}")
        with open(args.raw, encoding="utf-8") as handle:
            osm_data = json.load(handle)
    else:
        print(f"Запрашиваю {icao} у Overpass API…")
        query = build_overpass_query(meta["bbox"], args.service_roads)
        osm_data = request_overpass(query, on_retry)
        save_json(RAW_DIR / f"{icao}.osm.json", osm_data)
        print(f"  сырой ответ сохранён: {RAW_DIR / (icao + '.osm.json')}")

    airport = build_airport(icao, osm_data, meta)
    raw_counts = (len(airport["nodes"]), len(airport["edges"]))

    airport = finalise(airport)
    connected_counts = (len(airport["nodes"]), len(airport["edges"]))

    if not args.no_patch:
        patch = load_patch(icao)
        if patch:
            print(f"  накладываю правки: {PATCH_DIR / (icao + '.patch.json')}")
            airport = apply_patch(airport, patch)
        else:
            print("  файла правок нет, пропускаю")

    report(airport, raw_counts, connected_counts)
    path = OUTPUT_DIR / f"{icao}.json"
    save_json(path, airport)
    print(f"Готово: {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
