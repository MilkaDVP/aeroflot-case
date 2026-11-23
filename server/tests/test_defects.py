"""
Тесты открытого справочника неисправностей.

Эксперты Аэрофлота указали, что закрытый список неисправностей не покрывает
реальных дефектов. Проверяется, что диспетчер находит тип словами экипажа
или номером главы ATA, заводит новый тип, когда подходящего нет, и что
новый тип сразу работает в подборе — с правильной отметкой по категории
и типу ВС.
"""

from database import SessionLocal
from models.defect_type import DefectType
from services.defects import ensure_builtin_defects
from tests.test_api import ApiTestCase, auth_header


class DefectApiCase(ApiTestCase):
    """Общая подготовка: чистая база и вход диспетчером."""

    def setUp(self):
        self.reset_database()
        self.dispatcher = self.login("dispatcher", "dispatcher")

    def search(self, query):
        response = self.client.get(
            "/api/defect-types", params={"q": query}, headers=auth_header(self.dispatcher)
        )
        self.assertEqual(response.status_code, 200, response.text)
        return [item["code"] for item in response.json()]

    def create_type(self, name, ata, category=None, token=None):
        body = {"name": name, "ata": ata}
        if category:
            body["category"] = category
        return self.client.post(
            "/api/defect-types", json=body, headers=auth_header(token or self.dispatcher)
        )

    def register_call(self, defect_code, description=None, board="VP-BZQ"):
        return self.client.post(
            "/api/calls",
            json={
                "aircraft_id": self.board_id(self.dispatcher, board),
                "defect_code": defect_code,
                "description": description,
            },
            headers=auth_header(self.dispatcher),
        )


class TestSearch(DefectApiCase):
    """Поиск словами экипажа и по главам ATA."""

    def test_слово_в_другой_форме_находит_тип(self):
        # Экипаж говорит «утечки», в справочнике — «Утечка гидрожидкости».
        self.assertEqual(self.search("утечки")[0], "hydraulic_leak")

    def test_синоним_находит_несколько_типов(self):
        # «Течь» записана в синонимах и у гидросистемы, и у топливной.
        found = self.search("течь")
        self.assertIn("hydraulic_leak", found)
        self.assertIn("fuel_system_fault", found)

    def test_слово_в_названии_выше_синонима(self):
        # У «Утечки гидрожидкости» слово «течь» только в синонимах, а у
        # заведённого типа — в самом названии: он и должен стоять выше.
        custom = self.create_type("Течь воды в буфетной зоне", "38").json()
        found = self.search("течь")
        self.assertLess(found.index(custom["code"]), found.index("hydraulic_leak"))

    def test_номер_главы_оставляет_только_эту_главу(self):
        found = self.search("ATA 32")
        self.assertEqual(set(found), {"landing_gear_fault", "brake_wear"})

    def test_номер_главы_попадает_в_диапазон(self):
        # «Отказ запуска двигателя» записан главами 71–80.
        self.assertIn("engine_start_fault", self.search("72"))

    def test_регистр_и_ё_не_важны(self):
        self.assertIn("apu_fault", self.search("всу"))
        self.assertIn("landing_gear_fault", self.search("ШАССИ"))

    def test_пустой_запрос_отдаёт_весь_справочник_по_главам(self):
        response = self.client.get(
            "/api/defect-types", headers=auth_header(self.dispatcher)
        ).json()
        self.assertGreaterEqual(len(response), 12)
        chapters = [int(item["ata"].split("-")[0]) for item in response]
        self.assertEqual(chapters, sorted(chapters))

    def test_несуществующее_ничего_не_находит(self):
        self.assertEqual(self.search("квантовый телепорт"), [])

    def test_главы_ata_отдаются_с_категорией_по_умолчанию(self):
        chapters = {
            item["ata"]: item
            for item in self.client.get(
                "/api/ata-chapters", headers=auth_header(self.dispatcher)
            ).json()
        }
        self.assertEqual(chapters["29"]["default_category"], "B1")
        self.assertEqual(chapters["34"]["default_category"], "B2")
        self.assertEqual(chapters["33"]["default_category"], "A")


class TestCreate(DefectApiCase):
    """Пополнение справочника из формы вызова."""

    def test_новый_тип_получает_категорию_по_главе(self):
        response = self.create_type("Не открывается грузовой люк", "52")
        self.assertEqual(response.status_code, 201, response.text)

        body = response.json()
        self.assertEqual(body["category"], "B1")
        self.assertEqual(body["ata_title"], "Двери")
        self.assertFalse(body["is_builtin"])
        self.assertTrue(body["code"].startswith("custom-"))
        # И сразу находится поиском.
        self.assertIn(body["code"], self.search("люк"))

    def test_категорию_можно_задать_вопреки_главе(self):
        # Посадочные фары — глава «Освещение», но это работа B1, а не A.
        response = self.create_type("Не горит посадочная фара", "33", "B1")
        self.assertEqual(response.status_code, 201, response.text)
        self.assertEqual(response.json()["category"], "B1")

    def test_дубль_по_названию_отклоняется(self):
        self.create_type("Течь в туалетном модуле", "38")
        # Другой регистр, лишние пробелы — это всё ещё тот же тип.
        response = self.create_type("  течь  в ТУАЛЕТНОМ модуле ", "38")
        self.assertEqual(response.status_code, 409, response.text)
        self.assertIn("Течь в туалетном модуле", response.json()["detail"])

    def test_дубль_встроенного_типа_отклоняется(self):
        response = self.create_type("утечка гидрожидкости", "29")
        self.assertEqual(response.status_code, 409, response.text)

    def test_неизвестная_глава_отклоняется(self):
        response = self.create_type("Что-то непонятное", "99")
        self.assertEqual(response.status_code, 422, response.text)

    def test_категория_c_для_перрона_недопустима(self):
        # C — право подписи при базовом ТО, а не вид работ у борта.
        response = self.create_type("Подписать карту-наряд", "25", "C")
        self.assertEqual(response.status_code, 422, response.text)

    def test_инженер_не_заводит_типы(self):
        engineer = self.login("sokolov", "engineer")
        response = self.create_type("Скрип двери кабины", "52", token=engineer)
        self.assertEqual(response.status_code, 403, response.text)


class TestCallWithNewType(DefectApiCase):
    """Новый тип неисправности сразу работает в вызове и подборе."""

    def test_тип_b2_требует_отметку_b2_без_подкатегории(self):
        defect = self.create_type("Сбой системы развлечения пассажиров", "46").json()
        self.assertEqual(defect["category"], "B2")

        call = self.register_call(defect["code"], "Экраны в рядах 10–20 не включаются")
        self.assertEqual(call.status_code, 201, call.text)
        body = call.json()
        self.assertEqual(body["required_mark"], "B2")
        self.assertEqual(body["defect_name"], "Сбой системы развлечения пассажиров")
        self.assertEqual(body["defect_ata"], "46")
        self.assertEqual(body["defect_description"], "Экраны в рядах 10–20 не включаются")

        # Подбор идёт по записанной отметке: все кандидаты — с B2.
        result = self.client.post(
            f"/api/calls/{body['id']}/suggest", headers=auth_header(self.dispatcher)
        ).json()
        self.assertEqual(result["required_mark"], "B2")
        self.assertTrue(result["candidates"] or result["busy"], result["message"])
        for candidate in result["candidates"]:
            self.assertEqual(candidate["mark"], "B2")

    def test_тип_b1_на_a320_требует_b1_1(self):
        defect = self.create_type("Заедание замка створки ниши шасси", "32").json()
        body = self.register_call(defect["code"]).json()
        self.assertEqual(body["required_mark"], "B1.1")

    def test_описание_необязательно_и_чистится_от_пробелов(self):
        body = self.register_call("hydraulic_leak", "  течь  у левой   стойки ").json()
        self.assertEqual(body["defect_description"], "течь у левой стойки")

        plain = self.register_call("brake_wear").json()
        self.assertIsNone(plain["defect_description"])

    def test_неизвестный_код_отклоняется(self):
        response = self.register_call("custom-deadbeef")
        self.assertEqual(response.status_code, 422, response.text)
        self.assertIn("заведите новый", response.json()["detail"])

    def test_описание_видит_инженер(self):
        call = self.register_call("hydraulic_leak", "Течь у левой основной стойки").json()
        suggestion = self.client.post(
            f"/api/calls/{call['id']}/suggest", headers=auth_header(self.dispatcher)
        ).json()
        best = suggestion["best"]
        self.assertIsNotNone(best, suggestion["message"])
        # Утечка на A320 у стоянки 31 в тестовых данных — сценарий Соколова:
        # у него единственного учётная запись PWA и допуск B1.1 на A320 рядом.
        self.assertEqual(best["full_name"], "Соколов А. В.")

        assigned = self.client.post(
            f"/api/calls/{call['id']}/assign",
            json={"employee_id": best["employee_id"]},
            headers=auth_header(self.dispatcher),
        )
        self.assertEqual(assigned.status_code, 200, assigned.text)

        engineer = self.login("sokolov", "engineer")
        current = self.client.get("/api/me/current-call", headers=auth_header(engineer)).json()
        self.assertEqual(current["call"]["id"], call["id"])
        self.assertEqual(current["call"]["defect_description"], "Течь у левой основной стойки")


class TestBuiltins(DefectApiCase):
    """Встроенные типы в базе, созданной до открытого справочника."""

    def test_встроенные_досоздаются_и_не_трогают_заведённые(self):
        custom = self.create_type("Обледенение датчика угла атаки", "30").json()

        db = SessionLocal()
        try:
            db.query(DefectType).filter(DefectType.is_builtin.is_(True)).delete()
            db.commit()
            self.assertEqual(ensure_builtin_defects(db), 12)
            # Повторный запуск ничего не добавляет.
            self.assertEqual(ensure_builtin_defects(db), 0)
            self.assertIsNotNone(
                db.query(DefectType).filter(DefectType.code == custom["code"]).one_or_none()
            )
        finally:
            db.close()

    def test_вызов_без_записи_в_справочнике_показывает_встроенное_название(self):
        call = self.register_call("hydraulic_leak").json()

        db = SessionLocal()
        try:
            db.query(DefectType).filter(DefectType.code == "hydraulic_leak").delete()
            db.commit()
        finally:
            db.close()

        calls = self.client.get("/api/calls", headers=auth_header(self.dispatcher)).json()
        listed = next(item for item in calls if item["id"] == call["id"])
        self.assertEqual(listed["defect_name"], "Утечка гидрожидкости")


class TestFreeTextCall(ApiTestCase):
    """Вызов можно зарегистрировать текстом — тип определится сам."""

    def setUp(self):
        self.reset_database()
        self.token = self.login("dispatcher", "dispatcher")

    def create(self, description, code=None):
        return self.client.post(
            "/api/calls",
            json={"aircraft_id": self.board_id(self.token, "VP-BZQ"), "defect_code": code,
                  "description": description},
            headers=auth_header(self.token),
        )

    def test_тип_по_словам_экипажа(self):
        response = self.create("течь у левой основной стойки шасси")
        self.assertEqual(response.status_code, 201, response.text)
        body = response.json()
        self.assertEqual(body["defect_name"], "Неисправность шасси")
        self.assertEqual(body["defect_description"], "течь у левой основной стойки шасси")

    def test_непонятное_уходит_без_классификации(self):
        response = self.create("что-то стучит при рулении")
        self.assertEqual(response.status_code, 201, response.text)
        body = response.json()
        self.assertEqual(body["defect_name"], "Неисправность без классификации")
        self.assertEqual(body["required_mark"][:2], "B1")
        # Служебный тип не попадает в справочник.
        listed = self.client.get("/api/defect-types", headers=auth_header(self.token)).json()
        self.assertNotIn("unclassified", [item["code"] for item in listed])

    def test_без_типа_и_без_описания_нельзя(self):
        self.assertEqual(self.create("").status_code, 422)
