"""
Аэропорт.

Графы бывают двух происхождений.

Встроенные (Шереметьево, Домодедово) лежат в файлах data/airports/<ICAO>.json,
закоммичены в репозиторий и загружаются в память при старте. Сервер с ними
в интернет не ходит: жюри разворачивает проект без доступа к сети.

Добавленные во время работы — выгруженные администратором по коду ИКАО
или нарисованные им по точкам — хранятся здесь же, в поле graph. Положить
их в файл нельзя: каталог с графами лежит в образе, и при пересоздании
контейнера такой аэропорт исчез бы. База живёт в томе и переживает
пересборку.

Граф хранится одним документом, а не таблицами узлов и рёбер: он всегда
читается и пишется целиком, в память, и частичные выборки по нему
не нужны.
"""

from sqlalchemy import JSON, String, text
from sqlalchemy.orm import Mapped, mapped_column

from database import Base

# Откуда взялся граф аэропорта.
SOURCE_BUILTIN = "builtin"
SOURCE_OSM = "osm"
SOURCE_MANUAL = "manual"


class Airport(Base):
    """Карточка аэропорта. Ключ — код ИКАО, он же используется в маршрутах API."""

    __tablename__ = "airports"

    icao: Mapped[str] = mapped_column(String, primary_key=True)
    name: Mapped[str] = mapped_column(String)
    city: Mapped[str] = mapped_column(String)

    # builtin — граф в файле репозитория; osm — выгружен по коду ИКАО;
    # manual — нарисован администратором на карте.
    # server_default нужен, чтобы колонку можно было добавить в базу,
    # созданную прежней версией: все аэропорты в ней — встроенные.
    source: Mapped[str] = mapped_column(
        String, default=SOURCE_BUILTIN, server_default=text(f"'{SOURCE_BUILTIN}'")
    )
    # Граф целиком для аэропортов, созданных во время работы. У встроенных
    # пусто: их граф читается из файла.
    graph: Mapped[dict | None] = mapped_column(JSON, nullable=True)
