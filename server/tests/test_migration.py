"""
Тест досоздания колонок на старой базе.

База в Docker лежит в томе и переживает пересборку образа. Если в модели
появилась колонка, create_all её не добавит, и сервер упадёт на первом же
запросе у того, кто обновил проект, не удаляя том. Здесь база старой схемы
воспроизводится честно: создаётся полная схема, затем новые колонки
удаляются, и проверяется, что запуск их вернёт.
"""

import tempfile
import unittest
from pathlib import Path

from sqlalchemy import create_engine, inspect, text

import models  # noqa: F401  регистрация моделей в метаданных
from database import Base, add_missing_columns


class TestAddMissingColumns(unittest.TestCase):
    def setUp(self):
        directory = Path(tempfile.mkdtemp(prefix="aeroflot-migration-"))
        self.engine = create_engine(f"sqlite:///{(directory / 'old.db').as_posix()}")
        Base.metadata.create_all(bind=self.engine)
        with self.engine.begin() as connection:
            connection.execute(text("ALTER TABLE calls DROP COLUMN eta_at"))
            connection.execute(text("ALTER TABLE calls DROP COLUMN queued_at"))

    def tearDown(self):
        self.engine.dispose()

    def columns(self):
        return {column["name"] for column in inspect(self.engine).get_columns("calls")}

    def test_старая_база_получает_новые_колонки(self):
        self.assertNotIn("eta_at", self.columns())

        added = add_missing_columns(self.engine)

        self.assertEqual(sorted(added), ["calls.eta_at", "calls.queued_at"])
        self.assertIn("eta_at", self.columns())
        self.assertIn("queued_at", self.columns())

    def test_повторный_запуск_ничего_не_меняет(self):
        add_missing_columns(self.engine)
        self.assertEqual(add_missing_columns(self.engine), [])

    def test_колонка_not_null_с_умолчанием_добавляется(self):
        """
        airports.source появилась вместе с конструктором аэропортов.

        Она NOT NULL, но с умолчанием на стороне базы: в старой базе все
        аэропорты встроенные, и это значение верно для каждой строки.
        Без поддержки умолчания обновление останавливало бы запуск.
        """
        with self.engine.begin() as connection:
            connection.execute(text("ALTER TABLE airports DROP COLUMN source"))
            connection.execute(
                text("INSERT INTO airports (icao, name, city) VALUES ('AAAA', 'Т', 'Т')")
            )

        added = add_missing_columns(self.engine)
        self.assertIn("airports.source", added)

        with self.engine.begin() as connection:
            value = connection.execute(
                text("SELECT source FROM airports WHERE icao = 'AAAA'")
            ).scalar()
        self.assertEqual(value, "builtin")


if __name__ == "__main__":
    unittest.main()
