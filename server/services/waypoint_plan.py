"""
Лучший порядок промежуточных точек вызова.

Порядок слов в просьбе — не порядок движения: «через склад и стоянку 4»
может означать проезд мимо стоянки к складу и обратно. Здесь перебираются
все порядки (и варианты, если годится любой склад), время каждого считает
тот же расчёт, что и при назначении, и выбирается самый быстрый. При
равном времени — тот, где меньше узлов проходится повторно.

Только чтение: точки записывает обычный запрос «Промежуточные точки».
"""

from itertools import permutations, product

from fastapi import HTTPException

from algorithm.transport import Network, build, place_name, prepare_vehicles, search
from constants import VEHICLE_RESERVED
from services.assignment import depots_for, free_vehicles

# Больше вариантов не перебираем: запрос должен отвечать сразу.
MAX_OPTIONS = 800
# Разница в пределах секунды — равное время.
EQUAL_S = 1.0
# Сколько лучших кандидатов сравнивать, пока инженер не назначен.
MAX_PEOPLE = 5


class Traveller:
    """Кто едет: данные для расчёта, доступные машины и разрешён ли парк."""

    def __init__(self, name, person, vehicles, allow_fleet=True):
        self.name = name
        self.person = person
        self.vehicles = vehicles
        self.allow_fleet = allow_fleet


def assigned_traveller(call):
    """Назначенный инженер — со своей машиной, как в assigned_route."""
    employee = call.assigned_employee
    if employee.lat is None or employee.lon is None:
        return []
    person = employee.as_algorithm_dict()
    vehicle = call.vehicle
    if vehicle is None:
        return [Traveller(employee.full_name, {**person, "vehicle": None}, [], False)]
    if vehicle.status == VEHICLE_RESERVED or vehicle.employee_id != employee.id:
        return [Traveller(employee.full_name, {**person, "vehicle": None}, [vehicle.as_algorithm_dict()])]
    return [Traveller(employee.full_name, person, [])]


def clean_groups(graph, call, groups):
    """Убирает лишнее: сам борт, повторы и уже пройденные точки."""
    done = {item["node_id"] for item in call.waypoints or [] if item.get("done_at")}
    result, skipped = [], []
    for group in groups:
        for node_id in group:
            if graph.node(node_id) is None:
                raise HTTPException(422, f"Точки {node_id} нет на карте аэропорта")
        passed = [node_id for node_id in group if node_id in done]
        if passed:
            skipped.append(f"{place_name(graph.node(passed[0])) or passed[0]} — уже пройдена")
            continue
        group = [node_id for node_id in dict.fromkeys(group) if node_id != call.stand_node_id]
        if not group or group in result:
            continue
        result.append(group)
    return result, skipped


def orders_of(groups, keep_order):
    """Все цепочки узлов: порядки точек и варианты «любой склад»."""
    count = 1
    for index, group in enumerate(groups, 1):
        count *= len(group) * (1 if keep_order else index)
        if count > MAX_OPTIONS:
            raise HTTPException(422, "Слишком много точек — порядок не подобрать. Оставьте до шести")
    chains = []
    for order in [groups] if keep_order else permutations(groups):
        for chain in product(*order):
            # «Любой склад» может совпасть с названным — один и тот же дважды не нужен.
            if len(set(chain)) == len(chain) and list(chain) not in chains:
                chains.append(list(chain))
    return chains


def describe(graph, chain):
    return [place_name(graph.node(node_id)) or node_id for node_id in chain]


class NearestCache:
    """Граф, который помнит ближайшую вершину к уже спрошенной точке."""

    def __init__(self, graph):
        self._graph = graph
        self._found = {}

    def __getattr__(self, name):
        return getattr(self._graph, name)

    def nearest_node(self, *args):
        if args not in self._found:
            self._found[args] = self._graph.nearest_node(*args)
        return self._found[args]


def evaluate(graph, call, travellers, chains):
    """Варианты: (секунды, цепочка, кто, план) — для каждой цепочки лучший инженер."""
    depots = depots_for(call, graph)
    # Посадка машин на граф и проходы Дейкстры от цепочки не зависят:
    # сеть каждого едущего — одна на все варианты.
    maps = {}
    bases = []
    for traveller in travellers:
        base = Network(graph, call.stand_node_id, [])
        # Набор машин у кандидатов общий — готовим его один раз.
        shared = next((b for t, b in bases if t.vehicles is traveller.vehicles), None)
        base.prepared = shared.prepared if shared else prepare_vehicles(graph, traveller.vehicles)
        bases.append((traveller, base))
    graph = NearestCache(graph)
    found = []
    for chain in chains:
        best = None
        for traveller, base in bases:
            net = Network(graph, call.stand_node_id, [], depots, chain)
            net.prepared = base.prepared
            net._maps = maps
            option = search(net, traveller.person, allow_fleet=traveller.allow_fleet)
            if option and (best is None or option[0] < best[0]):
                best = (option[0], chain, traveller, net, option[1])
        if best:
            found.append(best)
    return sorted(found, key=lambda item: item[0])


def option_dict(graph, item):
    seconds, chain, traveller, net, recipe = item
    plan = build(net, recipe)
    nodes = plan.node_ids
    return {
        "node_ids": chain,
        "names": describe(graph, chain),
        "minutes": round(seconds / 60, 1),
        "engineer": traveller.name,
        "vehicle": plan.vehicle["call_sign"] if plan.vehicle else None,
        "distance_m": round(plan.distance_m),
        "repeats": len(nodes) - len(set(nodes)),
        "steps": [f"{s['title']}: {s['name'] or s['node_id']}" for s in plan.stops if not s.get("final")],
    }


def plan(db, call, graph, groups, keep_order, candidates):
    """
    Лучшая цепочка и с чем её сравнивали.

    candidates — функция, возвращающая подбор; нужна, пока инженера нет.
    """
    groups, skipped = clean_groups(graph, call, groups)
    chains = orders_of(groups, keep_order)
    if call.assigned_employee is not None:
        travellers = assigned_traveller(call)
    else:
        pool = [vehicle.as_algorithm_dict() for vehicle in free_vehicles(db, call.airport_icao)]
        travellers = [
            Traveller(c.employee["full_name"], c.employee, pool) for c in candidates().candidates[:MAX_PEOPLE]
        ]

    found = evaluate(graph, call, travellers, chains)
    if not found:
        # Ехать некому или путь не строится — порядок как назван.
        first = chains[0] if chains else [group[0] for group in groups]
        return {"node_ids": first, "names": describe(graph, first), "minutes": None, "engineer": None,
                "vehicle": None, "distance_m": None, "repeats": None, "steps": [],
                "skipped": skipped, "as_named": None, "direct_minutes": None}

    equal = [item for item in found if item[0] - found[0][0] <= EQUAL_S][:10]
    best = min((option_dict(graph, item) for item in equal), key=lambda o: (o["repeats"], o["minutes"]))

    # С чем сравнить: порядок, как назвали, и путь совсем без точек.
    named = {tuple(chain) for chain in product(*groups)}
    as_named = next((item for item in found if tuple(item[1]) in named), None)
    if as_named is not None and as_named[1] == best["node_ids"]:
        as_named = None
    direct = evaluate(graph, call, travellers, [[]])
    return {
        **best,
        "skipped": skipped,
        "as_named": option_dict(graph, as_named) if as_named else None,
        "direct_minutes": round(direct[0][0] / 60, 1) if direct else None,
    }
