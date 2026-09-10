"""
Поиск кратчайшего маршрута по графу аэропорта и расчёт времени в пути.

Алгоритм — Дейкстра. Выбран сознательно: веса рёбер (длины участков
рулёжек) неотрицательны, граф небольшой и статичный, а результат обязан
быть доказуемо оптимальным. Инвариант из ТЗ — «система никогда не назначает
сотрудника, который будет в пути дольше другого доступного» — держится
именно на оптимальности Дейкстры, поэтому эвристики вроде A* здесь не дают
выигрыша, но добавляют риск.
"""

import heapq

from constants import MODE_WALK, SPEED_KMH_BY_MODE


class Route:
    """Найденный маршрут: последовательность вершин, длина и время."""

    def __init__(self, node_ids, distance_m, mode, approach_m=0.0, speed_kmh=None):
        self.node_ids = node_ids
        self.mode = mode
        # Подход — расстояние от фактических координат сотрудника до первой
        # вершины графа. Не учитывать его нечестно: сотрудник стоит не в узле.
        self.approach_m = approach_m
        self.distance_m = distance_m + approach_m
        self.speed_kmh = speed_kmh
        self.minutes = travel_time_minutes(self.distance_m, mode, speed_kmh)

    def as_dict(self):
        """Представление для передачи в API и отрисовки на карте."""
        return {
            "node_ids": self.node_ids,
            "distance_m": round(self.distance_m, 1),
            "approach_m": round(self.approach_m, 1),
            "mode": self.mode,
            "minutes": round(self.minutes, 1),
        }


def travel_time_minutes(distance_m, mode, speed_kmh=None):
    """
    Время в пути в минутах: расстояние, делённое на скорость.

    По умолчанию берётся нормативная скорость способа передвижения.
    Индивидуальная скорость сотрудника (поле speed_kmh в модели данных)
    переопределяет её: у машины технической помощи и у автолестницы
    скорость по перрону разная.
    """
    effective_speed = speed_kmh or SPEED_KMH_BY_MODE[mode]
    return (distance_m / 1000.0) / effective_speed * 60.0


def shortest_path(graph, start_id, goal_id, mode=MODE_WALK):
    """
    Кратчайший путь между двумя вершинами графа.

    Возвращает Route или None, если пути нет: для спецтранспорта такое
    бывает штатно — часть перрона проходима только пешком.
    """
    if start_id not in graph.nodes or goal_id not in graph.nodes:
        return None

    if start_id == goal_id:
        return Route([start_id], 0.0, mode)

    # Накопленная длина пути до вершины и вершина, из которой в неё пришли.
    best_distance = {start_id: 0.0}
    came_from = {}
    visited = set()
    queue = [(0.0, start_id)]

    while queue:
        distance, current_id = heapq.heappop(queue)

        # Вершина уже раскрыта по более короткому пути — эта запись устарела.
        if current_id in visited:
            continue
        visited.add(current_id)

        if current_id == goal_id:
            return Route(restore_path(came_from, start_id, goal_id), distance, mode)

        for neighbour_id, edge_length in graph.neighbours(current_id, mode):
            if neighbour_id in visited:
                continue
            candidate = distance + edge_length
            if neighbour_id not in best_distance or candidate < best_distance[neighbour_id]:
                best_distance[neighbour_id] = candidate
                came_from[neighbour_id] = current_id
                heapq.heappush(queue, (candidate, neighbour_id))

    return None


def restore_path(came_from, start_id, goal_id):
    """Разворачивает цепочку предшественников в маршрут от старта к цели."""
    path = [goal_id]
    current_id = goal_id
    while current_id != start_id:
        current_id = came_from[current_id]
        path.append(current_id)
    path.reverse()
    return path


def route_from_point(graph, lat, lon, goal_id, mode=MODE_WALK, speed_kmh=None):
    """
    Маршрут от фактических координат сотрудника до вершины назначения.

    Сотрудник находится в произвольной точке перрона, а не в узле графа,
    поэтому сначала ищем ближайшую доступную вершину, а расстояние до неё
    добавляем к длине маршрута как подход.
    """
    entry_node, approach_m = graph.nearest_node(lat, lon, mode)
    if entry_node is None:
        return None

    route = shortest_path(graph, entry_node["id"], goal_id, mode)
    if route is None:
        return None

    return Route(route.node_ids, route.distance_m, mode, approach_m, speed_kmh)


def shortest_paths_from(graph, start_id, mode=MODE_WALK):
    """
    Длины кратчайших путей от вершины до всех достижимых вершин.

    Один проход Дейкстры вместо отдельного поиска до каждой цели.
    Пригодится, когда от одного техцентра нужно оценить сразу много
    стоянок — например, при подготовке демонстрационных сценариев.
    """
    if start_id not in graph.nodes:
        return {}

    best_distance = {start_id: 0.0}
    visited = set()
    queue = [(0.0, start_id)]

    while queue:
        distance, current_id = heapq.heappop(queue)
        if current_id in visited:
            continue
        visited.add(current_id)

        for neighbour_id, edge_length in graph.neighbours(current_id, mode):
            candidate = distance + edge_length
            if neighbour_id not in best_distance or candidate < best_distance[neighbour_id]:
                best_distance[neighbour_id] = candidate
                heapq.heappush(queue, (candidate, neighbour_id))

    return best_distance
