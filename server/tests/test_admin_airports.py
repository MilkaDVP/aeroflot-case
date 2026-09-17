"""
Тесты конструктора аэропортов.

Аэропорт заводится двумя способами: выгрузкой настоящего графа из
OpenStreetMap по коду ИКАО и ручной разметкой по точкам на карте.
Проверяется главное — что нерабочий граф не попадёт в систему: без
стоянок, без техцентра или с точками, не соединёнными между собой,
вызов окажется невыполним, а узнавать об этом в момент вызова к борту
недопустимо.

Импорт из OSM подменяется заглушкой: тесты не ходят в интернет,
иначе они зависели бы от доступности публичного Overpass.
"""

from unittest.mock import patch

import graph_registry
from tests.test_api import ApiTestCase, auth_header

# Маленький «аэропорт» из четырёх точек: техцентр, перекрёсток и две
# стоянки. Достаточно, чтобы проверить и связность, и маршрут.
NODES = [
    {"id": "t1", "type": "tech_center", "ref": "ТЦ-1", "lat": 55.4000, "lon": 37.9000},
    {"id": "j1", "type": "junction", "ref": None, "lat": 55.4010, "lon": 37.9020},
    {"id": "s1", "type": "stand", "ref": "1", "lat": 55.4020, "lon": 37.9040},
    {"id": "s2", "type": "stand", "ref": "2", "lat": 55.4005, "lon": 37.9050},
]
EDGES = [
    {"from_id": "t1", "to_id": "j1", "vehicle_allowed": True},
    {"from_id": "j1", "to_id": "s1", "vehicle_allowed": True},
    {"from_id": "j1", "to_id": "s2", "vehicle_allowed": False},
]

FAKE_OSM_GRAPH = {
    "icao": "ZZZZ",
    "name": "Тестовый аэродром",
    "city": "Тестоград",
    "ref_point": {"lat": 55.4, "lon": 37.9},
    "source": "заглушка теста",
    "generated_at": "2026-01-01T00:00:00+00:00",
    "nodes": [
        {"id": "n1", "type": "tech_center", "ref": "ТЦ", "lat": 55.4, "lon": 37.9,
         "x": 0.0, "y": 0.0},
        {"id": "n2", "type": "stand", "ref": "10", "lat": 55.401, "lon": 37.902,
         "x": 126.0, "y": -111.0},
    ],
    "edges": [
        {"from_id": "n1", "to_id": "n2", "distance_m": 168.0,
         "vehicle_allowed": True, "points": []},
    ],
}
FAKE_AERODROME = {
    "icao": "ZZZZ",
    "name": "Тестовый аэродром",
    "city": "Тестоград",
    "bbox": (55.39, 37.89, 55.41, 37.91),
}


class TestAdminAirports(ApiTestCase):
    """Создание, разметка и удаление аэропортов администратором."""

    def setUp(self):
        self.reset_database()
        self.admin = self.login("admin", "admin")
        self.dispatcher = self.login("dispatcher", "dispatcher")
        self.created = []

    def tearDown(self):
        # Реестр графов живёт в памяти процесса и переживает пересоздание
        # базы: без уборки следующий тест увидел бы чужой аэропорт.
        for icao in self.created:
            graph_registry.forget(icao)

    # --- Вспомогательное ---

    def create_empty(self, icao="TSTA", name="Учебный аэропорт"):
        response = self.client.post(
            "/api/admin/airports",
            json={"icao": icao, "name": name, "city": "Тестоград",
                  "lat": 55.4, "lon": 37.9},
            headers=auth_header(self.admin),
        )
        self.assertEqual(response.status_code, 201, response.text)
        self.created.append(icao)
        return response.json()

    def save_graph(self, icao, nodes=None, edges=None):
        return self.client.put(
            f"/api/admin/airports/{icao}/graph",
            json={"nodes": nodes if nodes is not None else NODES,
                  "edges": edges if edges is not None else EDGES},
            headers=auth_header(self.admin),
        )

    # --- Список ---

    def test_администратор_видит_встроенные_аэропорты(self):
        response = self.client.get(
            "/api/admin/airports", headers=auth_header(self.admin)
        )
        self.assertEqual(response.status_code, 200, response.text)

        by_icao = {item["icao"]: item for item in response.json()}
        self.assertIn("UUEE", by_icao)
        self.assertEqual(by_icao["UUEE"]["source"], "builtin")
        self.assertGreater(by_icao["UUEE"]["stands"], 100)
        # Встроенный граф лежит в файле репозитория и через интерфейс
        # не правится.
        self.assertFalse(by_icao["UUEE"]["editable"])

    def test_диспетчер_не_вправе_управлять_аэропортами(self):
        response = self.client.get(
            "/api/admin/airports", headers=auth_header(self.dispatcher)
        )
        self.assertEqual(response.status_code, 403, response.text)

    # --- Ручное создание ---

    def test_создание_пустого_аэропорта(self):
        body = self.create_empty()

        self.assertEqual(body["icao"], "TSTA")
        self.assertEqual(body["source"], "manual")
        self.assertEqual(body["nodes"], 0)
        self.assertTrue(body["editable"])

    def test_новый_аэропорт_доступен_администратору_сразу(self):
        self.create_empty()

        response = self.client.get("/api/airports", headers=auth_header(self.admin))
        self.assertIn("TSTA", [item["icao"] for item in response.json()])

    def test_повторный_код_отклоняется(self):
        self.create_empty()
        response = self.client.post(
            "/api/admin/airports",
            json={"icao": "TSTA", "name": "Другой", "city": "", "lat": 1.0, "lon": 1.0},
            headers=auth_header(self.admin),
        )
        self.assertEqual(response.status_code, 409, response.text)

    # --- Разметка по точкам ---

    def test_сохранение_нарисованного_графа(self):
        self.create_empty()
        response = self.save_graph("TSTA")
        self.assertEqual(response.status_code, 200, response.text)

        body = response.json()
        self.assertEqual(body["nodes"], 4)
        self.assertEqual(body["stands"], 2)
        self.assertEqual(body["tech_centers"], 1)
        self.assertEqual(body["edges"], 3)

    def test_граф_сразу_отдаётся_карте(self):
        """Нарисованный аэропорт должен открываться без перезапуска сервера."""
        self.create_empty()
        self.save_graph("TSTA")

        response = self.client.get(
            "/api/airports/TSTA", headers=auth_header(self.admin)
        )
        self.assertEqual(response.status_code, 200, response.text)

        body = response.json()
        self.assertEqual(len(body["nodes"]), 4)
        # Длины и метровые координаты считает сервер: редактор их не шлёт.
        edge = body["edges"][0]
        self.assertGreater(edge["distance_m"], 0)
        self.assertIsNotNone(body["nodes"][0]["x"])

    def test_граф_сообщает_своё_происхождение(self):
        """Под нарисованным аэропортом не должно стоять «Граф: OpenStreetMap»."""
        self.create_empty()
        self.save_graph("TSTA")

        manual = self.client.get("/api/airports/TSTA", headers=auth_header(self.admin))
        builtin = self.client.get("/api/airports/UUEE", headers=auth_header(self.admin))

        self.assertEqual(manual.json()["source"], "manual")
        self.assertEqual(builtin.json()["source"], "builtin")

    def test_длина_ребра_считается_по_координатам(self):
        self.create_empty()
        self.save_graph("TSTA")

        response = self.client.get(
            "/api/airports/TSTA", headers=auth_header(self.admin)
        )
        by_pair = {
            (edge["from_id"], edge["to_id"]): edge for edge in response.json()["edges"]
        }
        # t1 → j1: примерно 160 метров по координатам из NODES.
        self.assertAlmostEqual(by_pair[("t1", "j1")]["distance_m"], 160, delta=20)

    def test_правка_выгруженного_графа_сохраняет_длины_рулёжек(self):
        """
        Импортированный из OSM аэропорт дорисовывают в редакторе — у Пулково,
        например, в OSM нет ни одной стоянки. Сохранение не должно пересчитать
        настоящие изогнутые рулёжки по прямой: длина хорды меньше, и время
        в пути оказалось бы заниженным.
        """
        curved = dict(FAKE_OSM_GRAPH)
        curved["edges"] = [dict(FAKE_OSM_GRAPH["edges"][0], distance_m=400.0,
                                points=[[50.0, -20.0], [90.0, -80.0]])]
        with patch("api.admin_airports.import_airport",
                   return_value=(curved, FAKE_AERODROME)):
            self.client.post("/api/admin/airports/import", json={"icao": "ZZZZ"},
                             headers=auth_header(self.admin))
        self.created.append("ZZZZ")

        graph = self.client.get("/api/airports/ZZZZ", headers=auth_header(self.admin)).json()
        nodes = [{key: node[key] for key in ("id", "type", "ref", "lat", "lon")}
                 for node in graph["nodes"]]
        # Дорисовываем стоянку и связываем её с существующей точкой.
        nodes.append({"id": "p1", "type": "stand", "ref": "11",
                      "lat": 55.4015, "lon": 37.9030})
        edges = [
            # Прежняя рулёжка приходит в обратном направлении.
            {"from_id": "n2", "to_id": "n1", "vehicle_allowed": True},
            {"from_id": "n2", "to_id": "p1", "vehicle_allowed": True},
        ]
        response = self.client.put("/api/admin/airports/ZZZZ/graph",
                                   json={"nodes": nodes, "edges": edges},
                                   headers=auth_header(self.admin))
        self.assertEqual(response.status_code, 200, response.text)

        saved = self.client.get("/api/airports/ZZZZ", headers=auth_header(self.admin)).json()
        by_pair = {(e["from_id"], e["to_id"]): e for e in saved["edges"]}

        old = by_pair[("n2", "n1")]
        self.assertEqual(old["distance_m"], 400.0)
        # Изгиб развёрнут вместе с направлением связи.
        self.assertEqual(old["points"], [[90.0, -80.0], [50.0, -20.0]])
        # Опорная точка прежняя — иначе изгибы съехали бы с рулёжек.
        self.assertEqual(saved["ref_point"], FAKE_OSM_GRAPH["ref_point"])
        # Новая связь — по прямой.
        self.assertLess(by_pair[("n2", "p1")]["distance_m"], 200)
        self.assertEqual(saved["source"], "osm")

    # --- Проверки графа ---

    def test_граф_без_стоянок_отклоняется(self):
        self.create_empty()
        nodes = [node for node in NODES if node["type"] != "stand"]
        response = self.save_graph("TSTA", nodes=nodes, edges=EDGES[:1])

        self.assertEqual(response.status_code, 422, response.text)
        self.assertIn("стоянка", response.json()["detail"].lower())

    def test_граф_без_техцентра_отклоняется(self):
        self.create_empty()
        nodes = [node for node in NODES if node["type"] != "tech_center"]
        edges = [edge for edge in EDGES if "t1" not in (edge["from_id"], edge["to_id"])]
        response = self.save_graph("TSTA", nodes=nodes, edges=edges)

        self.assertEqual(response.status_code, 422, response.text)
        self.assertIn("техцентр", response.json()["detail"].lower())

    def test_несвязанные_точки_отклоняются(self):
        """
        Главная проверка: оторванная стоянка означает невыполнимый вызов.

        Внешне такой граф выглядит нормально, но маршрут к этой стоянке
        не построится, и на демонстрации это будет похоже на отказ
        алгоритма.
        """
        self.create_empty()
        response = self.save_graph("TSTA", edges=EDGES[:2])

        self.assertEqual(response.status_code, 422, response.text)
        self.assertIn("соединен", response.json()["detail"].lower())

    def test_связь_на_несуществующую_точку_отклоняется(self):
        self.create_empty()
        edges = EDGES + [{"from_id": "s1", "to_id": "нет-такой", "vehicle_allowed": True}]
        response = self.save_graph("TSTA", edges=edges)

        self.assertEqual(response.status_code, 422, response.text)

    def test_встроенный_аэропорт_через_интерфейс_не_правится(self):
        response = self.save_graph("UUEE")
        self.assertEqual(response.status_code, 409, response.text)

    # --- Импорт из OpenStreetMap ---

    def test_импорт_по_коду_икао(self):
        with patch(
            "api.admin_airports.import_airport",
            return_value=(FAKE_OSM_GRAPH, FAKE_AERODROME),
        ):
            response = self.client.post(
                "/api/admin/airports/import",
                json={"icao": "ZZZZ"},
                headers=auth_header(self.admin),
            )

        self.assertEqual(response.status_code, 201, response.text)
        self.created.append("ZZZZ")

        body = response.json()
        self.assertEqual(body["source"], "osm")
        self.assertEqual(body["name"], "Тестовый аэродром")
        self.assertEqual(body["stands"], 1)

    def test_неизвестный_код_икао(self):
        with patch("api.admin_airports.import_airport", return_value=(None, None)):
            response = self.client.post(
                "/api/admin/airports/import",
                json={"icao": "ZZZY"},
                headers=auth_header(self.admin),
            )
        self.assertEqual(response.status_code, 404, response.text)

    def test_аэродром_без_рулёжек(self):
        with patch(
            "api.admin_airports.import_airport", return_value=(None, FAKE_AERODROME)
        ):
            response = self.client.post(
                "/api/admin/airports/import",
                json={"icao": "ZZZW"},
                headers=auth_header(self.admin),
            )
        self.assertEqual(response.status_code, 422, response.text)
        self.assertIn("вручную", response.json()["detail"])

    def test_недоступный_overpass_объясняется(self):
        """Отказ внешнего сервиса не должен выглядеть как ошибка системы."""
        with patch(
            "api.admin_airports.import_airport",
            side_effect=RuntimeError("Overpass API недоступен: timeout"),
        ):
            response = self.client.post(
                "/api/admin/airports/import",
                json={"icao": "ZZZV"},
                headers=auth_header(self.admin),
            )
        self.assertEqual(response.status_code, 503, response.text)
        self.assertIn("OpenStreetMap", response.json()["detail"])

    # --- Удаление ---

    def test_удаление_созданного_аэропорта(self):
        self.create_empty()
        response = self.client.delete(
            "/api/admin/airports/TSTA", headers=auth_header(self.admin)
        )
        self.assertEqual(response.status_code, 204, response.text)

        listing = self.client.get("/api/admin/airports", headers=auth_header(self.admin))
        self.assertNotIn("TSTA", [item["icao"] for item in listing.json()])

    def test_встроенный_аэропорт_не_удаляется(self):
        response = self.client.delete(
            "/api/admin/airports/UUEE", headers=auth_header(self.admin)
        )
        self.assertEqual(response.status_code, 409, response.text)
