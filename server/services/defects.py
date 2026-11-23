"""
Открытый справочник неисправностей: поиск, пополнение, встроенные типы.

Почему справочник открытый. Выпадающий список из дюжины пунктов не покрывает
того, что на перроне случается на самом деле, — эксперты Аэрофлота прямо
указали на это. Поэтому диспетчер ищет неисправность словами экипажа,
а если такой нет — заводит новый тип с главой ATA и категорией допуска.
По категории система дальше подбирает инженера, так что новый тип
сразу участвует в подборе наравне со встроенными.

Как устроен поиск — намеренно просто, чтобы его можно было объяснить.
Каждое слово запроса должно найтись в названии, синонимах или названии
главы ATA. У слов срезается окончание: «утечки» находит «утечку»,
«двигателя» — «двигатель». Число в запросе понимается как номер главы:
«32» — всё по шасси, включая встроенный «отказ запуска» с диапазоном 71–80
для запроса «72».
"""

import re
import uuid

from constants import ATA_CHAPTERS, DEFECT_CATEGORIES, DEFECT_TYPES
from models.defect_type import DefectType

SEARCH_LIMIT = 20
MIN_NAME_LENGTH = 3

# Окончания, которые срезаются у слов запроса и справочника. Длинные идут
# первыми: «-ами» должно срезаться целиком, а не одной «-и».
WORD_ENDINGS = (
    "ами", "ями", "ого", "его", "ому", "ему", "ыми", "ими",
    "ой", "ей", "ый", "ий", "ая", "яя", "ое", "ее", "ов", "ев",
    "ы", "и", "а", "я", "о", "е", "у", "ю", "ь",
)
# Короче этого основа не обрезается: «шина» не должна стать «шин» и найти
# «машину», а «ВСУ» — потерять последнюю букву.
MIN_STEM_LENGTH = 4

# «ATA 29», «ата 29», «гл. 29», «глава 29» — всё это просто номер главы.
CHAPTER_PREFIX = re.compile(r"\b(ata|ата|гл|глава)\b\.?", re.IGNORECASE)
TOKEN_SPLIT = re.compile(r"[^\w]+", re.UNICODE)

ERROR_SHORT_NAME = "Название неисправности слишком короткое: опишите её хотя бы парой слов"
ERROR_UNKNOWN_CHAPTER = "Глава ATA {ata} не описана в справочнике глав"
ERROR_BAD_CATEGORY = "Категория допуска должна быть одной из: A, B1, B2"


class DuplicateDefect(Exception):
    """Тип с таким названием уже есть — заводить второй незачем."""

    def __init__(self, existing):
        super().__init__(existing.name)
        self.existing = existing


class InvalidDefect(Exception):
    """Новый тип описан так, что по нему нельзя подобрать инженера."""


def normalize(text):
    """Нижний регистр и «ё» как «е»: экипаж пишет как придётся."""
    return (text or "").lower().replace("ё", "е")


def stem(word):
    """Грубая основа слова: срезается одно окончание из списка."""
    for ending in WORD_ENDINGS:
        if word.endswith(ending) and len(word) - len(ending) >= MIN_STEM_LENGTH:
            return word[: -len(ending)]
    return word


def query_tokens(query):
    """Слова запроса: основы слов и отдельно номера глав."""
    cleaned = CHAPTER_PREFIX.sub(" ", normalize(query))
    words = []
    chapters = []
    for token in TOKEN_SPLIT.split(cleaned):
        if not token:
            continue
        if token.isdigit():
            chapters.append(token)
        else:
            words.append(stem(token))
    return words, chapters


def chapter_title(ata):
    """
    Название главы ATA.

    У встроенного «отказа запуска» глава записана диапазоном «71-80» —
    берётся название первой главы диапазона.
    """
    first = (ata or "").split("-")[0].strip()
    entry = ATA_CHAPTERS.get(first)
    return entry[0] if entry else ""


def chapter_matches(ata, number):
    """Попадает ли номер главы из запроса в главу типа (в том числе в диапазон)."""
    parts = [part.strip() for part in (ata or "").split("-")]
    if len(parts) == 2 and parts[0].isdigit() and parts[1].isdigit():
        return int(parts[0]) <= int(number) <= int(parts[1])
    return parts[0] == number


def searchable_text(defect):
    """Всё, по чему тип может найтись: название, синонимы, глава."""
    return normalize(f"{defect.name} {defect.keywords} {chapter_title(defect.ata)}")


def score(defect, words, chapters):
    """
    Насколько тип подходит под запрос; None — не подходит совсем.

    Слово в названии весит больше, чем в синонимах или названии главы:
    при запросе «утечка» «Утечка гидрожидкости» должна стоять выше
    «Неисправности топливной системы», у которой «течь» лишь в синонимах.
    """
    for number in chapters:
        if not chapter_matches(defect.ata, number):
            return None

    name = normalize(defect.name)
    text = searchable_text(defect)
    total = 1
    for word in words:
        if word in name:
            total += 3
        elif word in text:
            total += 1
        else:
            return None

    if words and name.startswith(words[0]):
        total += 2
    return total


def search(db, query, limit=SEARCH_LIMIT):
    """Типы неисправностей под запрос, лучшие первыми; пустой запрос — все."""
    defects = db.query(DefectType).filter(DefectType.code != UNCLASSIFIED_CODE).all()
    words, chapters = query_tokens(query)

    if not words and not chapters:
        return sorted(defects, key=list_order)[:limit]

    ranked = []
    for defect in defects:
        value = score(defect, words, chapters)
        if value is not None:
            ranked.append((-value, list_order(defect), defect))
    ranked.sort(key=ranking_key)
    return [item[2] for item in ranked[:limit]]


UNCLASSIFIED_CODE = "unclassified"
CLASSIFY_WORD_MIN = 3


def classify(db, text):
    """
    Тип неисправности по свободному тексту экипажа.

    Поиск по справочнику требует всех слов запроса, а фраза «течь у левой
    основной стойки» так не найдёт ничего. Здесь каждое значимое слово
    голосует за типы, где оно встречается (в названии — весомее), номер
    главы ATA сужает выбор. Не подошло ничего — «без классификации»:
    вызов регистрируется, слова экипажа сохраняются в описании.
    """
    words, chapters = query_tokens(text)
    words = [word for word in words if len(word) >= CLASSIFY_WORD_MIN]
    best = None
    for defect in db.query(DefectType).all():
        if defect.code == UNCLASSIFIED_CODE:
            continue
        if chapters and not any(chapter_matches(defect.ata, number) for number in chapters):
            continue
        name = normalize(defect.name)
        rest = searchable_text(defect)
        total = sum(3 if word in name else 1 if word in rest else 0 for word in words)
        if chapters:
            total += 2
        if total > 0 and (best is None or (total, -list_order(defect)[0]) > (best[0], -list_order(best[1])[0])):
            best = (total, defect)
    if best is not None:
        return best[1]
    return unclassified(db)


def unclassified(db):
    """
    Служебный тип «без классификации» — создаётся при первой надобности.

    В справочник и поиск не выводится: выбрать его руками незачем, он
    только принимает вызовы, которые не подошли ни к одному типу. Допуск —
    B1: широкий механический, инженер разберётся на месте.
    """
    defect = find(db, UNCLASSIFIED_CODE)
    if defect is None:
        defect = DefectType(
            code=UNCLASSIFIED_CODE, name="Неисправность без классификации", ata="",
            category="B1", keywords="", is_builtin=True,
        )
        db.add(defect)
        db.flush()
    return defect


def list_order(defect):
    """Порядок без запроса: по главе ATA, внутри главы — по названию."""
    first = (defect.ata or "").split("-")[0]
    return (int(first) if first.isdigit() else 999, normalize(defect.name))


def ranking_key(item):
    """Сортировка результатов поиска: сначала оценка, затем обычный порядок."""
    return item[0], item[1]


def find(db, code):
    """Тип неисправности по коду или None."""
    return db.query(DefectType).filter(DefectType.code == code).one_or_none()


def create(db, name, ata, category=None, keywords="", user_id=None):
    """
    Заводит новый тип неисправности.

    Категория не указана — берётся та, что обычно требуется по главе ATA.
    Тип с тем же названием (без учёта регистра и «ё») уже есть — возвращать
    второй экземпляр нельзя: справочник быстро зарос бы дублями, и поиск
    выдавал бы одно и то же по нескольку раз.
    """
    title = " ".join((name or "").split())
    if len(title) < MIN_NAME_LENGTH:
        raise InvalidDefect(ERROR_SHORT_NAME)

    chapter = (ata or "").strip()
    if chapter not in ATA_CHAPTERS:
        raise InvalidDefect(ERROR_UNKNOWN_CHAPTER.format(ata=chapter or "—"))

    family = category or ATA_CHAPTERS[chapter][1]
    if family not in DEFECT_CATEGORIES:
        raise InvalidDefect(ERROR_BAD_CATEGORY)

    wanted = normalize(title)
    for existing in db.query(DefectType).all():
        if normalize(existing.name) == wanted:
            raise DuplicateDefect(existing)

    defect = DefectType(
        code=f"custom-{uuid.uuid4().hex[:8]}",
        name=title,
        ata=chapter,
        category=family,
        keywords=" ".join((keywords or "").split()),
        is_builtin=False,
        created_by=user_id,
    )
    db.add(defect)
    db.flush()
    return defect


def ensure_builtin_defects(db):
    """
    Досоздаёт встроенные типы и приводит их к описанию в constants.py.

    Вызывается при каждом запуске: база в Docker-томе переживает пересборку
    образа, и в старой базе справочника может не быть вовсе. Типы, заведённые
    людьми, не трогаются.
    """
    existing = {
        defect.code: defect
        for defect in db.query(DefectType).filter(DefectType.code.in_(list(DEFECT_TYPES)))
    }

    added = 0
    for code, description in DEFECT_TYPES.items():
        defect = existing.get(code)
        if defect is None:
            defect = DefectType(code=code, is_builtin=True)
            db.add(defect)
            added += 1
        defect.name = description["name"]
        defect.ata = description["ata"]
        defect.category = description["category"]
        defect.keywords = description.get("keywords", "")
        defect.is_builtin = True

    db.commit()
    return added
