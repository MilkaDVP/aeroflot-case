"""
Как добраться до борта: пешком, на машине, через склад — с пересадками.

Способ передвижения — не свойство инженера, а решение под каждый вызов.
Сравниваются все разумные варианты, побеждает самый быстрый:

- весь путь пешком;
- своя машина (инженер уже в ней);
- пешком до свободной машины парка, пересадка, дальше на ней.

Если вызову нужен инструмент со склада, в маршрут входит склад
(промежуточная точка), и варианты становятся многоточечными:

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

STOP_TITLES = {
    STOP_WAREHOUSE: "получить инструмент",
    STOP_BOARD: "посадка в спецтранспорт",
    STOP_ALIGHT: "высадка",
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
        first_drive = next((leg for leg in self.legs if leg.mode == MODE_VEHICLE), None)
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


def stop(graph, kind, node_id, dwell_s, final=False):
    node = graph.node(node_id) or {}
    return {
        "kind": kind,
        "node_id": node_id,
        "name": node.get("ref") if kind == STOP_WAREHOUSE else None,
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


class Network:
    """
    Всё, что общее для кандидатов одного вызова: цель, машины, склады
    и кэш проходов Дейкстры по (точка, способ, скорость).
    """

    def __init__(self, graph, goal_id, vehicles=None, depots=None):
        self.graph = graph
        self.goal = goal_id
        self.prepared = prepare_vehicles(graph, vehicles or [])
        # Склады, через которые обязан пройти маршрут; None — склад не нужен.
        self.depots = depots
        self._maps = {}

    def times(self, start_id, mode, speed_kmh=None):
        key = (start_id, mode, agent_speed(mode, speed_kmh))
        if key not in self._maps:
            self._maps[key] = shortest_paths_from(self.graph, start_id, mode, speed_kmh)
        return self._maps[key]


def seconds(distance_m, mode, speed_kmh=None):
    return distance_m / (agent_speed(mode, speed_kmh) / 3.6)


# --- Варианты ----------------------------------------------------------------

def options(net, employee):
    """
    Все варианты добраться для сотрудника: (секунды, рецепт).

    Рецепт — список шагов: ("walk"|"drive", от, до, подход в метрах)
    и ("stop", вид, узел, задержка, финальная?), плюс машина и признак
    пересадки на неё.
    """
    graph, goal = net.graph, net.goal
    found = []
    own = employee.get("vehicle")
    tools = net.depots

    if own is not None:
        # Машина уже при нём: едет от своих координат.
        speed = own.get("speed_kmh")
        entry, offset = graph.nearest_node(employee["lat"], employee["lon"], MODE_VEHICLE)
        if entry is None:
            return found
        start_s = seconds(offset, MODE_VEHICLE, speed)
        to_goal = net.times(goal, MODE_VEHICLE, speed)
        if not tools and entry["id"] in to_goal:
            found.append((
                start_s + to_goal[entry["id"]] + VEHICLE_BOARDING_S,
                {"vehicle": own, "pickup": False, "steps": [
                    ("drive", entry["id"], goal, offset, speed),
                    ("stop", STOP_ALIGHT, goal, VEHICLE_BOARDING_S, True),
                ]},
            ))
        for depot in tools or []:
            to_depot = net.times(depot, MODE_VEHICLE, speed)
            if entry["id"] in to_depot and depot in to_goal:
                found.append((
                    start_s + to_depot[entry["id"]] + VEHICLE_BOARDING_S + TOOL_PICKUP_S
                    + VEHICLE_BOARDING_S + to_goal[depot] + VEHICLE_BOARDING_S,
                    {"vehicle": own, "pickup": False, "steps": [
                        ("drive", entry["id"], depot, offset, speed),
                        ("stop", STOP_ALIGHT, depot, VEHICLE_BOARDING_S, False),
                        ("stop", STOP_WAREHOUSE, depot, TOOL_PICKUP_S, False),
                        ("stop", STOP_BOARD, depot, VEHICLE_BOARDING_S, False),
                        ("drive", depot, goal, 0.0, speed),
                        ("stop", STOP_ALIGHT, goal, VEHICLE_BOARDING_S, True),
                    ]},
                ))
        return found

    walk_speed = employee.get("speed_kmh")
    entry, approach = graph.nearest_node(employee["lat"], employee["lon"], MODE_WALK)
    if entry is None:
        return found
    start = entry["id"]
    approach_s = seconds(approach, MODE_WALK, walk_speed)
    walk_from_me = net.times(start, MODE_WALK, walk_speed)

    def ride(item, origin_s, origin, origin_approach, destination, to_destination, tail_steps, tail_s):
        """Дойти от origin до машины item, сесть и доехать до destination."""
        node = item["node_id"]
        vehicle = item["vehicle"]
        speed = vehicle.get("speed_kmh")
        offset = item["offset_m"]
        if node not in origin_s or node not in to_destination:
            return
        total = (
            origin_s[node] + seconds(offset, MODE_WALK, walk_speed) + VEHICLE_BOARDING_S
            + seconds(offset, MODE_VEHICLE, speed) + to_destination[node] + tail_s
        )
        steps = [
            ("walk", origin, node, origin_approach + offset, walk_speed),
            ("stop", STOP_BOARD, node, VEHICLE_BOARDING_S, False),
            ("drive", node, destination, offset, speed),
        ] + tail_steps
        return total, {"vehicle": vehicle, "pickup": True, "steps": steps}

    if not tools:
        if goal in walk_from_me:
            found.append((approach_s + walk_from_me[goal], {"vehicle": None, "pickup": False, "steps": [
                ("walk", start, goal, approach, walk_speed),
            ]}))
        for item in net.prepared:
            to_goal = net.times(goal, MODE_VEHICLE, item["vehicle"].get("speed_kmh"))
            option = ride(item, walk_from_me, start, approach, goal, to_goal,
                          [("stop", STOP_ALIGHT, goal, VEHICLE_BOARDING_S, True)], VEHICLE_BOARDING_S)
            if option:
                found.append((approach_s + option[0], option[1]))
        return found

    for depot in tools:
        if depot not in walk_from_me:
            continue
        reach = approach_s + walk_from_me[depot] + TOOL_PICKUP_S
        walk_from_depot = net.times(depot, MODE_WALK, walk_speed)
        warehouse = ("stop", STOP_WAREHOUSE, depot, TOOL_PICKUP_S, False)

        # Склад пешком → пешком к борту.
        if goal in walk_from_depot:
            found.append((reach + walk_from_depot[goal], {"vehicle": None, "pickup": False, "steps": [
                ("walk", start, depot, approach, walk_speed), warehouse,
                ("walk", depot, goal, 0.0, walk_speed),
            ]}))

        for item in net.prepared:
            speed = item["vehicle"].get("speed_kmh")
            to_goal = net.times(goal, MODE_VEHICLE, speed)
            # Склад пешком → пешком к машине → на машине к борту.
            option = ride(item, walk_from_depot, depot, 0.0, goal, to_goal,
                          [("stop", STOP_ALIGHT, goal, VEHICLE_BOARDING_S, True)], VEHICLE_BOARDING_S)
            if option:
                total, recipe = option
                recipe["steps"] = [("walk", start, depot, approach, walk_speed), warehouse] + recipe["steps"]
                found.append((reach + total, recipe))

            # Пешком к машине → на машине к складу → на машине к борту.
            to_depot = net.times(depot, MODE_VEHICLE, speed)
            if depot in to_goal:
                tail = [
                    ("stop", STOP_ALIGHT, depot, VEHICLE_BOARDING_S, False), warehouse,
                    ("stop", STOP_BOARD, depot, VEHICLE_BOARDING_S, False),
                    ("drive", depot, goal, 0.0, speed),
                    ("stop", STOP_ALIGHT, goal, VEHICLE_BOARDING_S, True),
                ]
                tail_s = VEHICLE_BOARDING_S * 3 + TOOL_PICKUP_S + to_goal[depot]
                option = ride(item, walk_from_me, start, approach, depot, to_depot, tail, tail_s)
                if option:
                    found.append((approach_s + option[0], option[1]))
    return found


def build(net, recipe):
    """Восстанавливает полные участки маршрута по рецепту варианта."""
    graph = net.graph
    legs = []
    stops = []
    for step in recipe["steps"]:
        if step[0] == "stop":
            _, kind, node_id, dwell, final = step
            stops.append(stop(graph, kind, node_id, dwell, final))
            continue
        kind, origin, destination, approach, speed = step
        mode = MODE_WALK if kind == "walk" else MODE_VEHICLE
        route = shortest_path(graph, origin, destination, mode, speed)
        if route is None:
            return None
        legs.append(Route(route.node_ids, route.distance_m, mode, approach, speed, route.path_seconds))
    return Plan(legs, vehicle=recipe["vehicle"], pickup=recipe["pickup"], stops=stops)


def best_plan(net, employee):
    """
    Самый быстрый способ добраться для одного сотрудника.

    При равенстве честнее идти пешком, чем занимать машину, которая может
    понадобиться другому: варианты сравниваются строго, пеший — первым.
    """
    best = None
    for total, recipe in options(net, employee):
        if best is None or total < best[0]:
            best = (total, recipe)
    return build(net, best[1]) if best else None


# --- Точки входа -------------------------------------------------------------

def plan_for(graph, employee, goal_id, vehicles, depots=None):
    """
    Способ добраться для одного сотрудника — при назначении.

    Тот же расчёт, что в подборе, чтобы время, которое увидел диспетчер,
    совпало с временем, записанным в вызов.
    """
    return best_plan(Network(graph, goal_id, vehicles, depots), employee)


def plan_walk(graph, employee, goal_id, depots=None):
    """Весь путь пешком, даже если рядом есть машина: так решил диспетчер."""
    if depots:
        walker = {**employee, "vehicle": None}
        return best_plan(Network(graph, goal_id, [], depots), walker)
    route = route_from_point(
        graph, employee["lat"], employee["lon"], goal_id, MODE_WALK, employee.get("speed_kmh")
    )
    return Plan([route]) if route is not None else None


def plan_via(graph, employee, goal_id, vehicle):
    """
    Дойти до указанной машины и ехать на ней — вторым в машине ответственного.

    Машина может быть уже занята: в ней поедет ответственный инженер, и
    второй идёт к ней, а не к свободной. Поэтому расчёт по одной заданной
    машине, без сравнения с остальными.
    """
    walker = {**employee, "vehicle": None}
    net = Network(graph, goal_id, [vehicle])
    rides = [(total, recipe) for total, recipe in options(net, walker) if recipe["pickup"]]
    if not rides:
        return None
    return build(net, min(rides, key=lambda option: option[0])[1])
