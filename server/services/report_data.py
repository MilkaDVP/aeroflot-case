"""
Цифры отчёта за период. Только чтение.

Доля «в срок» считается по вызовам, где исход известен: инженер прибыл
или пятнадцать минут уже истекли. Отменённые в неё не входят. Время
назначения берётся из журнала событий: в самом вызове хранится
только последнее состояние.
"""

from collections import Counter, defaultdict
from datetime import timedelta
from statistics import mean

from clock import utc_now
from constants import REGULATION_ARRIVAL_LIMIT_MIN
from graph_registry import get_graph
from models.airport import Airport
from models.call import ACTIVE_CALL_STATUSES, STATUS_ARRIVED, STATUS_CANCELLED, STATUS_CLOSED, Call
from models.call_message import CallMessage
from models.event_log import EventLog
from services import shift_clock
from services.deadline import started_at
from services.journal import STATUS_TITLES

LIMIT = REGULATION_ARRIVAL_LIMIT_MIN
SLOW_ASSIGN_MIN = 5
STAGES = (("assign", "Ждал назначения"), ("travel", "Дорога"))


def minutes(start, end):
    return (end - start).total_seconds() / 60 if start and end else None


def average(values):
    known = [value for value in values if value is not None]
    return mean(known) if known else None


class Row:
    """Один вызов отчёта: моменты, исход и подписи."""

    def __init__(self, call, group, events, graph, now):
        self.call = call
        self.id = call.id
        self.start = started_at(call)
        self.board = call.aircraft.board_number if call.aircraft else next(
            (e.board_number for e in events if e.board_number), "—")
        node = graph.node(call.stand_node_id) if graph else None
        self.node = node
        self.stand = (node or {}).get("ref") or "—"
        self.defect = call.defect_type.name if call.defect_type else call.defect_code
        self.mark = call.required_mark
        self.engineer = call.assigned_employee.full_name if call.assigned_employee else (
            call.previous_employee.full_name if call.previous_employee else "—")
        self.arrived = min((c.first_arrived_at for c in group if c.first_arrived_at), default=None)
        self.closed = call.closed_at if call.status == STATUS_CLOSED else None
        self.assigned = next((e.ts for e in events if e.type in ("assigned", "reassigned")), None)
        self.cancelled = call.status == STATUS_CANCELLED
        self.active = call.status in ACTIVE_CALL_STATUSES
        self.status = STATUS_TITLES.get(call.status, call.status)
        self.arrival_min = minutes(self.start, self.arrived)
        overdue = now > self.start + timedelta(minutes=LIMIT)
        if self.cancelled:
            self.on_time = None
        elif self.arrival_min is not None:
            self.on_time = self.arrival_min <= LIMIT
        else:
            # Не прибыл: срок истёк — нарушение, нет — исход ещё неизвестен.
            self.on_time = False if overdue else None
        self.work_min = minutes(self.arrived, self.closed)
        self.parts = any(
            (e.params.get("этап работ") or {}).get("стало") == "waiting_parts"
            for e in events if e.type == "call_flags")
        self.unassigned = [e for e in events if e.type == "unassigned"]
        self.unassigned_count = len(self.unassigned)

    def stage_minutes(self):
        """Минуты по этапам до прибытия; None — этапа в журнале нет."""
        points = [self.start, self.assigned, self.arrived]
        return {
            key: (max(0, value) if (value := minutes(points[i], points[i + 1])) is not None else None)
            for i, (key, _) in enumerate(STAGES)
        }

    def main_delay(self):
        """Какой этап занял больше всего — для списка нарушений."""
        known = {k: v for k, v in self.stage_minutes().items() if v is not None}
        if not known:
            return "инженер не прибыл" if self.call.assigned_employee else "исполнитель не назначен"
        key = max(known, key=known.get)
        return f"{dict(STAGES)[key].lower()} {known[key]:.0f} мин"


def load(db, icao, start, end, now):
    calls = db.query(Call).filter(Call.airport_icao == icao).order_by(Call.created_at).all()
    roots = [c for c in calls if c.parent_call_id is None and start <= started_at(c) < end]
    ids = {c.id for c in roots}
    groups = defaultdict(list)
    for call in calls:
        root = call.parent_call_id or call.id
        if root in ids:
            groups[root].append(call)
    events = defaultdict(list)
    if ids:
        wanted = ("assigned", "reassigned", "unassigned", "status", "call_flags", "call_created")
        query = db.query(EventLog).filter(
            EventLog.airport_icao == icao, EventLog.type.in_(wanted), EventLog.ts >= start - timedelta(hours=1))
        for event in query.order_by(EventLog.id):
            if event.call_id in ids:
                events[event.call_id].append(event)
    try:
        graph = get_graph(icao)
    except Exception:  # граф аэропорта мог быть удалён
        graph = None
    return [Row(c, groups[c.id], events[c.id], graph, now) for c in roots], groups


def headline(rows):
    judged = [r for r in rows if r.on_time is not None]
    arrivals = [r.arrival_min for r in rows if r.arrival_min is not None and not r.cancelled]
    return {
        "total": len(rows),
        "closed": sum(r.call.status == STATUS_CLOSED for r in rows),
        "active": sum(r.active for r in rows),
        "cancelled": sum(r.cancelled for r in rows),
        "judged": len(judged),
        "on_time": sum(r.on_time for r in judged),
        "late": sum(not r.on_time for r in judged),
        "share": sum(r.on_time for r in judged) / len(judged) if judged else None,
        "avg": average(arrivals),
        "worst": max(arrivals, default=None),
        "aog": sum(bool(r.call.aog) for r in rows),
        "urgent": sum(bool(r.call.urgent) for r in rows),
    }


def outcome(row):
    if row.on_time is True:
        return "ok"
    return "late" if row.on_time is False else "other"


def timeline(rows, start, end, local):
    """Вызовы по часам (до полутора суток) или по дням."""
    hourly = end - start <= timedelta(hours=36)
    step = timedelta(hours=1) if hourly else timedelta(days=1)
    cursor = local(start).replace(minute=0, second=0, microsecond=0)
    if not hourly:
        cursor = cursor.replace(hour=0)
    buckets = []
    finish = local(end)
    while cursor < finish and len(buckets) < 100:
        label = cursor.strftime("%H" if hourly else "%d.%m")
        buckets.append({"label": label, "at": cursor, "ok": 0, "late": 0, "other": 0})
        cursor += step
    for row in rows:
        at = local(row.start)
        for bucket in reversed(buckets):
            if bucket["at"] <= at:
                bucket[outcome(row)] += 1
                break
    return buckets, hourly


def build(db, icao, start, end, with_people):
    now = utc_now()
    airport = db.get(Airport, icao)

    def local(moment):
        return shift_clock.local_now(airport, moment)

    rows, groups = load(db, icao, start, end, now)
    previous, _ = load(db, icao, start - (end - start), start, now)
    ids = [r.id for r in rows]
    counted = [r for r in rows if not r.cancelled]

    stages = {key: average(r.stage_minutes()[key] for r in counted) for key, _ in STAGES}
    heat = Counter()
    for row in rows:
        at = local(row.start)
        heat[(at.weekday(), at.hour)] += 1

    stands = defaultdict(lambda: {"count": 0, "late": 0, "node": None})
    defects = defaultdict(lambda: {"count": 0, "late": 0})
    boards = Counter()
    for row in counted:
        for store, key in ((stands, row.stand), (defects, row.defect)):
            store[key]["count"] += 1
            store[key]["late"] += row.on_time is False
        stands[row.stand]["node"] = row.node
        boards[(row.board, row.defect)] += 1

    problems = []
    if ids:
        problems = db.query(CallMessage).filter(CallMessage.call_id.in_(ids), CallMessage.is_problem.is_(True)).all()
    data = {
        "icao": icao, "airport": airport.name if airport else icao, "city": airport.city if airport else "",
        "start": local(start), "end": local(end), "made": local(now), "now": now,
        "head": headline(rows), "previous": headline(previous),
        "timeline": timeline(rows, start, end, local),
        "stages": stages, "heat": heat, "long": end - start > timedelta(days=3),
        "stands": sorted(stands.items(), key=lambda i: -i[1]["count"]),
        "defects": sorted(defects.items(), key=lambda i: -i[1]["count"]),
        "repeats": [(b, d, n) for (b, d), n in boards.most_common() if n > 1][:5],
        "violations": sorted((r for r in rows if r.on_time is False), key=lambda r: -(r.arrival_min or 1e9)),
        "open": [r for r in rows if r.active],
        "slow": sum((r.stage_minutes()["assign"] or 0) > SLOW_ASSIGN_MIN for r in counted),
        "overrides": sum(bool(r.call.override_reason) for r in rows),
        "mel": Counter(r.call.mel_category for r in rows if r.call.mel_category),
        "parts": sum(r.parts for r in rows),
        "tools": sum(bool(r.call.missing_tools) for r in rows),
        "problems": len(problems), "problems_open": sum(m.resolved_at is None for m in problems),
        "work": average(r.work_min for r in counted),
        "vehicle": sum(r.call.vehicle_id is not None for r in counted if r.assigned),
        "foot": sum(r.call.vehicle_id is None for r in counted if r.assigned),
        "reasons": Counter((e.params or {}).get("причина") or "без причины" for r in rows for e in r.unassigned),
        "local": local, "people": None,
    }
    if with_people:
        data["people"] = people(rows, groups, start, end, now)
        data["lanes"] = end - start <= timedelta(hours=36)
    return data


def people(rows, groups, start, end, now):
    """Работа инженеров, включая помощников."""
    by_root = {r.id: r for r in rows}
    result = defaultdict(lambda: {"calls": 0, "arrivals": [], "late": 0, "busy": 0.0, "spans": []})
    for root_id, group in groups.items():
        row = by_root[root_id]
        for call in group:
            person = call.assigned_employee
            if person is None or call.status == STATUS_CANCELLED:
                continue
            item = result[person.full_name]
            item["calls"] += 1
            if call.first_arrived_at and call.id == root_id:
                item["arrivals"].append(minutes(row.start, call.first_arrived_at))
                item["late"] += row.on_time is False
            begin = row.assigned if call.id == root_id else call.created_at
            finish = call.closed_at or (now if call.status in ACTIVE_CALL_STATUSES else None)
            if begin and finish and finish > begin:
                span = (max(begin, start), min(finish, end))
                if span[1] > span[0]:
                    item["busy"] += minutes(*span)
                    item["spans"].append((*span, call.first_arrived_at))
    return sorted(result.items(), key=lambda i: -i[1]["calls"])


def conclusions(data):
    """Выводы по правилам: ими пользуемся, когда GigaChat недоступен."""
    head = data["head"]
    if not head["total"]:
        return ["За период вызовов не было."]
    lines = []
    if head["share"] is not None:
        lines.append(f"В норматив уложились {head['on_time']} из {head['judged']} вызовов ({head['share']:.0%}).")
        old = data["previous"]["share"]
        if old is not None and abs(head["share"] - old) >= 0.05:
            lines.append(f"Это {'лучше' if head['share'] > old else 'хуже'} прошлого такого же периода: было {old:.0%}.")
    known = {k: v for k, v in data["stages"].items() if v}
    if known:
        key = max(known, key=known.get)
        lines.append(f"Больше всего времени до прибытия: {dict(STAGES)[key].lower()} — в среднем {known[key]:.1f} мин.")
    if data["slow"]:
        lines.append(f"Дольше {SLOW_ASSIGN_MIN} минут ждали назначения: {data['slow']}.")
    if data["open"]:
        lines.append(f"Следующей смене остаются незавершённые вызовы: {len(data['open'])}.")
    if head["judged"] < 10:
        lines.append("Вызовов немного: проценты могут сильно меняться от одного случая.")
    return lines


def lost_time(data):
    """Нарушения по этапу, на котором потеряно больше всего времени."""
    result = Counter()
    for row in data["violations"]:
        known = {k: v for k, v in row.stage_minutes().items() if v is not None}
        result[dict(STAGES)[max(known, key=known.get)] if known else row.main_delay()] += 1
    return result


def advice(data):
    """Что исправить — по правилам: (проблема, что сделать, чего ждать, код)."""
    head, lines = data["head"], []
    lost = lost_time(data)
    waited = lost[dict(STAGES)["assign"]] + lost["исполнитель не назначен"]
    if waited:
        lines.append((f"Нарушений из-за ожидания назначения: {waited}",
                      "Назначать сразу по подбору; если допущенных нет — сразу ставить в очередь или звать начальника смены",
                      "Меньше минут до выезда", "назначение"))
    if lost[dict(STAGES)["travel"]]:
        lines.append((f"Нарушений из-за дороги: {lost[dict(STAGES)['travel']]}",
                      "Держать инженеров и машины ближе к частым стоянкам, чаще отправлять на машине",
                      "Короче дорога до борта", "дорога"))
    hot = [(k, v) for k, v in data["stands"] if v["late"]]
    if hot:
        name, value = max(hot, key=lambda i: (i[1]["late"], i[1]["count"]))
        lines.append((f"Стоянка {name}: нарушений {value['late']} из {value['count']}",
                      "Поставить рядом дежурного инженера или машину в часы пик", "Меньше опозданий на этой стоянке", "стоянка"))
    if data["repeats"]:
        board, defect, count = data["repeats"][0]
        lines.append((f"{board}: «{defect}» повторилась {count} раза" if count < 5 else f"{board}: «{defect}» повторилась {count} раз",
                      "Найти причину повтора, а не только устранять проявление", "Меньше повторных вызовов", "повтор"))
    if data["tools"]:
        lines.append((f"Не хватало инструмента: {data['tools']}",
                      "Отмечать нужный инструмент при регистрации и проверять наборы в начале смены", "Без возвратов на склад", "инструмент"))
    if data["parts"]:
        lines.append((f"Ждали запчасти: {data['parts']}", "Пополнить запас по частым неисправностям", "Меньше простоя у борта", "запчасти"))
    if data["problems_open"]:
        lines.append((f"Неисправленных проблем от инженеров: {data['problems_open']}",
                      "Назначить ответственного за каждую и закрыть до конца смены", "Ничего не теряется при передаче", "проблемы"))
    if data["open"]:
        lines.append((f"Незавершённых вызовов: {len(data['open'])}",
                      "Передать следующей смене поимённо, просроченные — первыми", "Чистое начало смены", "передача"))
    if not lines and head["total"]:
        lines.append(("Заметных потерь времени нет", "Сохранить текущую расстановку людей и машин", "Доля в срок не ниже нынешней", "порядок"))
    return lines[:7]


def facts(data):
    """Цифры для пересказа GigaChat — без имён людей."""
    head = data["head"]
    return {
        "вызовов": head["total"], "в_срок": head["on_time"], "с_опозданием": head["late"],
        "отменено": head["cancelled"], "среднее_прибытие_мин": round(head["avg"], 1) if head["avg"] else None,
        "в_срок_прошлый_период": data["previous"]["on_time"], "с_опозданием_прошлый_период": data["previous"]["late"],
        "этапы_мин": {dict(STAGES)[k]: round(v, 1) for k, v in data["stages"].items() if v is not None},
        "ждали_назначения_дольше_5_мин": data["slow"],
        "стоянки": [{"стоянка": k, "вызовов": v["count"], "опозданий": v["late"]} for k, v in data["stands"][:5]],
        "неисправности": [{"тип": k, "вызовов": v["count"], "опозданий": v["late"]} for k, v in data["defects"][:5]],
        "снятия_исполнителя": dict(data["reasons"].most_common(5)),
        "нарушения_где_потеряно_время": dict(lost_time(data)),
        "повторы_на_борту": [{"борт": b, "тип": d, "раз": n} for b, d, n in data["repeats"]],
        "ехали_на_машине": data["vehicle"], "шли_пешком": data["foot"], "назначен_не_предложенный": data["overrides"],
        "проблем_не_исправлено": data["problems_open"],
        "ждали_запчасти": data["parts"], "не_хватало_инструмента": data["tools"], "незавершённых": len(data["open"]),
    }


__all__ = ["LIMIT", "STAGES", "STATUS_ARRIVED", "build", "conclusions", "facts", "load", "minutes"]
