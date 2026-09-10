"""
Проверка допуска сотрудника к работам на конкретном воздушном судне.

Это первый и главный фильтр подбора. Он не про оптимизацию, а про закон:
сотрудник без действующей отметки нужной категории и без допуска на данный
тип ВС не имеет права выполнять работы, каким бы близким он ни был.
Система, предлагающая такое назначение, хуже, чем отсутствие системы.

Проверка состоит из трёх независимых условий:
  1. категория отметки подходит под вид работ (с учётом иерархии допусков);
  2. тип ВС входит в список допусков этой отметки (type rating);
  3. срок действия отметки не истёк на дату вызова.
"""

from datetime import date

from constants import (
    AIRCRAFT_GROUP_BY_TYPE,
    CATEGORY_SUBSTITUTIONS,
    DEFECT_TYPES,
    SPLIT_BY_AIRCRAFT_GROUP,
)

# Причины отказа в допуске. Возвращаются диспетчеру как есть: он должен
# видеть не «не подходит», а конкретное основание.
REASON_NO_CATEGORY = "нет отметки {mark}"
REASON_NO_TYPE_RATING = "отметка {mark} есть, но без допуска на {aircraft_type}"
REASON_EXPIRED = "отметка {mark} на {aircraft_type} истекла {valid_until}"


class Mark:
    """
    Квалификационная отметка, разобранная на семейство и подкатегорию.

    «B1.1» — это семейство B1 и первая группа ВС (самолёты с ГТД).
    «B2» — семейство B2 без деления по типу ВС, группа отсутствует.
    """

    def __init__(self, family, group):
        self.family = family
        self.group = group

    def __str__(self):
        return format_mark(self.family, self.group)

    def __eq__(self, other):
        return self.family == other.family and self.group == other.group


def parse_mark(text):
    """
    Разбирает запись отметки из свидетельства: «A1», «B1.3», «B2», «C».

    Формат записи отличается у семейств A и B1 — «A1», но «B1.1», — поэтому
    разбор идёт по правилам ФАП, а не одним регулярным выражением.
    """
    value = text.strip()

    if value.startswith("B1."):
        return Mark("B1", int(value[3:]))
    if value.startswith("A") and len(value) > 1:
        return Mark("A", int(value[1:]))
    if value in ("B2", "C", "A", "B1"):
        return Mark(value, None)

    raise ValueError(f"не удаётся разобрать квалификационную отметку: {text!r}")


def format_mark(family, group):
    """Обратное преобразование: семейство и группа в запись свидетельства."""
    if group is None:
        return family
    if family == "B1":
        return f"B1.{group}"
    return f"{family}{group}"


def aircraft_group(aircraft_type):
    """
    Подкатегория по типу ВС: 1 — самолёт с ГТД, 3 — вертолёт с ГТД и т. д.

    Неизвестный тип — не повод угадать. Лучше явная ошибка на этапе
    создания вызова, чем молчаливо неверный подбор.
    """
    if aircraft_type not in AIRCRAFT_GROUP_BY_TYPE:
        raise ValueError(
            f"тип ВС {aircraft_type!r} не описан в справочнике AIRCRAFT_GROUP_BY_TYPE"
        )
    return AIRCRAFT_GROUP_BY_TYPE[aircraft_type]


def required_mark(defect_code, aircraft_type):
    """
    Какая отметка нужна для устранения дефекта на этом типе ВС.

    Складывается из двух измерений: вид работ даёт семейство (A / B1 / B2),
    тип ВС даёт подкатегорию. Для B2 и C подкатегории нет.
    """
    if defect_code not in DEFECT_TYPES:
        raise ValueError(f"код дефекта {defect_code!r} не описан в справочнике")

    family = DEFECT_TYPES[defect_code]["category"]
    if family in SPLIT_BY_AIRCRAFT_GROUP:
        return Mark(family, aircraft_group(aircraft_type))
    return Mark(family, None)


def mark_satisfies(mark, required):
    """
    Закрывает ли отметка сотрудника требуемую по иерархии допусков.

    Семейство должно входить в список замен для требуемого (B1 закрывает A),
    а подкатегория — совпадать: допуск на самолёты не даёт права работать
    на вертолётах, даже в пределах одного семейства.
    """
    if mark.family not in CATEGORY_SUBSTITUTIONS[required.family]:
        return False
    if required.group is None:
        return True
    return mark.group == required.group


def qualification_is_valid(qualification, on_date):
    """Не истёк ли срок действия отметки на дату вызова."""
    valid_until = qualification.get("valid_until")
    if not valid_until:
        return False
    return date.fromisoformat(valid_until) >= on_date


def find_qualification(employee, required, aircraft_type, on_date):
    """
    Ищет у сотрудника отметку, дающую право на эти работы.

    Возвращает пару (отметка, причина отказа). Отметка не None — допуск есть.
    Причина заполняется, когда допуска нет, и объясняет ровно, чего не хватило:
    отметки, допуска на тип ВС или действующего срока. Из нескольких неудач
    выбирается самая «близкая» — так диспетчер видит, что сотрудник почти
    подходил, а не просто «не подходит».
    """
    best_failure = None
    best_rank = -1

    for qualification in employee.get("qualifications", []):
        mark = parse_mark(qualification["category"])

        if not mark_satisfies(mark, required):
            continue

        # Отметка подходящая — дальше проверяем допуск на тип ВС и срок.
        if aircraft_type not in qualification.get("aircraft_types", []):
            if best_rank < 1:
                best_failure = REASON_NO_TYPE_RATING.format(
                    mark=mark, aircraft_type=aircraft_type
                )
                best_rank = 1
            continue

        if not qualification_is_valid(qualification, on_date):
            if best_rank < 2:
                best_failure = REASON_EXPIRED.format(
                    mark=mark,
                    aircraft_type=aircraft_type,
                    valid_until=qualification.get("valid_until", "—"),
                )
                best_rank = 2
            continue

        return qualification, None

    if best_failure is None:
        best_failure = REASON_NO_CATEGORY.format(mark=required)

    return None, best_failure
