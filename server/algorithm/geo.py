"""
Геометрия на поверхности Земли.

Сервер получает от PWA инженера сырые GPS-координаты, которые не совпадают
ни с одним узлом графа. Здесь то, что нужно, чтобы посадить такую точку
на граф и посчитать расстояние до неё.
"""

import math

EARTH_RADIUS_M = 6371008.8


def distance_to_segment(lat, lon, a, b):
    scale = math.cos(math.radians(lat))
    def offset(point):
        return (math.radians(point["lon"] - lon) * EARTH_RADIUS_M * scale,
                math.radians(point["lat"] - lat) * EARTH_RADIUS_M)
    x, y = offset(a)
    end_x, end_y = offset(b)
    dx, dy = end_x - x, end_y - y
    length2 = dx * dx + dy * dy
    t = max(0, min(1, -(x * dx + y * dy) / length2)) if length2 else 0
    return math.hypot(x + t * dx, y + t * dy)


def haversine_m(lat1, lon1, lat2, lon2):
    """Расстояние между двумя точками по поверхности Земли, в метрах."""
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    d_phi = math.radians(lat2 - lat1)
    d_lambda = math.radians(lon2 - lon1)
    a = (
        math.sin(d_phi / 2) ** 2
        + math.cos(phi1) * math.cos(phi2) * math.sin(d_lambda / 2) ** 2
    )
    return 2 * EARTH_RADIUS_M * math.asin(math.sqrt(a))
