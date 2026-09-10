"""
Геометрия на поверхности Земли.

Сервер получает от PWA инженера сырые GPS-координаты, которые не совпадают
ни с одним узлом графа. Здесь то, что нужно, чтобы посадить такую точку
на граф и посчитать расстояние до неё.
"""

import math

EARTH_RADIUS_M = 6371008.8


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
