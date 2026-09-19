"""
Тесты проверки допуска.

Здесь проверяется не оптимизация, а законность назначения. Каждый тест
соответствует условию, нарушение которого делает назначение недопустимым:
не та категория, не тот тип ВС, истёкший срок действия отметки.
"""

import unittest
from datetime import date

from algorithm.qualification import (
    aircraft_group,
    find_qualification,
    format_mark,
    mark_satisfies,
    parse_mark,
    required_mark,
)
from constants import GROUP_TURBINE_AIRPLANE, GROUP_TURBINE_HELICOPTER

# Фиксированная дата проверки: тесты не должны меняться со временем.
TODAY = date(2026, 9, 3)


def employee_with(*qualifications):
    """Минимальный сотрудник — только то, что нужно проверке допуска."""
    return {
        "id": 1,
        "full_name": "Иванов И. И.",
        "qualifications": list(qualifications),
    }


def qualification(category, aircraft_types, valid_until="2027-12-31"):
    """Одна квалификационная отметка сотрудника."""
    return {
        "category": category,
        "aircraft_types": list(aircraft_types),
        "valid_until": valid_until,
    }


class TestMarkParsing(unittest.TestCase):
    """Разбор записи отметки из свидетельства."""

    def test_разбор_отметок_всех_семейств(self):
        self.assertEqual((parse_mark("A1").family, parse_mark("A1").group), ("A", 1))
        self.assertEqual((parse_mark("B1.3").family, parse_mark("B1.3").group), ("B1", 3))
        self.assertEqual((parse_mark("B2").family, parse_mark("B2").group), ("B2", None))
        self.assertEqual((parse_mark("C").family, parse_mark("C").group), ("C", None))

    def test_запись_отметки_восстанавливается(self):
        # Формат записи у семейств разный: A1, но B1.1 — проверяем оба.
        for text in ("A1", "A4", "B1.1", "B1.4", "B2", "C"):
            mark = parse_mark(text)
            self.assertEqual(format_mark(mark.family, mark.group), text)

    def test_мусор_не_разбирается(self):
        with self.assertRaises(ValueError):
            parse_mark("B3.7")


class TestRequiredMark(unittest.TestCase):
    """Какая отметка требуется под дефект и тип ВС."""

    def test_простой_дефект_требует_категорию_A(self):
        # Замена колеса на A320: категория A, первая группа ВС.
        self.assertEqual(str(required_mark("brake_wear", "A320")), "A1")

    def test_механический_дефект_требует_B1(self):
        self.assertEqual(str(required_mark("hydraulic_leak", "A320")), "B1.1")

    def test_авионика_требует_B2_без_подкатегории(self):
        # B2 по типу двигателя не дробится — отметка остаётся просто B2.
        self.assertEqual(str(required_mark("nav_system_error", "A320")), "B2")

    def test_подкатегория_зависит_от_типа_вс(self):
        # Та же утечка гидрожидкости, но на вертолёте — уже третья группа.
        self.assertEqual(str(required_mark("hydraulic_leak", "Ми-8")), "B1.3")

    def test_группы_типов_вс(self):
        self.assertEqual(aircraft_group("SSJ-100"), GROUP_TURBINE_AIRPLANE)
        self.assertEqual(aircraft_group("Ми-8"), GROUP_TURBINE_HELICOPTER)

    def test_неизвестный_тип_вс_это_ошибка(self):
        # Угадывать группу нельзя: молчаливо неверный подбор хуже отказа.
        with self.assertRaises(ValueError):
            required_mark("brake_wear", "Ан-124")

    def test_неизвестный_код_дефекта_это_ошибка(self):
        with self.assertRaises(ValueError):
            required_mark("нет_такого_дефекта", "A320")


class TestHierarchy(unittest.TestCase):
    """Иерархия допусков: что чем закрывается."""

    def test_B1_закрывает_работы_уровня_A(self):
        self.assertTrue(mark_satisfies(parse_mark("B1.1"), parse_mark("A1")))

    def test_A_не_закрывает_работы_уровня_B1(self):
        self.assertFalse(mark_satisfies(parse_mark("A1"), parse_mark("B1.1")))

    def test_другая_группа_вс_не_закрывает(self):
        # B1.2 — поршневые самолёты. На турбореактивный он не годится,
        # хотя семейство отметки то же самое.
        self.assertFalse(mark_satisfies(parse_mark("B1.2"), parse_mark("A1")))

    def test_авионик_не_закрывает_механику(self):
        # Осознанно консервативное решение, см. CATEGORY_SUBSTITUTIONS.
        self.assertFalse(mark_satisfies(parse_mark("B2"), parse_mark("A1")))
        self.assertFalse(mark_satisfies(parse_mark("B2"), parse_mark("B1.1")))

    def test_C_не_подменяет_линейные_категории(self):
        self.assertFalse(mark_satisfies(parse_mark("C"), parse_mark("B1.1")))


class TestFindQualification(unittest.TestCase):
    """Подбор отметки у конкретного сотрудника и объяснение отказа."""

    def test_подходящая_отметка_находится(self):
        employee = employee_with(qualification("B1.1", ["A320", "A321"]))
        found, reason = find_qualification(
            employee, required_mark("hydraulic_leak", "A320"), "A320", TODAY
        )
        self.assertIsNotNone(found)
        self.assertIsNone(reason)
        self.assertEqual(found["category"], "B1.1")

    def test_нужная_отметка_но_нет_допуска_на_тип(self):
        # Ключевой случай из ТЗ: инженер с B1.1 на A320 не имеет права
        # обслуживать B777, даже если он ближе всех и свободен.
        employee = employee_with(qualification("B1.1", ["A320"]))
        found, reason = find_qualification(
            employee, required_mark("hydraulic_leak", "B777"), "B777", TODAY
        )
        self.assertIsNone(found)
        self.assertIn("B777", reason)

    def test_истёкшая_отметка_не_годится(self):
        employee = employee_with(
            qualification("B1.1", ["A320"], valid_until="2026-01-15")
        )
        found, reason = find_qualification(
            employee, required_mark("hydraulic_leak", "A320"), "A320", TODAY
        )
        self.assertIsNone(found)
        self.assertIn("истекла", reason)

    def test_отметка_действует_в_последний_день(self):
        # Граница включительная: свидетельство действует до конца даты.
        employee = employee_with(
            qualification("B1.1", ["A320"], valid_until=TODAY.isoformat())
        )
        found, _ = find_qualification(
            employee, required_mark("hydraulic_leak", "A320"), "A320", TODAY
        )
        self.assertIsNotNone(found)

    def test_нет_подходящего_семейства(self):
        employee = employee_with(qualification("B2", ["A320"]))
        found, reason = find_qualification(
            employee, required_mark("hydraulic_leak", "A320"), "A320", TODAY
        )
        self.assertIsNone(found)
        self.assertIn("B1.1", reason)

    def test_вторая_отметка_спасает_когда_первая_не_подошла(self):
        # У сотрудника две отметки; годится вторая — она и должна найтись.
        employee = employee_with(
            qualification("B1.1", ["A320"]),
            qualification("B2", ["A320", "B777"]),
        )
        found, _ = find_qualification(
            employee, required_mark("nav_system_error", "B777"), "B777", TODAY
        )
        self.assertIsNotNone(found)
        self.assertEqual(found["category"], "B2")


if __name__ == "__main__":
    unittest.main()
