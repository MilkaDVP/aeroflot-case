"""
PDF отчёта: страницы А4 с инфографикой.

Цифры готовит report_data, здесь только вид. Цвета: синий — величина,
зелёный и красный — только «в срок» и «опоздал», всегда с подписью.
"""

import io
import math
from functools import partial
from pathlib import Path
from xml.sax.saxutils import escape

from reportlab.lib.colors import HexColor, white
from reportlab.lib.enums import TA_RIGHT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import (
    BaseDocTemplate, CondPageBreak, Flowable, Frame, KeepTogether, PageTemplate, Paragraph, Spacer, Table, TableStyle,
)

from graph_registry import get_graph
from services.report_data import LIMIT, STAGES

FONTS = Path(__file__).resolve().parent.parent / "assets" / "fonts"
pdfmetrics.registerFont(TTFont("PT", str(FONTS / "PT_Sans-Web-Regular.ttf")))
pdfmetrics.registerFont(TTFont("PT-Bold", str(FONTS / "PT_Sans-Web-Bold.ttf")))
pdfmetrics.registerFontFamily("PT", normal="PT", bold="PT-Bold", italic="PT", boldItalic="PT-Bold")

INK, INK2, MUTED = HexColor("#0b0b0b"), HexColor("#52514e"), HexColor("#898781")
GRID, CARD = HexColor("#e1e0d9"), HexColor("#f4f4f1")
BLUE, BLUE_DARK = HexColor("#2a78d6"), HexColor("#0d366b")
GOOD, BAD, OTHER = HexColor("#0ca30c"), HexColor("#d03b3b"), HexColor("#b4b2aa")
STAGE_COLORS = [HexColor("#2a78d6"), HexColor("#eb6834"), HexColor("#1baf7a")]
SEQUENTIAL = [HexColor(c) for c in ("#cde2fb", "#9ec5f4", "#6da7ec", "#3987e5", "#256abf", "#184f95", "#0d366b")]
WIDTH = A4[0] - 32 * mm
DAYS = ("Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс")

BODY = ParagraphStyle("body", fontName="PT", fontSize=9.5, leading=13.5, textColor=INK)
SMALL = ParagraphStyle("small", parent=BODY, fontSize=8, leading=10.5, textColor=INK2)
CELL = ParagraphStyle("cell", parent=BODY, fontSize=8, leading=10)
RIGHT = ParagraphStyle("right", parent=CELL, alignment=TA_RIGHT)
H2 = ParagraphStyle("h2", parent=BODY, fontName="PT-Bold", fontSize=13, leading=17, spaceBefore=12, spaceAfter=4)


def text(canvas, x, y, value, size=8, color=INK2, bold=False, align="left", limit=None):
    font = "PT-Bold" if bold else "PT"
    value = str(value)
    if limit:
        while len(value) > 1 and pdfmetrics.stringWidth(value, font, size) > limit:
            value = value[:-2].rstrip() + "…"
    canvas.setFont(font, size)
    canvas.setFillColor(color)
    {"left": canvas.drawString, "right": canvas.drawRightString, "center": canvas.drawCentredString}[align](x, y, value)


def number(value, digits=1):
    return "—" if value is None else f"{value:.{digits}f}".replace(".", ",")


def percent(value):
    return "—" if value is None else f"{value:.0%}"


class Drawn(Flowable):
    """Рисунок на всю ширину: draw(canvas, ширина, высота)."""

    def __init__(self, height, draw):
        super().__init__()
        self.width, self.height, self.paint = WIDTH, height, draw

    def wrap(self, *_):
        return self.width, self.height

    def draw(self):
        self.paint(self.canv, self.width, self.height)


def section(title, *parts, note=None):
    """Заголовок не отрывается от первого рисунка."""
    items = [Paragraph(title, H2)]
    if note:
        items.append(Paragraph(note, SMALL))
        items.append(Spacer(1, 3))
    return [KeepTogether([*items, *parts[:1]]), *parts[1:]]


def legend(canvas, x, y, items):
    for color, label in items:
        canvas.setFillColor(color)
        canvas.roundRect(x, y - 0.5, 7, 7, 1.5, stroke=0, fill=1)
        text(canvas, x + 10, y, label, 8)
        x += 22 + pdfmetrics.stringWidth(label, "PT", 8)


def bar(canvas, x, y, width, height, color):
    if width <= 0 or height <= 0:
        return
    canvas.setFillColor(color)
    canvas.roundRect(x, y, width, height, min(2, width / 2, height / 2), stroke=0, fill=1)


# --- Рисунки ---------------------------------------------------------------------


def cover(data, canvas, width, height):
    canvas.setFillColor(BLUE_DARK)
    canvas.roundRect(0, 0, width, height, 8, stroke=0, fill=1)
    text(canvas, 16, height - 28, "Отчёт по оперативному ТО", 20, white, True)
    text(canvas, 16, height - 46, f"{data['airport']} · {data['icao']}", 11, HexColor("#cde2fb"))
    text(canvas, 16, 14, f"{data['start']:%d.%m.%Y %H:%M} — {data['end']:%d.%m.%Y %H:%M}", 11, white, True)
    text(canvas, width - 16, 14, f"сформирован {data['made']:%d.%m.%Y %H:%M}", 8.5, HexColor("#cde2fb"), align="right")


def tiles(items, canvas, width, height):
    """Крупные цифры: (значение, подпись, пояснение, цвет пояснения)."""
    gap = 8
    size = (width - gap * (len(items) - 1)) / len(items)
    for index, (value, label, note, color) in enumerate(items):
        x = index * (size + gap)
        canvas.setFillColor(CARD)
        canvas.roundRect(x, 0, size, height, 6, stroke=0, fill=1)
        text(canvas, x + 10, height - 17, label, 8, INK2, limit=size - 16)
        text(canvas, x + 10, height - 41, value, 21, INK, True, limit=size - 16)
        text(canvas, x + 10, 9, note, 7.5, color or MUTED, limit=size - 16)


def ring(data, canvas, width, height):
    """Кольцо «в срок / опоздали» и полоса этапов рядом."""
    head = data["head"]
    radius, cx, cy = 46, 60, height / 2
    total = head["judged"]
    if total:
        share = head["on_time"] / total
        for start, extent, color in ((90, -360 * share, GOOD), (90 - 360 * share, -360 * (1 - share), BAD)):
            if abs(extent) < 0.5:
                continue
            # Промежуток между частями — фоном, не обводкой.
            pad = 1.5 if 0 < share < 1 else 0
            canvas.setStrokeColor(color)
            canvas.setLineWidth(13)
            path = canvas.beginPath()
            path.arc(cx - radius, cy - radius, cx + radius, cy + radius, start - pad, extent + 2 * pad)
            canvas.drawPath(path, stroke=1, fill=0)
    text(canvas, cx, cy - 3, percent(head["share"]), 20, INK, True, "center")
    text(canvas, cx, cy - 16, "в срок", 8, INK2, align="center")
    for index, (color, label, value) in enumerate(((GOOD, "✓ В срок", head["on_time"]), (BAD, "! Нарушение", head["late"]))):
        y = cy + 10 - index * 22
        canvas.setFillColor(color)
        canvas.roundRect(126, y - 1, 8, 8, 2, stroke=0, fill=1)
        text(canvas, 139, y, f"{label.split(' ', 1)[1]}: {value}", 9.5, INK, True)

    left = 240
    text(canvas, left, height - 12, data.get("stage_title") or "Из чего складывается путь до борта, в среднем, мин", 9, INK, True)
    values = [(label, data["stages"][key], color) for (key, label), color in zip(STAGES, STAGE_COLORS)]
    known = sum(v or 0 for _, v, _ in values)
    if not known:
        text(canvas, left, cy, "В журнале нет данных об этапах", 9, MUTED)
        return
    x, span = left, width - left
    for _, value, color in values:
        part = span * (value or 0) / known
        bar(canvas, x, height - 46, max(0, part - 2), 22, color)
        if part > 24:
            text(canvas, x + part / 2 - 1, height - 39, number(value), 9, white if color != STAGE_COLORS[2] else INK, True, "center")
        x += part
    for index, (label, value, color) in enumerate(values):
        y = height - 66 - index * 15
        canvas.setFillColor(color)
        canvas.roundRect(left, y - 0.5, 8, 8, 2, stroke=0, fill=1)
        text(canvas, left + 13, y, label, 9)
        text(canvas, width, y, f"{number(value)} мин", 9, INK, True, "right")


def columns(buckets, canvas, width, height):
    """Вызовы по времени: в срок, опоздали, прочие."""
    parts = (("ok", GOOD, "в срок"), ("late", BAD, "нарушение срока"), ("other", OTHER, "отменены или ещё идут"))
    legend(canvas, 0, height - 9, [(c, l) for _, c, l in parts])
    base, top = 16, height - 24
    peak = max((b["ok"] + b["late"] + b["other"] for b in buckets), default=0)
    if not peak:
        text(canvas, width / 2, height / 2, "Вызовов не было", 9, MUTED, align="center")
        return
    ticks = sorted({0, peak} | ({peak // 2} if peak > 3 else set()))
    for tick in ticks:
        y = base + (top - base) * tick / peak
        canvas.setStrokeColor(GRID)
        canvas.setLineWidth(0.6)
        canvas.line(18, y, width, y)
        text(canvas, 13, y - 3, tick, 7.5, MUTED, align="right")
    slot = (width - 22) / len(buckets)
    size = min(24, slot * 0.7)
    every = max(1, len(buckets) // 16)
    for index, bucket in enumerate(buckets):
        x = 22 + slot * index + (slot - size) / 2
        y = base
        for key, color, _ in parts:
            tall = (top - base) * bucket[key] / peak
            bar(canvas, x, y, size, tall - (1.5 if tall > 3 else 0), color)
            y += tall
        if index % every == 0:
            text(canvas, x + size / 2, 4, bucket["label"], 7.5, MUTED, align="center")


def heatmap(heat, canvas, width, height):
    peak = max(heat.values(), default=1)
    cell, row = (width - 30) / 24, (height - 30) / 7
    for day in range(7):
        y = height - 14 - row * (day + 1)
        text(canvas, 0, y + row / 2 - 3, DAYS[day], 8)
        for hour in range(24):
            count = heat.get((day, hour), 0)
            color = SEQUENTIAL[min(6, int(6 * count / peak))] if count else CARD
            x = 28 + cell * hour
            canvas.setFillColor(color)
            canvas.roundRect(x, y, cell - 2, row - 2, 2, stroke=0, fill=1)
            if count:
                dark = count / peak > 0.45
                text(canvas, x + cell / 2 - 1, y + row / 2 - 4, count, 7, white if dark else INK, align="center")
    for hour in range(0, 24, 3):
        text(canvas, 28 + cell * hour + cell / 2, height - 9, f"{hour:02d}", 7.5, MUTED, align="center")
    text(canvas, 28, 2, "Число в клетке — вызовы за этот час; чем темнее, тем больше", 7.5, MUTED)


def ranking(items, canvas, width, height):
    """Горизонтальные полосы: всего и из них с опозданием."""
    legend(canvas, 0, height - 9, [(BLUE, "без опоздания"), (BAD, "с опозданием")])
    peak = max(v["count"] for _, v in items)
    label, row = 190, 17
    for index, (name, value) in enumerate(items):
        y = height - 32 - index * row
        text(canvas, 0, y + 2, name, 8.5, INK, limit=label - 10)
        span = width - label - 100
        good = span * (value["count"] - value["late"]) / peak
        late = span * value["late"] / peak
        bar(canvas, label, y, good - (2 if late else 0), 9, BLUE)
        bar(canvas, label + good, y, late, 9, BAD)
        note = str(value["count"]) + (f", опозданий {value['late']}" if value["late"] else "")
        text(canvas, label + good + late + 6, y + 1.5, note, 8, INK2)


def fit_graph(graph, width, height):
    """Куда на рисунке попадают метры карты."""
    nodes = [n for n in graph.nodes.values() if n["type"] != "junction"] or list(graph.nodes.values())
    xs, ys = [n["x"] for n in nodes], [n["y"] for n in nodes]
    # Поля под круги, подписи и легенду внизу.
    inner_w, inner_h = width - 70, height - 80
    scale = min(inner_w / max(1, max(xs) - min(xs)), inner_h / max(1, max(ys) - min(ys)))
    x0, y0 = min(xs) - 35 / scale, min(ys) - 30 / scale
    w, h = max(xs) - x0 + 35 / scale, max(ys) - y0 + 50 / scale
    dx, dy = (width - w * scale) / 2, (height - h * scale) / 2
    return lambda x, y: (dx + (x - x0) * scale, height - dy - (y - y0) * scale)


def base_map(canvas, graph, place, width, height):
    canvas.setFillColor(CARD)
    canvas.roundRect(0, 0, width, height, 6, stroke=0, fill=1)
    clip = canvas.beginPath()
    clip.roundRect(0, 0, width, height, 6)
    canvas.clipPath(clip, stroke=0, fill=0)
    canvas.setStrokeColor(HexColor("#b4b2aa"))
    canvas.setLineWidth(0.7)
    for edge in graph.edges:
        a, b = graph.node(edge["from_id"]), graph.node(edge["to_id"])
        if a and b:
            polyline(canvas, [place(a["x"], a["y"]), *(place(*p) for p in edge.get("points") or []), place(b["x"], b["y"])])


def polyline(canvas, points):
    path = canvas.beginPath()
    path.moveTo(*points[0])
    for point in points[1:]:
        path.lineTo(*point)
    canvas.drawPath(path, stroke=1, fill=0)


CARD_W, CARD_H = 74, 30
MAP_CARDS = 14


def overlap(a, b, gap):
    """Насколько перекрываются две карточки с центрами a и b."""
    x, y = CARD_W + gap - abs(a[0] - b[0]), CARD_H + gap - abs(a[1] - b[1])
    return x * y if x > 0 and y > 0 else 0


def inside(point, box, gap):
    return abs(point[0] - box[0]) < CARD_W / 2 + gap and abs(point[1] - box[1]) < CARD_H / 2 + gap


def through(start, end, box):
    """Проходит ли линия через карточку."""
    return any(inside((start[0] + (end[0] - start[0]) * k / 24, start[1] + (end[1] - start[1]) * k / 24), box, 5)
               for k in range(25))


def crossing(a, b, c, d):
    def side(p, q, r):
        return (q[0] - p[0]) * (r[1] - p[1]) - (q[1] - p[1]) * (r[0] - p[0])
    return side(a, b, c) * side(a, b, d) < 0 and side(c, d, a) * side(c, d, b) < 0


def spread(anchors, width, height):
    """
    Места карточек: перебор положений вокруг своей точки. Штраф — за
    наложение на карточки и точки, за линии сквозь карточки и крест-накрест,
    за длину линии. Несколько проходов: первые карточки уступают поздним.
    """
    def places(anchor):
        for far in (34, 52, 74, 100, 130, 165):
            for turn in range(24):
                angle = turn * math.pi / 12
                x = anchor[0] + math.cos(angle) * (far + CARD_W / 2 - 15)
                y = anchor[1] + math.sin(angle) * far
                if CARD_W / 2 + 6 <= x <= width - CARD_W / 2 - 6 and CARD_H / 2 + 24 <= y <= height - CARD_H / 2 - 6:
                    yield x, y

    def cost(n, box, boxes):
        own = anchors[n]
        value = math.dist(own, box) * 0.4
        value += sum(400 for point in anchors if inside(point, box, 7))
        value += sum(120 for m, point in enumerate(anchors)
                     if m != n and near(point, own, box))
        for m, other in enumerate(boxes):
            if m == n or other is None:
                continue
            value += 600 * (overlap(box, other, 5) > 0) + overlap(box, other, 5)
            value += 250 * through(own, box, other) + 250 * through(anchors[m], other, box)
            value += 150 * crossing(own, box, anchors[m], other)
        return value

    boxes = [None] * len(anchors)
    options = [list(places(a)) or [(min(max(a[0], CARD_W / 2 + 6), width - CARD_W / 2 - 6),
                                    min(max(a[1], CARD_H / 2 + 24), height - CARD_H / 2 - 6))] for a in anchors]
    # Итог зависит от порядка: пробуем несколько и берём лучший.
    best, score = boxes, None
    count = len(anchors)
    for turn in range(min(6, count)):
        order = [(n * (turn + 1) + turn) % count for n in range(count)]
        order = list(dict.fromkeys(order + list(range(count))))
        boxes = [None] * count
        for _ in range(4):
            before = list(boxes)
            for n in order:
                boxes[n] = min(options[n], key=lambda box: cost(n, box, boxes))
            if boxes == before:
                break
        total = sum(cost(n, box, boxes) for n, box in enumerate(boxes))
        if score is None or total < score:
            best, score = boxes, total
    return best


def near(point, start, end):
    """Идёт ли линия вплотную к чужой точке."""
    length = math.dist(start, end) or 1
    k = ((point[0] - start[0]) * (end[0] - start[0]) + (point[1] - start[1]) * (end[1] - start[1])) / length ** 2
    k = min(1, max(0, k))
    return math.dist(point, (start[0] + (end[0] - start[0]) * k, start[1] + (end[1] - start[1]) * k)) < 6


def stand_map(icao, stands, canvas, width, height):
    graph = get_graph(icao)
    place = fit_graph(graph, width, height)
    canvas.saveState()
    base_map(canvas, graph, place, width, height)
    stands = stands[:MAP_CARDS]
    anchors = [place(v["node"]["x"], v["node"]["y"]) for _, v in stands]
    boxes = spread(anchors, width, height)
    colors = [BAD if v["late"] else BLUE for _, v in stands]
    canvas.setLineWidth(1)
    for (x, y), (cx, cy), color in zip(anchors, boxes, colors):
        canvas.setStrokeColor(color)
        canvas.line(x, y, cx, cy)
    for (x, y), color in zip(anchors, colors):
        canvas.setFillColor(color)
        canvas.setStrokeColor(white)
        canvas.setLineWidth(1.5)
        canvas.circle(x, y, 4.5, stroke=1, fill=1)
    for (name, value), (cx, cy), color in zip(stands, boxes, colors):
        left, bottom = cx - CARD_W / 2, cy - CARD_H / 2
        canvas.setFillColor(white)
        canvas.setStrokeColor(color)
        canvas.setLineWidth(1)
        canvas.roundRect(left, bottom, CARD_W, CARD_H, 4, stroke=1, fill=1)
        text(canvas, left + 6, bottom + 18, name, 9, INK, True, limit=CARD_W - 32)
        text(canvas, left + CARD_W - 6, bottom + 17, value["count"], 12, INK, True, "right")
        note = f"опозданий: {value['late']}" if value["late"] else "без опозданий"
        text(canvas, left + 6, bottom + 6, note, 7.5, BAD if value["late"] else INK2)
    canvas.restoreState()
    legend(canvas, 8, 8, [(BLUE, "без опозданий"), (BAD, "были опоздания")])


def lanes(data, canvas, width, height):
    """Лента занятости: дорога светлее, работа у борта темнее."""
    legend(canvas, 0, height - 9, [(SEQUENTIAL[2], "едет к борту"), (BLUE_DARK, "у борта")])
    start, end = data["start"], data["end"]
    total = (end - start).total_seconds()
    left, right = 130, width - 4
    bottom = 14

    def at(moment):
        return left + (right - left) * (data["local"](moment) - start).total_seconds() / total

    hours = int(total // 3600)
    for hour in range(0, hours + 1, max(1, hours // 8)):
        x = left + (right - left) * hour * 3600 / total
        canvas.setStrokeColor(GRID)
        canvas.setLineWidth(0.6)
        canvas.line(x, bottom, x, height - 18)
        text(canvas, x, 4, f"{(start.hour + hour) % 24:02d}:{start.minute:02d}", 7.5, MUTED, align="center")
    for index, (name, item) in enumerate(data["people"][:14]):
        y = height - 34 - index * 17
        text(canvas, 0, y + 2, name, 8.5, INK, limit=left - 10)
        for begin, finish, arrived in item["spans"]:
            split = min(max(arrived or finish, begin), finish)
            bar(canvas, at(begin), y, max(1.5, at(split) - at(begin) - 1), 9, SEQUENTIAL[2])
            bar(canvas, at(split), y, at(finish) - at(split), 9, BLUE_DARK)


def table(header, rows, widths, right=()):
    # Стрелки и пустые значения журнала: в шрифте отчёта стрелки нет.
    rows = [[str(v).replace(" → ", " — ").replace("None", "нет") for v in row] for row in rows]
    body = [[Paragraph(f"<b>{escape(h)}</b>", RIGHT if i in right else CELL) for i, h in enumerate(header)]]
    for row in rows:
        body.append([Paragraph(escape(str(v)), RIGHT if i in right else CELL) for i, v in enumerate(row)])
    result = Table(body, colWidths=[w * WIDTH for w in widths], repeatRows=1)
    result.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), CARD), ("LINEBELOW", (0, 0), (-1, -1), 0.5, GRID),
        ("VALIGN", (0, 0), (-1, -1), "TOP"), ("TOPPADDING", (0, 0), (-1, -1), 3.5), ("BOTTOMPADDING", (0, 0), (-1, -1), 3.5),
        ("LEFTPADDING", (0, 0), (-1, -1), 5), ("RIGHTPADDING", (0, 0), (-1, -1), 5),
    ]))
    return result


# --- Сборка -----------------------------------------------------------------------


def document(story, footer):
    buffer = io.BytesIO()

    def page(canvas, doc):
        text(canvas, 16 * mm, 9 * mm, footer, 7.5, MUTED)
        text(canvas, A4[0] - 16 * mm, 9 * mm, f"стр. {doc.page}", 7.5, MUTED, align="right")

    doc = BaseDocTemplate(buffer, pagesize=A4, title=footer, author="Диспетчеризация ОТО")
    frame = Frame(16 * mm, 14 * mm, WIDTH, A4[1] - 28 * mm, leftPadding=0, rightPadding=0, topPadding=0, bottomPadding=0)
    doc.addPageTemplates([PageTemplate(frames=[frame], onPage=page)])
    doc.build(story)
    return buffer.getvalue()


def delta(now, before, better_up, digits=1, suffix=""):
    """Сравнение с прошлым таким же периодом: слова, не только цвет."""
    if now is None or before is None:
        return "прошлый период: нет данных", MUTED
    change = now - before
    if abs(change) < 10 ** -digits / 2:
        return "как в прошлом периоде", MUTED
    good = (change > 0) == better_up
    sign = "+" if change > 0 else "−"
    return (f"{sign}{number(abs(change), digits)}{suffix} — {'лучше' if good else 'хуже'}",
            HexColor("#006300") if good else BAD)


def report(data, lines, steps=(), by_ai=False):
    head, before = data["head"], data["previous"]
    share_note = delta(head["share"] and head["share"] * 100, before["share"] and before["share"] * 100, True, 0, " п. п.")
    avg_note = delta(head["avg"], before["avg"], False, 1, " мин")
    story = [
        Drawn(84, partial(cover, data)), Spacer(1, 8),
        Drawn(66, partial(tiles, [
            (head["total"], "Вызовов", f"в прошлом периоде: {before['total']}", None),
            (percent(head["share"]), f"В срок ({LIMIT} мин)", *share_note),
            (f"{number(head['avg'])} мин", "Среднее прибытие", *avg_note),
            (f"{number(head['worst'])} мин", "Самое долгое", f"не прибыли в срок: {head['late']}", BAD if head["late"] else None),
        ])), Spacer(1, 8),
        Drawn(66, partial(tiles, [
            (head["closed"], "Выполнено", f"работа у борта: {number(data['work'], 0)} мин", None),
            (head["active"], "Не завершено", "перейдёт дальше" if head["active"] else "хвостов нет", None),
            (head["cancelled"], "Отменено", "в доле «в срок» не учтены", None),
            (head["aog"], "AOG", f"срочных: {head['urgent']}", BAD if head["aog"] else None),
        ])),
        *section("Главное", *[Paragraph(f"•&nbsp; {escape(line)}", BODY) for line in lines]),
        *section(f"Норматив прибытия — {LIMIT} минут", Drawn(116, partial(ring, data)),
                 note=f"По {head['judged']} вызовам с известным исходом: прибыли или срок истёк"),
        *section("Вызовы по часам" if data["timeline"][1] else "Вызовы по дням",
                 Drawn(130, partial(columns, data["timeline"][0]))),
    ]
    if data["long"] and data["heat"]:
        story += section("Когда вызовов больше", Drawn(160, partial(heatmap, data["heat"])))

    story += violations(data)
    story += places(data)
    if data["people"] is not None:
        story += people_part(data)
    story += quality(data)
    story += handover(data)
    story += improvements(steps, by_ai)
    return document(story, f"Отчёт ОТО · {data['icao']} · {data['start']:%d.%m.%Y %H:%M} — {data['end']:%d.%m.%Y %H:%M}")


def improvements(steps, by_ai):
    """Итог отчёта: что сделать, чтобы следующий период был лучше."""
    if not steps:
        return []
    return [CondPageBreak(140), *section("Что сделать, чтобы стало лучше", table(
        ("№", "Проблема", "Что сделать", "Что это даст"),
        [(index, *step[:3]) for index, step in enumerate(steps, 1)], (0.05, 0.3, 0.4, 0.25)),
        note=("Проблемы и цифры — из отчёта, рекомендации — GigaChat. " if by_ai else "Рекомендации по правилам системы. ")
        + "Решение за начальником смены")]


def violations(data):
    rows = data["violations"]
    if not rows:
        return section("Нарушения норматива", Paragraph("Нарушений за период нет.", BODY))
    shown = rows[:25]
    return section("Нарушения норматива", table(
        ("№", "Время", "Борт", "Стоянка", "Неисправность", "Инженер", "Прибытие", "Где потеряно время"),
        [(r.id, f"{data['local'](r.start):%d.%m %H:%M}", r.board, r.stand, r.defect,
          r.engineer if data["people"] is not None else "—",
          f"{number(r.arrival_min, 0)} мин" if r.arrival_min else "не прибыл", r.main_delay()) for r in shown],
        (0.06, 0.11, 0.11, 0.09, 0.2, 0.16, 0.1, 0.17)),
        note=f"Показаны {len(shown)} из {len(rows)}" if len(rows) > len(shown) else None)


def places(data):
    story = []
    if data["stands"]:
        top = [item for item in data["stands"] if item[1]["node"]]
        if top:
            note = "Крупная цифра на карточке — число вызовов, линия ведёт к стоянке"
            if len(top) > MAP_CARDS:
                note += f". Показаны {MAP_CARDS} самых частых из {len(top)}"
            story += section("Где вызывают чаще", Drawn(310, partial(stand_map, data["icao"], top)), note=note)
        story += section("Стоянки", Drawn(30 + 17 * min(8, len(data["stands"])), partial(ranking, data["stands"][:8])))
    if data["defects"]:
        story += section("Неисправности", Drawn(30 + 17 * min(8, len(data["defects"])), partial(ranking, data["defects"][:8])))
    if data["repeats"]:
        story += section("Повторы на одном борту", table(("Борт", "Неисправность", "Раз"), data["repeats"],
                                                        (0.25, 0.6, 0.15), right=(2,)))
    return story


def people_part(data):
    if not data["people"]:
        return section("Работа инженеров", Paragraph("Назначений за период нет.", BODY))
    span = (data["end"] - data["start"]).total_seconds() / 60
    parts = []
    if data["lanes"]:
        parts.append(Drawn(40 + 17 * min(14, len(data["people"])), partial(lanes, data)))
        parts.append(Spacer(1, 6))
    parts.append(table(
        ("Инженер", "Вызовов", "Среднее прибытие", "Опозданий", "Занят, мин", "Загрузка"),
        [(name, item["calls"],
          f"{number(sum(item['arrivals']) / len(item['arrivals']))} мин" if item["arrivals"] else "—",
          item["late"], number(item["busy"], 0), f"{min(1, item['busy'] / span):.0%}") for name, item in data["people"]],
        (0.34, 0.12, 0.18, 0.12, 0.12, 0.12), right=(1, 2, 3, 4, 5)))
    return [CondPageBreak(120), *section("Работа инженеров", *parts,
                                         note="Загрузка — доля всего периода, занятая вызовами. Прибытие считается у ответственного")]


def quality(data):
    rows = [
        ("Ждали назначения дольше 5 минут", data["slow"]),
        ("Назначен не тот, кого предложила система", data["overrides"]),
        ("Ехали на машине / шли пешком", f"{data['vehicle']} / {data['foot']}"),
        ("Ждали запчасти", data["parts"]), ("Не хватало инструмента", data["tools"]),
        ("Проблем от инженеров (не исправлено)", f"{data['problems']} ({data['problems_open']})"),
        ("Категории MEL", ", ".join(f"{k}: {v}" for k, v in sorted(data["mel"].items())) or "нет"),
    ]
    story = section("Ход работ", table(("Показатель", "Значение"), rows, (0.7, 0.3), right=(1,)))
    if data["reasons"]:
        story += section("Почему снимали исполнителя", table(("Причина", "Раз"), data["reasons"].most_common(8),
                                                            (0.85, 0.15), right=(1,)))
    return story


def handover(data):
    if not data["open"]:
        return section("Передача смены", Paragraph("Незавершённых вызовов нет.", BODY))
    return section("Передача смены: незавершённые вызовы", table(
        ("№", "С", "Борт", "Стоянка", "Неисправность", "Статус", "Инженер"),
        [(r.id, f"{data['local'](r.start):%d.%m %H:%M}", r.board, r.stand, r.defect, r.status,
          r.engineer if data["people"] is not None or r.assigned else "—") for r in data["open"]],
        (0.07, 0.12, 0.13, 0.1, 0.25, 0.16, 0.17)))


# --- Один вызов ---------------------------------------------------------------------


def route_map(icao, row, canvas, width, height):
    graph = get_graph(icao)
    place = fit_graph(graph, width, height)
    canvas.saveState()
    base_map(canvas, graph, place, width, height)
    nodes = [n for key in row.call.route_node_ids or [] if (n := graph.node(key))]
    bends = {}
    for edge in graph.edges:
        bends[(edge["from_id"], edge["to_id"])] = edge.get("points") or []
        bends[(edge["to_id"], edge["from_id"])] = (edge.get("points") or [])[::-1]
    if len(nodes) > 1:
        points = [place(nodes[0]["x"], nodes[0]["y"])]
        for a, b in zip(nodes, nodes[1:]):
            points += [place(*p) for p in bends.get((a["id"], b["id"]), [])] + [place(b["x"], b["y"])]
        canvas.setStrokeColor(BLUE)
        canvas.setLineWidth(2)
        polyline(canvas, points)
    if row.node:
        x, y = place(row.node["x"], row.node["y"])
        canvas.setFillColor(BAD)
        canvas.setStrokeColor(white)
        canvas.circle(x, y, 6, stroke=1, fill=1)
        text(canvas, x, y + 10, f"борт, ст. {row.stand}", 8, INK, True, "center")
    canvas.restoreState()


def call_card(row, events, messages, airport, local):
    stages = row.stage_minutes()
    data = {"head": {"judged": 0 if row.on_time is None else 1, "on_time": int(row.on_time is True),
                     "late": int(row.on_time is False), "share": None if row.on_time is None else float(row.on_time)},
            "stages": stages, "stage_title": "Из чего сложился путь до борта, мин"}
    verdict = {True: "в срок", False: "с опозданием", None: "исход не определён"}[row.on_time]

    def top(canvas, width, height):
        canvas.setFillColor(BLUE_DARK)
        canvas.roundRect(0, 0, width, height, 8, stroke=0, fill=1)
        text(canvas, 16, height - 28, f"Вызов №{row.id} · {row.board}", 20, white, True)
        text(canvas, 16, 14, f"{airport.name} · ст. {row.stand} · {row.defect}", 10.5, HexColor("#cde2fb"), limit=width - 32)

    story = [
        Drawn(64, top), Spacer(1, 8),
        Drawn(66, partial(tiles, [
            (f"{local(row.start):%H:%M}", "Начало отсчёта", f"{local(row.start):%d.%m.%Y}", None),
            (f"{number(row.arrival_min)} мин" if row.arrival_min is not None else "—", "Прибытие", verdict,
             BAD if row.on_time is False else None),
            (f"{number(row.work_min, 0)} мин" if row.work_min is not None else "—", "Работа у борта", row.status, None),
            (row.mark, "Нужная отметка", f"MEL {row.call.mel_category or '—'}{' · AOG' if row.call.aog else ''}", None),
        ])), Spacer(1, 4),
        Paragraph(f"Исполнитель: <b>{escape(row.engineer)}</b>"
                  + (f". Экипаж: «{escape(row.call.defect_description)}»" if row.call.defect_description else ""), BODY),
        *section("Время до прибытия", Drawn(116, partial(ring, data))),
    ]
    if row.node:
        story += section("Маршрут", Drawn(220, partial(route_map, row.call.airport_icao, row)),
                         note="Последний рассчитанный маршрут, не запись перемещения")
    if events:
        story += section("Хронология", table(
            ("Время", "Событие", "Кто"),
            [(f"{local(e.ts):%d.%m %H:%M:%S}", e.text, e.actor_name or "система") for e in events], (0.18, 0.6, 0.22)))
    if messages:
        story += section("Переписка", table(
            ("Время", "Автор", "Сообщение"),
            [(f"{local(m.created_at):%d.%m %H:%M}", m.author_name,
              m.text + ("" if not m.is_problem else " [проблема, исправлена]" if m.resolved_at else " [проблема]")) for m in messages],
            (0.15, 0.25, 0.6)))
    return document(story, f"Вызов №{row.id} · {row.board} · {row.call.airport_icao}")
