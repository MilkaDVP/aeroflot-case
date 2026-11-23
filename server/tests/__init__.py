"""
Тесты серверной части.

Окружение выставляется здесь, при импорте пакета tests, — раньше любого
тестового модуля. Адрес базы database.py читает один раз, при первом
импорте, и если бы первым его подключил тест, запущенный отдельно
(pytest tests/test_x.py), тесты работали бы с рабочей базой стенда
server/aeroflot.db и пересоздали бы её.
"""

import os
import tempfile
from pathlib import Path

if "AEROFLOT_TEST_DB" not in os.environ:
    _path = Path(tempfile.mkdtemp(prefix="aeroflot-test-")) / "test.db"
    os.environ["AEROFLOT_TEST_DB"] = _path.as_posix()
os.environ["DATABASE_URL"] = f"sqlite:///{os.environ['AEROFLOT_TEST_DB']}"
# Фоновые такты в тестах выключены: движение, прогнозы и нарушения
# проверяются прямым вызовом с заданным временем.
os.environ["DRILL_LOOP"] = "0"
os.environ["JOURNAL_LOOP"] = "0"
# Тесты не обращаются к внешнему GigaChat, даже если ключ лежит в server/.env.
os.environ["GIGACHAT_ENABLED"] = "0"
