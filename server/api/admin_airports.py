"""
Аэропорты под управлением администратора: импорт по коду и ручная правка.

Два способа завести аэропорт. Импорт по коду ИКАО выгружает настоящий
граф из OpenStreetMap — сотни стоянок и рулёжек, как в Шереметьеве.
Пустой аэропорт администратор рисует сам, точку за точкой: так проверяют
поведение системы на понятной, маленькой схеме, где видно каждый маршрут.

Встроенные аэропорты (файлы в репозитории) через интерфейс не правятся:
их графы лежат в git и готовятся скриптом выгрузки. Иначе одно и то же
поле существовало бы в двух несогласованных версиях — в файле и в базе.
"""

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

import graph_registry
from auth.dependencies import Context, require_roles
from database import get_db
from models.airport import SOURCE_MANUAL, SOURCE_OSM, Airport
from models.aircraft import Aircraft
from models.call import Call
from models.employee import Employee
from models.user import ROLE_ADMIN, User
from models.vehicle import Vehicle
from schemas.airport import (
    AirportCreateRequest,
    AirportImportRequest,
    AirportSummary,
    GraphUpdateRequest,
)
from services.airport_builder import (
    GraphError,
    build_graph_payload,
    empty_payload,
    summarise,
)
from services.osm_import import import_airport

router = APIRouter(prefix="/api/admin/airports", tags=["Администрирование"])

ERROR_EXISTS = "Аэропорт {icao} уже заведён"
ERROR_NOT_FOUND = "Аэропорт {icao} не найден"
ERROR_BUILTIN = (
    "Аэропорт {icao} встроенный: его граф лежит в файле репозитория "
    "и правится скриптом выгрузки, а не через интерфейс"
)
ERROR_OSM_NOT_FOUND = (
    "Аэродром с кодом {icao} в OpenStreetMap не найден. Проверьте код ИКАО "
    "или создайте аэропорт вручную и нанесите точки на карту"
)
ERROR_OSM_EMPTY = (
    "Для {icao} в OpenStreetMap нет размеченных рулёжных дорожек. "
    "Такой аэропорт придётся нарисовать вручную"
)
ERROR_OSM_UNAVAILABLE = (
    "Не удалось обратиться к OpenStreetMap: {reason}. Проверьте доступ в интернет "
    "или создайте аэропорт вручную"
)
ERROR_IN_USE = (
    "В аэропорту {icao} есть данные: сотрудники, борта, машины или вызовы. "
    "Удалите их прежде, чем удалять аэропорт"
)


def load_airport(db, icao):
    """Аэропорт по коду или 404."""
    airport = db.get(Airport, icao.upper())
    if airport is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND, ERROR_NOT_FOUND.format(icao=icao.upper())
        )
    return airport


def ensure_editable(airport):
    """Встроенные аэропорты через интерфейс не меняются."""
    if airport.graph is None:
        raise HTTPException(
            status.HTTP_409_CONFLICT, ERROR_BUILTIN.format(icao=airport.icao)
        )


def grant_access(db, airport):
    """
    Открывает новый аэропорт администраторам.

    Иначе созданный аэропорт не появится в выборе даже у того, кто его
    завёл: доступ к аэропортам в системе явный, по учётной записи.
    """
    for user in db.query(User).filter(User.role == ROLE_ADMIN):
        if airport not in user.airports:
            user.airports.append(airport)


def to_summary(airport):
    """Карточка аэропорта со сводкой по графу."""
    if airport.graph is not None:
        counts = summarise(airport.graph)
        editable = True
    else:
        # Встроенный: считаем по загруженному в память графу.
        graph = graph_registry.get_graph(airport.icao)
        nodes = list(graph.nodes.values())
        counts = {
            "nodes": len(nodes),
            "stands": len([node for node in nodes if node["type"] == "stand"]),
            "tech_centers": len([node for node in nodes if node["type"] == "tech_center"]),
            "edges": len(graph.edges),
        }
        editable = False

    return AirportSummary(
        icao=airport.icao,
        name=airport.name,
        city=airport.city,
        source=airport.source,
        editable=editable,
        **counts,
    )


@router.get("", response_model=list[AirportSummary], summary="Все аэропорты системы")
def list_all_airports(
    context: Context = Depends(require_roles(ROLE_ADMIN)),
    db: Session = Depends(get_db),
):
    """Список для администратора: и встроенные, и заведённые им самим."""
    airports = db.query(Airport).order_by(Airport.icao).all()
    return [to_summary(airport) for airport in airports]


@router.post(
    "/import",
    response_model=AirportSummary,
    status_code=status.HTTP_201_CREATED,
    summary="Добавить аэропорт по коду ИКАО",
)
def import_from_osm(
    payload: AirportImportRequest,
    context: Context = Depends(require_roles(ROLE_ADMIN)),
    db: Session = Depends(get_db),
):
    """
    Выгружает настоящий граф аэропорта из OpenStreetMap по коду ИКАО.

    Это единственное место, где сервер обращается в интернет, и только
    по явной команде администратора. Вся остальная работа — подбор,
    маршруты, карта — идёт по графу в памяти.

    Границы лётного поля берутся из самого OSM по коду, поэтому координаты
    вводить не нужно. Обрывки рулёжек у краёв отбрасываются: маршрут
    на несвязанный кусок не построится, и это выглядело бы как отказ
    алгоритма.
    """
    icao = payload.icao.upper()
    if db.get(Airport, icao) is not None:
        raise HTTPException(status.HTTP_409_CONFLICT, ERROR_EXISTS.format(icao=icao))

    try:
        graph_payload, aerodrome = import_airport(icao, payload.service_roads)
    except RuntimeError as error:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            ERROR_OSM_UNAVAILABLE.format(reason=error),
        )

    if aerodrome is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND, ERROR_OSM_NOT_FOUND.format(icao=icao)
        )
    if graph_payload is None:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT, ERROR_OSM_EMPTY.format(icao=icao)
        )

    airport = Airport(
        icao=icao,
        name=aerodrome["name"],
        city=aerodrome["city"],
        source=SOURCE_OSM,
        graph=graph_payload,
    )
    db.add(airport)
    grant_access(db, airport)
    db.commit()

    graph_registry.register(graph_payload)
    return to_summary(airport)


@router.post(
    "",
    response_model=AirportSummary,
    status_code=status.HTTP_201_CREATED,
    summary="Создать пустой аэропорт для ручной разметки",
)
def create_airport(
    payload: AirportCreateRequest,
    context: Context = Depends(require_roles(ROLE_ADMIN)),
    db: Session = Depends(get_db),
):
    """
    Заводит аэропорт без точек — их администратор нанесёт на карту.

    Координаты нужны, чтобы редактор открыл карту над нужным участком
    земли, а не посреди океана.
    """
    icao = payload.icao.upper()
    if db.get(Airport, icao) is not None:
        raise HTTPException(status.HTTP_409_CONFLICT, ERROR_EXISTS.format(icao=icao))

    graph_payload = empty_payload(
        icao, payload.name, payload.city, payload.lat, payload.lon
    )
    airport = Airport(
        icao=icao,
        name=payload.name,
        city=payload.city,
        source=SOURCE_MANUAL,
        graph=graph_payload,
    )
    db.add(airport)
    grant_access(db, airport)
    db.commit()

    graph_registry.register(graph_payload)
    return to_summary(airport)


@router.put(
    "/{icao}/graph", response_model=AirportSummary, summary="Сохранить нарисованный граф"
)
def save_graph(
    icao: str,
    payload: GraphUpdateRequest,
    context: Context = Depends(require_roles(ROLE_ADMIN)),
    db: Session = Depends(get_db),
):
    """
    Записывает точки и связи, нанесённые администратором на карту.

    Граф проверяется до сохранения: длины считает сервер по координатам,
    связность обязательна. Несвязанный граф выглядит нормально, но вызов
    на отрезанную стоянку окажется невыполним — узнавать об этом в момент
    вызова к борту недопустимо.
    """
    airport = load_airport(db, icao)
    ensure_editable(airport)

    try:
        graph_payload = build_graph_payload(airport, payload.nodes, payload.edges)
    except GraphError as error:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(error))

    airport.graph = graph_payload
    db.commit()

    graph_registry.register(graph_payload)
    return to_summary(airport)


@router.delete(
    "/{icao}", status_code=status.HTTP_204_NO_CONTENT, summary="Удалить аэропорт"
)
def delete_airport(
    icao: str,
    context: Context = Depends(require_roles(ROLE_ADMIN)),
    db: Session = Depends(get_db),
):
    """
    Удаляет аэропорт, заведённый через интерфейс.

    Пока в нём есть сотрудники, борта, машины или вызовы, удаление
    запрещено: иначе остались бы записи, ссылающиеся на несуществующий
    аэропорт, и списки перестали бы открываться.
    """
    airport = load_airport(db, icao)
    ensure_editable(airport)

    if airport_has_data(db, airport.icao):
        raise HTTPException(
            status.HTTP_409_CONFLICT, ERROR_IN_USE.format(icao=airport.icao)
        )

    for user in db.query(User):
        if airport in user.airports:
            user.airports.remove(airport)

    db.delete(airport)
    db.commit()
    graph_registry.forget(airport.icao)


def airport_has_data(db, icao):
    """Есть ли в аэропорту записи, мешающие его удалить."""
    for model in (Employee, Aircraft, Vehicle, Call):
        if db.query(model).filter(model.airport_icao == icao).first() is not None:
            return True
    return False
