"""
Как добраться до борта: пешком, на своей машине или дойдя до свободной.

Способ передвижения больше не свойство инженера, а решение, которое
принимается под каждый вызов. Для инженера без машины сравниваются
варианты «весь путь пешком» и «дойти до свободной машины парка и доехать» —
по каждой свободной машине. Требование §10 — учёт наличия поблизости
спецтранспорта и его влияния на маршрут — ровно об этом.

Как считается, чтобы не перебирать машины вслепую. Граф неориентированный,
поэтому путь от машины до стоянки равен пути от стоянки до машины: один
проход Дейкстры в режиме транспорта от стоянки даёт время езды сразу от
всех машин, и он общий для всех кандидатов. Второй проход — пешком от
инженера — даёт время ходьбы сразу до всех машин и до самой стоянки.
Итого один проход на вызов и один на кандидата, вместо двух на каждую
пару «кандидат — машина». Полные маршруты восстанавливаются только
для выбранного варианта.
"""

from constants import MODE_PICKUP, MODE_VEHICLE, MODE_WALK
from algorithm.routing import (
    Route,
    route_from_point,
    shortest_path,
    shortest_paths_from,
    travel_time_minutes,
)


class Plan:
    """
    Выбранный способ добраться: один или два участка маршрута.

    Пешком или на своей машине — один участок. Через свободную машину —
    два: пешком до машины и на машине до борта. Каждый участок — обычный
    Route со своим способом передвижения и своей скоростью.
    """

    def __init__(self, legs, vehicle=None, pickup=False):
        self.legs = legs
        # Машина, на которой едет исполнитель, — словарь алгоритма или None.
        self.vehicle = vehicle
        # True — машину сначала нужно забрать; False — она уже при нём.
        self.pickup = pickup
        self.minutes = sum(leg.minutes for leg in legs)
        self.distance_m = sum(leg.distance_m for leg in legs)

    @property
    def mode(self):
        if self.pickup:
            return MODE_PICKUP
        return self.legs[0].mode

    @property
    def approach_m(self):
        return self.legs[0].approach_m

    @property
    def node_ids(self):
        """Вершины всего маршрута без повтора узла пересадки."""
        joined = []
        for leg in self.legs:
            for node_id in leg.node_ids:
                if not joined or joined[-1] != node_id:
                    joined.append(node_id)
        return joined

    @property
    def pickup_node_id(self):
        """Узел, где исполнитель забирает машину, — начало участка на машине."""
        return self.legs[1].node_ids[0] if self.pickup else None

    def as_dict(self):
        """Представление для API и карты: весь маршрут и его участки."""
        return {
            "node_ids": self.node_ids,
            "distance_m": round(self.distance_m, 1),
            "approach_m": round(self.approach_m, 1),
            "mode": self.mode,
            "minutes": round(self.minutes, 1),
            "legs": [leg.as_dict() for leg in self.legs],
            "vehicle": (
                {"id": self.vehicle["id"], "call_sign": self.vehicle["call_sign"]}
                if self.vehicle
                else None
            ),
            "pickup_node_id": self.pickup_node_id,
        }


def drive_distances_to(graph, goal_id):
    """
    Длины путей на транспорте от всех вершин до стоянки — один проход.

    Проход идёт от стоянки: граф неориентированный, и путь «машина → борт»
    равен пути «борт → машина».
    """
    return shortest_paths_from(graph, goal_id, MODE_VEHICLE)


def prepare_vehicles(graph, vehicles):
    """
    Посадка свободных машин на граф — один раз на весь подбор.

    Машина стоит в произвольной точке перрона; ей нужна ближайшая вершина,
    куда может заехать транспорт. Считать это для каждого кандидата заново
    незачем: машины от кандидатов не зависят.
    """
    prepared = []
    for vehicle in vehicles:
        node, offset_m = graph.nearest_node(vehicle["lat"], vehicle["lon"], MODE_VEHICLE)
        if node is not None:
            prepared.append({"vehicle": vehicle, "node_id": node["id"], "offset_m": offset_m})
    return prepared


def pickup_minutes(item, walk_m, drive_m, walk_speed):
    """
    Время варианта «дойти до машины и доехать».

    Смещение машины от вершины графа учитывается в обоих участках:
    инженер доходит до самой машины и выезжает от неё к рулёжке.
    """
    offset = item["offset_m"]
    vehicle_speed = item["vehicle"].get("speed_kmh")
    return travel_time_minutes(walk_m + offset, MODE_WALK, walk_speed) + travel_time_minutes(
        drive_m + offset, MODE_VEHICLE, vehicle_speed
    )


def best_plan(graph, employee, goal_id, prepared, drive_map):
    """
    Самый быстрый способ добраться для одного сотрудника.

    employee — словарь с lat, lon, speed_kmh и vehicle (своя машина или None).
    prepared и drive_map — общие для всего подбора, см. prepare_vehicles
    и drive_distances_to. Возвращает Plan или None, если пути нет вовсе.
    """
    own = employee.get("vehicle")
    if own is not None:
        # Машина уже при нём: едет от своих координат, пешего участка нет.
        route = route_from_point(
            graph, employee["lat"], employee["lon"], goal_id, MODE_VEHICLE, own.get("speed_kmh")
        )
        return Plan([route], vehicle=own) if route is not None else None

    walk_speed = employee.get("speed_kmh")
    entry, approach_m = graph.nearest_node(employee["lat"], employee["lon"], MODE_WALK)
    if entry is None:
        return None
    walk_map = shortest_paths_from(graph, entry["id"], MODE_WALK)

    best_minutes = None
    # None — «весь путь пешком»; иначе подготовленная машина.
    choice = None

    if goal_id in walk_map:
        best_minutes = travel_time_minutes(walk_map[goal_id] + approach_m, MODE_WALK, walk_speed)

    for item in prepared:
        node_id = item["node_id"]
        if drive_map is None or node_id not in walk_map or node_id not in drive_map:
            continue
        minutes = pickup_minutes(item, walk_map[node_id] + approach_m, drive_map[node_id], walk_speed)
        # Строго меньше: при равенстве честнее идти пешком, чем занимать
        # машину, которая может понадобиться другому.
        if best_minutes is None or minutes < best_minutes:
            best_minutes = minutes
            choice = item

    if best_minutes is None:
        return None
    if choice is None:
        return walk_plan(graph, entry["id"], goal_id, approach_m, walk_speed)
    return pickup_plan(graph, entry["id"], goal_id, approach_m, walk_speed, choice)


def walk_plan(graph, entry_id, goal_id, approach_m, walk_speed):
    """Весь путь пешком — один участок."""
    route = shortest_path(graph, entry_id, goal_id, MODE_WALK)
    if route is None:
        return None
    return Plan([Route(route.node_ids, route.distance_m, MODE_WALK, approach_m, walk_speed)])


def pickup_plan(graph, entry_id, goal_id, approach_m, walk_speed, item):
    """Пешком до свободной машины, дальше на ней — два участка."""
    walk = shortest_path(graph, entry_id, item["node_id"], MODE_WALK)
    drive = shortest_path(graph, item["node_id"], goal_id, MODE_VEHICLE)
    if walk is None or drive is None:
        return None

    offset = item["offset_m"]
    vehicle = item["vehicle"]
    legs = [
        Route(walk.node_ids, walk.distance_m, MODE_WALK, approach_m + offset, walk_speed),
        Route(drive.node_ids, drive.distance_m, MODE_VEHICLE, offset, vehicle.get("speed_kmh")),
    ]
    return Plan(legs, vehicle=vehicle, pickup=True)


def plan_for(graph, employee, goal_id, vehicles):
    """
    Способ добраться для одного сотрудника — при назначении.

    Тот же расчёт, что в подборе, чтобы время, которое увидел диспетчер,
    совпало с временем, записанным в вызов.
    """
    prepared = prepare_vehicles(graph, vehicles)
    drive_map = drive_distances_to(graph, goal_id) if prepared else None
    return best_plan(graph, employee, goal_id, prepared, drive_map)
