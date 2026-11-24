"""
Как добраться до борта: пешком, на машине, через склад — с пересадками.

Способ передвижения — не свойство инженера, а решение под каждый вызов.
Сравниваются все разумные варианты, побеждает самый быстрый:

- весь путь пешком;
- своя машина (инженер уже в ней);
- пешком до свободной машины парка, пересадка, дальше на ней.

Диспетчер может задать промежуточные точки — по порядку: склад (30 с
на инструмент), стоянку спецтехники (пересадка на машину), любую другую
точку (маршрут проходит через неё). Если вызову нужен инструмент, а
склад не выбран — система сама выберет самый быстрый склад. Варианты
становятся многоточечными, например:

- пешком на склад → пешком к борту;
- пешком на склад → пешком к машине → на машине к борту
  (две промежуточные точки: склад и стоянка спецтехники);
- пешком к машине → на машине к складу → на машине к борту;
- своя машина → склад → борт.

Регламентные задержки входят во время: посадка в машину и высадка — по
10 с, получение инструмента на складе — 30 с. Скоростной режим участков
учитывает поиск пути (routing.py).

Как считается, чтобы не перебирать варианты вслепую. Граф
неориентированный, поэтому время от любой вершины до точки равно времени
от точки до неё: один проход Дейкстры от стоянки даёт время езды сразу
от всех машин, один проход от склада — время до склада отовсюду. Эти
проходы общие для всех кандидатов (Network кэширует их), а на кандидата
приходится один проход пешком от его координат. Полные маршруты
восстанавливаются только для выбранного варианта.
"""

from algorithm.routing import Route, agent_speed, route_from_point, shortest_path, shortest_paths_from
from constants import (
    MODE_PICKUP,
    MODE_VEHICLE,
    MODE_WALK,
    TOOL_PICKUP_S,
    VEHICLE_BOARDING_S,
)

STOP_WAREHOUSE = "warehouse"
STOP_BOARD = "board"
STOP_ALIGHT = "alight"
# Промежуточная точка, через которую маршрут просто проходит.
STOP_PASS = "pass"
# Стоянка спецтехники: здесь пересадка на машину.
STOP_PARKING = "parking"

# Сначала отвести прежнюю машину на её стоянку.
STOP_RETURN = "return"

STOP_TITLES = {
    STOP_WAREHOUSE: "получить инструмент",
    STOP_BOARD: "посадка в спецтранспорт",
    STOP_ALIGHT: "высадка",
    STOP_PASS: "через точку",
    STOP_RETURN: "вернуть машину",
}


class Plan:
    """
    Выбранный способ добраться: участки маршрута и остановки между ними.

    Каждый участок — Route со своим способом передвижения и скоростью.
    Остановки (stops) — склад, посадка, высадка — с регламентной задержкой.
    """

    def __init__(self, legs, vehicle=None, pickup=False, stops=None):
        self.legs = legs
        # Машина, на которой едет исполнитель, — словарь алгоритма или None.
        self.vehicle = vehicle
        # True — машину сначала нужно забрать; False — она уже при нём.
        self.pickup = pickup
        self.stops = stops or []
        self.dwell_s = sum(stop["dwell_s"] for stop in self.stops)
        self.minutes = sum(leg.minutes for leg in legs) + self.dwell_s / 60
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
        """Вершины всего маршрута без повтора узлов пересадки."""
        joined = []
        for leg in self.legs:
            for node_id in leg.node_ids:
                if not joined or joined[-1] != node_id:
                    joined.append(node_id)
        return joined

    @property
    def pickup_node_id(self):
        """Узел, где исполнитель садится в машину парка, — начало езды."""
        if not self.pickup:
            return None
        # Езда на прежней машине до её стоянки — ещё не та посадка.
        legs = self.legs[1:] if self.stops and self.stops[0]["kind"] == STOP_RETURN else self.legs
        first_drive = next((leg for leg in legs if leg.mode == MODE_VEHICLE), None)
        return first_drive.node_ids[0] if first_drive else None

    @property
    def warehouse_node_id(self):
        stop = next((s for s in self.stops if s["kind"] == STOP_WAREHOUSE), None)
        return stop["node_id"] if stop else None

    @property
    def waypoints(self):
        """Промежуточные точки маршрута — без высадки у самого борта."""
        return [stop for stop in self.stops if not stop.get("final")]

    def as_dict(self):
        """Представление для API и карты: весь маршрут, участки и остановки."""
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
            "stops": self.stops,
            "dwell_s": self.dwell_s,
        }


def place_name(node):
    """Как назвать точку человеку: «Склад №1», «стоянка 4»; перекрёсток — без имени."""
    if not node or node.get("type") == "junction":
        return None
    if node.get("type") == "stand":
        return f"стоянка {node.get('ref')}"
    return node.get("ref")


def stop(graph, kind, node_id, dwell_s, final=False):
    node = graph.node(node_id) or {}
    return {
        "kind": kind,
        "node_id": node_id,
        "name": place_name(node),
        "title": STOP_TITLES[kind],
        "dwell_s": dwell_s,
        "final": final,
    }


# --- Общие для подбора расчёты ------------------------------------------------


def prepare_vehicles(graph, vehicles):
    """
    Посадка свободных машин на граф — один раз на весь подбор.

    Машина стоит в произвольной точке; ей нужна ближайшая вершина, куда
    может заехать транспорт. Считать это для каждого кандидата заново
    незачем: машины от кандидатов не зависят.
    """
    prepared = []
    for vehicle in vehicles:
        node, offset_m = graph.nearest_node(vehicle["lat"], vehicle["lon"], MODE_VEHICLE)
        if node is not None:
            prepared.append({"vehicle": vehicle, "node_id": node["id"], "offset_m": offset_m})
    return prepared


def warehouses(graph):
    """Склады аэропорта — места получения инструмента."""
    return [node["id"] for node in graph.nodes_of_type("warehouse")]


def waypoint_kind(graph, node_id):
    """Что делать в промежуточной точке — по виду объекта."""
    node_type = (graph.node(node_id) or {}).get("type")
    if node_type == "warehouse":
        return STOP_WAREHOUSE
    if node_type == "vehicle_parking":
        return STOP_PARKING
    return STOP_PASS


def normalize_waypoints(graph, waypoints):
    """Список узлов промежуточных точек — только существующие вершины."""
    result = []
    for item in waypoints or []:
        node_id = item["node_id"] if isinstance(item, dict) else item
        if graph.node(node_id) is not None:
            result.append(node_id)
    return result


class Network:
    """
    Всё, что общее для кандидатов одного вызова: цель, машины, точки
    маршрута и кэш проходов Дейкстры по (точка, способ, скорость).

    waypoints — промежуточные точки, выбранные диспетчером, по порядку.
    depots — склады, один из которых обязан войти в маршрут (если склада
    среди точек нет): система сама выберет, какой быстрее.
    """

    def __init__(self, graph, goal_id, vehicles=None, depots=None, waypoints=None):
        self.graph = graph
        self.goal = goal_id
        self.prepared = prepare_vehicles(graph, vehicles or [])
        self.waypoints = normalize_waypoints(graph, waypoints)
        has_warehouse = any(waypoint_kind(graph, n) == STOP_WAREHOUSE for n in self.waypoints)
        self.depots = None if has_warehouse else (depots or None)
        self._maps = {}

    def times(self, start_id, mode, speed_kmh=None):
        key = (start_id, mode, agent_speed(mode, speed_kmh))
        if key not in self._maps:
            self._maps[key] = shortest_paths_from(self.graph, start_id, mode, speed_kmh)
        return self._maps[key]

    def chains(self):
        """Варианты цепочки точек: выбранные диспетчером, и склад — если нужен."""
        if not self.depots:
            return [self.waypoints]
        # Склад — в любом месте цепочки: заезжать на него первым, а потом
        # возвращаться той же дорогой, нужно не всегда.
        return [self.waypoints[:i] + [depot] + self.waypoints[i:]
                for depot in self.depots for i in range(len(self.waypoints) + 1)]


def seconds(distance_m, mode, speed_kmh=None):
    return distance_m / (agent_speed(mode, speed_kmh) / 3.6)


# --- Поиск варианта ------------------------------------------------------------

FOOT = "foot"
PARKING_WALK_LIMIT_S = 300


def best_chain(net, employee, chain, allow_fleet=True, fleet_only=False):
    """
    Самый быстрый проход по цепочке точек: (секунды, рецепт) или None.

    Пошаговый перебор по участкам «точка → следующая точка». Состояние
    после участка — пешком или в машине (в какой). На каждом участке
    пешеход может идти пешком или дойти до свободной машины, сесть
    и доехать; в машине — едет дальше. В точке-складе — 30 с (в машине:
    высадка, склад, посадка), на стоянке спецтехники — пересадка на машину,
    через остальные точки маршрут просто проходит. У борта — высадка.
    """
    graph, goal = net.graph, net.goal
    points = chain + [goal]
    own = employee.get("vehicle")
    walk_speed = employee.get("speed_kmh")

    # Состояние: ключ (FOOT или идентификатор машины) → (секунды, шаги, машина, пересадка?).
    if own is not None:
        speed = own.get("speed_kmh")
        entry, offset = graph.nearest_node(employee["lat"], employee["lon"], MODE_VEHICLE)
        if entry is None:
            return None
        states = {("own",): (seconds(offset, MODE_VEHICLE, speed), [], own, False, entry["id"], offset)}
    else:
        entry, approach = graph.nearest_node(employee["lat"], employee["lon"], MODE_WALK)
        if entry is None:
            return None
        states = {FOOT: (seconds(approach, MODE_WALK, walk_speed), [], None, False, entry["id"], approach)}

    for index, target in enumerate(points):
        final = index == len(points) - 1
        moved = {}

        def keep(key, value):
            if value is not None and (key not in moved or value[0] < moved[key][0]):
                moved[key] = value

        for key, (cost, steps, vehicle, pickup, origin, extra) in states.items():
            if key == FOOT:
                walk = net.times(origin, MODE_WALK, walk_speed)
                if target in walk:
                    keep(FOOT, (cost + walk[target], steps + [("walk", origin, target, extra, walk_speed)],
                                None, False, target, 0.0))
                if allow_fleet:
                    for item in net.prepared:
                        node, fleet = item["node_id"], item["vehicle"]
                        speed = fleet.get("speed_kmh")
                        drive = net.times(target, MODE_VEHICLE, speed)
                        if node not in walk or node not in drive:
                            continue
                        offset = item["offset_m"]
                        keep(fleet["id"], (
                            cost + walk[node] + seconds(offset, MODE_WALK, walk_speed) + VEHICLE_BOARDING_S
                            + seconds(offset, MODE_VEHICLE, speed) + drive[node],
                            steps + [
                                ("walk", origin, node, extra + offset, walk_speed),
                                ("stop", STOP_BOARD, node, VEHICLE_BOARDING_S, False),
                                ("drive", node, target, offset, speed),
                            ],
                            fleet, True, target, 0.0,
                        ))
            else:
                speed = vehicle.get("speed_kmh")
                drive = net.times(origin, MODE_VEHICLE, speed)
                if target in drive:
                    keep(key, (cost + drive[target], steps + [("drive", origin, target, extra, speed)],
                               vehicle, pickup, target, 0.0))
        if not moved:
            return None

        # Что делать в точке.
        states = {}
        for key, (cost, steps, vehicle, pickup, origin, extra) in moved.items():
            if final:
                if key != FOOT:
                    cost += VEHICLE_BOARDING_S
                    steps = steps + [("stop", STOP_ALIGHT, target, VEHICLE_BOARDING_S, True)]
                states[key] = (cost, steps, vehicle, pickup, origin, extra)
                continue
            kind = waypoint_kind(graph, target)
            if kind == STOP_WAREHOUSE:
                if key == FOOT:
                    cost += TOOL_PICKUP_S
                    steps = steps + [("stop", STOP_WAREHOUSE, target, TOOL_PICKUP_S, False)]
                else:
                    cost += 2 * VEHICLE_BOARDING_S + TOOL_PICKUP_S
                    steps = steps + [
                        ("stop", STOP_ALIGHT, target, VEHICLE_BOARDING_S, False),
                        ("stop", STOP_WAREHOUSE, target, TOOL_PICKUP_S, False),
                        ("stop", STOP_BOARD, target, VEHICLE_BOARDING_S, False),
                    ]
            elif kind == STOP_PASS:
                steps = steps + [("stop", STOP_PASS, target, 0, False)]
            elif kind == STOP_PARKING and key == FOOT and allow_fleet:
                # Стоянка спецтехники — пересадка: здесь берётся машина,
                # стоящая ближе всех к этой точке.
                if net.prepared:
                    walk = net.times(target, MODE_WALK, walk_speed)
                    # Машина должна стоять здесь же, на стоянке, а не где-то
                    # на другом конце поля: до 5 минут пешком.
                    reachable = [item for item in net.prepared
                                 if item["node_id"] in walk and walk[item["node_id"]] <= PARKING_WALK_LIMIT_S]
                    if reachable:
                        item = min(reachable, key=lambda it: walk[it["node_id"]] + it["offset_m"])
                        fleet = item["vehicle"]
                        offset = item["offset_m"]
                        cost += walk[item["node_id"]] + seconds(offset, MODE_WALK, walk_speed) + VEHICLE_BOARDING_S
                        steps = steps + [
                            ("walk", target, item["node_id"], offset, walk_speed),
                            ("stop", STOP_BOARD, item["node_id"], VEHICLE_BOARDING_S, False),
                        ]
                        candidate = (cost, steps, fleet, True, item["node_id"], offset)
                        if fleet["id"] not in states or candidate[0] < states[fleet["id"]][0]:
                            states[fleet["id"]] = candidate
                        continue
            elif kind == STOP_PARKING:
                steps = steps + [("stop", STOP_PASS, target, 0, False)]
            if key not in states or cost < states[key][0]:
                states[key] = (cost, steps, vehicle, pickup, origin, extra)

    options = list(states.items())
    if fleet_only:
        options = [item for item in options if item[1][3]]
    if not options:
        return None
    # При равенстве честнее идти пешком, чем занимать машину.
    _, (cost, steps, vehicle, pickup, _, _) = min(
        options, key=lambda item: (item[1][0], item[0] != FOOT)
    )
    return cost, {"vehicle": vehicle, "pickup": pickup, "steps": steps}


def build(net, recipe):
    """Восстанавливает полные участки маршрута по рецепту варианта."""
    graph = net.graph
    legs = []
    stops = []
    # Где маршрут уже проходил: из равных по времени путей берётся тот,
    # что меньше повторяется.
    passed = set()
    for step in recipe["steps"]:
        if step[0] == "stop":
            _, kind, node_id, dwell, final = step
            stops.append(stop(graph, kind, node_id, dwell, final))
            continue
        kind, origin, destination, approach, speed = step
        mode = MODE_WALK if kind == "walk" else MODE_VEHICLE
        route = shortest_path(graph, origin, destination, mode, speed, passed)
        if route is None:
            return None
        passed.update(route.node_ids)
        legs.append(Route(route.node_ids, route.distance_m, mode, approach, speed, route.path_seconds))
    return Plan(legs, vehicle=recipe["vehicle"], pickup=recipe["pickup"], stops=stops)


def search(net, employee, allow_fleet=True, fleet_only=False):
    best = None
    for chain in net.chains():
        found = best_chain(net, employee, chain, allow_fleet, fleet_only)
        if found and (best is None or found[0] < best[0]):
            best = found
    return best


def best_plan(net, employee):
    """Самый быстрый способ добраться для одного сотрудника."""
    found = search(net, employee)
    return build(net, found[1]) if found else None


# --- Точки входа -------------------------------------------------------------

def plan_for(graph, employee, goal_id, vehicles, depots=None, waypoints=None):
    """
    Способ добраться для одного сотрудника — при назначении.

    Тот же расчёт, что в подборе, чтобы время, которое увидел диспетчер,
    совпало с временем, записанным в вызов.
    """
    return best_plan(Network(graph, goal_id, vehicles, depots, waypoints), employee)


def plan_walk(graph, employee, goal_id, depots=None, waypoints=None):
    """Весь путь пешком, даже если рядом есть машина: так решил диспетчер."""
    walker = {**employee, "vehicle": None}
    net = Network(graph, goal_id, [], depots, waypoints)
    found = search(net, walker, allow_fleet=False)
    return build(net, found[1]) if found else None


def plan_after_return(graph, employee, vehicle, home, build_plan):
    """
    Сначала вернуть машину, на которой инженер едет, потом новый путь.

    build_plan(person) строит путь от стоянки возврата. Машина уже
    на месте — участка возврата нет.
    """
    lat, lon = home
    node, offset = graph.nearest_node(lat, lon, MODE_VEHICLE)
    if node is None:
        return None
    base = build_plan({**employee, "lat": lat, "lon": lon, "vehicle": None})
    drive = route_from_point(graph, employee["lat"], employee["lon"], node["id"], MODE_VEHICLE,
                             vehicle.get("speed_kmh"))
    if base is None or drive is None:
        return None
    back = stop(graph, STOP_RETURN, node["id"], VEHICLE_BOARDING_S)
    return Plan([drive, *base.legs], vehicle=base.vehicle, pickup=base.pickup, stops=[back, *base.stops])


def plan_via(graph, employee, goal_id, vehicle, waypoints=None, depots=None):
    """
    Дойти до указанной машины и ехать на ней — вторым в машине ответственного.

    Машина может быть уже занята: в ней поедет ответственный инженер, и
    второй идёт к ней, а не к свободной. Поэтому расчёт по одной заданной
    машине, без сравнения с остальными.
    """
    walker = {**employee, "vehicle": None}
    net = Network(graph, goal_id, [vehicle], depots, waypoints)
    found = search(net, walker, fleet_only=True)
    return build(net, found[1]) if found else None
