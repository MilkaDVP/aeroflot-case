"""
Сборка графа аэропорта из точек, расставленных администратором.

Редактор присылает вершины с координатами и связи между ними. Всё
остальное — длины рёбер, проекция в метры, опорная точка — считает сервер:
эти величины должны получаться ровно так же, как при выгрузке из OSM,
иначе маршруты в нарисованном аэропорту оказались бы в других единицах,
чем в настоящем.

Проверки здесь же. Граф из несвязанных точек внешне выглядит нормально,
но Дейкстра на нём молча вернёт «пути нет», и на демонстрации это будет
похоже на отказ алгоритма — поэтому связность проверяется при сохранении,
а не обнаруживается потом на вызове.
"""

from datetime import datetime, timezone

from services.osm_import import (
    compute_ref_point,
    haversine_m,
    largest_connected_component,
    project_edges,
    project_nodes,
)

NODE_TYPES = ("stand", "tech_center", "junction")

ERROR_EMPTY = "В графе нет ни одной точки"
ERROR_NO_EDGES = "Точки не соединены: без связей маршрут построить нельзя"
ERROR_BAD_TYPE = "Недопустимый тип точки {type}. Допустимые: stand, tech_center, junction"
ERROR_DUPLICATE_ID = "Точка {id} описана дважды"
ERROR_UNKNOWN_NODE = "Связь ссылается на несуществующую точку {id}"
ERROR_SELF_EDGE = "Связь соединяет точку {id} саму с собой"
ERROR_DISCONNECTED = (
    "Не все точки соединены между собой: {lost} из {total} остались в стороне. "
    "Соедините их, иначе маршрут к ним построить нельзя"
)
ERROR_NO_STAND = "Нужна хотя бы одна стоянка: без неё вызов регистрировать некуда"
ERROR_NO_TECH = (
    "Нужен хотя бы один техцентр: сотрудникам смены нужно откуда-то выезжать"
)


class GraphError(ValueError):
    """Граф не годится для маршрутизации. Текст показывается администратору."""


def check_nodes(nodes):
    """Проверяет вершины: типы, повторы и наличие обязательных."""
    if not nodes:
        raise GraphError(ERROR_EMPTY)

    seen = set()
    for node in nodes:
        if node.type not in NODE_TYPES:
            raise GraphError(ERROR_BAD_TYPE.format(type=node.type))
        if node.id in seen:
            raise GraphError(ERROR_DUPLICATE_ID.format(id=node.id))
        seen.add(node.id)

    if not [node for node in nodes if node.type == "stand"]:
        raise GraphError(ERROR_NO_STAND)
    if not [node for node in nodes if node.type == "tech_center"]:
        raise GraphError(ERROR_NO_TECH)

    return seen


def check_edges(edges, node_ids):
    """Проверяет связи: обе точки существуют, петель нет."""
    if not edges:
        raise GraphError(ERROR_NO_EDGES)

    for edge in edges:
        if edge.from_id == edge.to_id:
            raise GraphError(ERROR_SELF_EDGE.format(id=edge.from_id))
        for node_id in (edge.from_id, edge.to_id):
            if node_id not in node_ids:
                raise GraphError(ERROR_UNKNOWN_NODE.format(id=node_id))


def build_graph_payload(airport, nodes, edges):
    """
    Собирает документ графа из присланных точек и связей.

    Длины считаются по координатам: редактор их не присылает и не должен —
    иначе расстояние зависело бы от того, что нарисовал браузер.
    """
    node_ids = check_nodes(nodes)
    check_edges(edges, node_ids)

    prepared_nodes = [
        {
            "id": node.id,
            "type": node.type,
            "ref": node.ref,
            "lat": round(node.lat, 7),
            "lon": round(node.lon, 7),
            "x": None,
            "y": None,
        }
        for node in nodes
    ]
    by_id = {node["id"]: node for node in prepared_nodes}

    prepared_edges = []
    for edge in edges:
        source = by_id[edge.from_id]
        target = by_id[edge.to_id]
        prepared_edges.append(
            {
                "from_id": edge.from_id,
                "to_id": edge.to_id,
                "distance_m": round(
                    haversine_m(source["lat"], source["lon"], target["lat"], target["lon"]), 1
                ),
                "vehicle_allowed": edge.vehicle_allowed,
                # Нарисованная связь — прямая: промежуточной геометрии у неё
                # нет по определению.
                "points": [],
            }
        )

    check_connectivity(prepared_nodes, prepared_edges)

    payload = {
        "icao": airport.icao,
        "name": airport.name,
        "city": airport.city,
        "ref_point": None,
        "source": "Составлен вручную в интерфейсе администратора",
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "nodes": prepared_nodes,
        "edges": prepared_edges,
    }
    payload["ref_point"] = compute_ref_point(prepared_nodes)
    project_nodes(payload["nodes"], payload["ref_point"])
    project_edges(payload["edges"], payload["ref_point"])
    return payload


def check_connectivity(nodes, edges):
    """
    Все ли точки связаны между собой.

    Считаем наибольшую связную компоненту: если она меньше всего графа,
    часть точек недостижима, и вызов на такую стоянку окажется невыполним.
    """
    kept_nodes, _ = largest_connected_component(nodes, edges)
    if len(kept_nodes) < len(nodes):
        raise GraphError(
            ERROR_DISCONNECTED.format(lost=len(nodes) - len(kept_nodes), total=len(nodes))
        )


def empty_payload(icao, name, city, lat, lon):
    """
    Заготовка аэропорта без точек.

    Нужна, чтобы редактор знал, какой участок земли показать: без опорной
    точки карта открылась бы посреди океана.
    """
    return {
        "icao": icao,
        "name": name,
        "city": city,
        "ref_point": {"lat": round(lat, 7), "lon": round(lon, 7)},
        "source": "Составлен вручную в интерфейсе администратора",
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "nodes": [],
        "edges": [],
    }


def summarise(payload):
    """Сколько в графе точек каждого рода — для списка аэропортов."""
    nodes = payload.get("nodes", [])
    return {
        "nodes": len(nodes),
        "stands": len([node for node in nodes if node["type"] == "stand"]),
        "tech_centers": len([node for node in nodes if node["type"] == "tech_center"]),
        "edges": len(payload.get("edges", [])),
    }
