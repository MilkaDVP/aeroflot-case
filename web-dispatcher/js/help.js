/*
 * Подсказки: словарь терминов, строка «что сейчас» и окно справки.
 *
 * На финале за экран сядет человек, который видит его впервые, и рядом
 * не будет никого, кто объяснит. Поэтому интерфейс объясняет себя сам:
 *   - термины (отметка, допуск, ATA, AOG) остаются как есть — эксперт
 *     должен видеть профессиональный язык, — но у каждого есть пояснение
 *     при наведении и строка в словаре;
 *   - строка под шапкой всегда говорит, что делать сейчас;
 *   - кнопка «Как это работает» открывает одну страницу: путь вызова,
 *     устройство экрана, цвета, звук, учения, словарь.
 *
 * Тексты собраны в одном месте: подсказка при наведении и строка словаря
 * берутся из одной записи и не могут разойтись.
 */

const TERMS = {
  regulation: {
    name: "Регламент 15 минут",
    text:
      "Инженер должен быть у борта не позже чем через 15 минут после сообщения экипажа. " +
      "Это условие договора: опоздание — штраф",
  },
  mark: {
    name: "Отметка (A, B1.1, B2)",
    text:
      "Квалификационная отметка из свидетельства специалиста: A — простые работы, " +
      "B1 — механика, B2 — авионика. Цифра после точки — подкатегория",
  },
  rating: {
    name: "Допуск на тип",
    text:
      "Право работать именно на этом типе самолёта. Одной отметки мало: " +
      "инженер с B1.1 без допуска на A330 к A330 не подходит",
  },
  ata: {
    name: "Глава ATA",
    text:
      "Международный номер системы самолёта: 29 — гидросистема, 32 — шасси. " +
      "По главе определяется, какая отметка нужна",
  },
  aog: {
    name: "AOG",
    text:
      "Aircraft On Ground — борт не может лететь. Ставит только инженер и только " +
      "после осмотра у борта; такой вызов стоит первым в списке",
  },
  vehicle: {
    name: "Машина (ТМ-04)",
    text:
      "Машина техпомощи из общего парка. «пешком → ТМ-04» значит: инженер дойдёт " +
      "до свободной машины и поедет на ней — так быстрее, чем пешком",
  },
  forecast: {
    name: "Прогноз прибытия",
    text:
      "Когда исполнитель будет у борта, если поедет с того места, где он сейчас. " +
      "Пересчитывается каждые несколько секунд",
  },
  queue: {
    name: "Очередь",
    text:
      "Вызов закреплён за занятым инженером: он приедет, когда закончит текущую работу. " +
      "Ставить в очередь вправе начальник смены",
  },
  override: {
    name: "Переопределение",
    text:
      "Начальник смены назначил не того, кого предложила система. Причина обязательна " +
      "и попадает в итог смены",
  },
  calc: {
    name: "Время подбора",
    text: "Сколько система искала исполнителя: проверка допусков и маршруты по перрону",
  },
};

/**
 * Атрибуты подсказки для термина: ` title="…" data-term="…"`.
 * Подставляется прямо в разметку рядом с классом элемента.
 */
function term(key) {
  const entry = TERMS[key];
  if (!entry) {
    return "";
  }
  return ` title="${escapeHtml(entry.name)}. ${escapeHtml(entry.text)}" data-term="${key}"`;
}

/* --- Строка «что сейчас» --- */

/**
 * Что диспетчеру делать прямо сейчас — одной фразой.
 *
 * Порядок проверок — это порядок срочности: сначала то, по чему уже идёт
 * время (сообщение экипажа ждёт разбора), затем тревоги, затем открытый
 * вызов, затем остальное. Строка не принимает решений — она показывает,
 * где следующее действие.
 *
 * @param {Object} context reports, calls, selectedCall, suggestion
 * @returns {{text: string, callId: (number|null)}}
 */
function nextStep(context) {
  const { reports, calls, selectedCall, suggestion } = context;

  if (reports.length) {
    return step(
      `Экипаж сообщил о неисправности на ${reports[0].board_number}. ` +
        "Нажмите «Разобрать» во входящих слева — срок уже идёт"
    );
  }

  const open = selectedCall && !selectedCall.assigned_employee_id ? selectedCall : null;
  if (open && suggestion && suggestion.best) {
    return step(
      `${open.board_number}: система предлагает отправить ${suggestion.best.full_name} — ` +
        "нажмите «Назначить» справа или выберите другого в списке"
    );
  }
  if (open && suggestion) {
    return step(
      `${open.board_number}: свободных с допуском нет. ` +
        (suggestion.busy.length
          ? "Поставьте вызов в очередь к занятому — список справа"
          : "Сообщите начальнику смены: нужен сотрудник с допуском")
    );
  }

  const alert = attentionFirst(calls).find(isAlertCall);
  if (alert && (!selectedCall || selectedCall.id !== alert.id)) {
    return step(
      `${alert.board_number} требует внимания: ${alert.control.reason}. Откройте вызов`,
      alert.id
    );
  }
  if (alert) {
    return step(`${alert.board_number}: ${alert.control.advice || alert.control.reason}`);
  }

  // Сначала тот вызов, по которому есть кого назначить: по нему
  // диспетчер может действовать прямо сейчас.
  const waiting = calls.find(canBeAssignedNow);
  if (waiting) {
    return step(
      `Вызов ${waiting.board_number} ждёт назначения — откройте его в списке слева`,
      waiting.id
    );
  }
  const stuck = calls.find(hasNoAssignee);
  if (stuck) {
    return step(
      `${stuck.board_number}: назначить некого — нет свободных с допуском. ` +
        "Откройте вызов: в нём видно, кто занят и когда освободится",
      stuck.id
    );
  }
  if (calls.length) {
    return step("Все вызовы у исполнителей. Следите за минутами до срока на карточках слева");
  }
  return step("Вызовов нет. Когда экипаж сообщит о неисправности, сообщение появится слева");
}

function hasNoAssignee(call) {
  return !call.assigned_employee_id && call.status !== "closed";
}

/** Исполнителя нет, и подбор ещё не говорил, что назначать некого. */
function canBeAssignedNow(call) {
  const nobody = call.status === "suggested" && !call.suggested_employee_id;
  return hasNoAssignee(call) && !nobody;
}

function step(text, callId) {
  return { text, callId: callId || null };
}

/**
 * Перерисовывает строку под шапкой.
 *
 * @param {Object} context данные для nextStep и признак учений (drill)
 * @param {Function} onOpenCall открыть вызов, о котором говорит строка
 */
function renderGuide(context, onOpenCall) {
  const next = nextStep(context);
  const text = document.getElementById("guide-text");
  text.textContent = next.text;
  text.classList.toggle("is-link", Boolean(next.callId));
  text.onclick = next.callId ? () => onOpenCall(next.callId) : null;

  document.getElementById("guide-drill").hidden = !context.drill;
}

/* --- Окно справки --- */

const HELP_STEPS = [
  ["Экипаж", "сообщает о неисправности с планшета или через систему авиакомпании. С этой минуты идёт срок"],
  ["Диспетчер", "разбирает сообщение: какой борт и что сломалось. Сообщение становится вызовом"],
  ["Система", "предлагает ближайшего свободного инженера с допуском и показывает маршрут"],
  ["Диспетчер", "назначает исполнителя. Начальник смены может выбрать другого — с причиной"],
  ["Инженер", "получает вызов в телефон, едет, отмечает прибытие, ведёт работы и закрывает вызов"],
];

const HELP_SCREEN = [
  ["Слева", "вызовы. Те, что требуют вмешательства, — красные и наверху; на каждом написано, что сделать"],
  ["В центре", "карта перрона: сотрудники, машины, борта и маршрут выбранного исполнителя. Щелчок по стоянке — новый вызов на этот борт"],
  ["Справа", "кого отправить на выбранный вызов и почему: время в пути, допуск, кто не подошёл"],
  ["Под шапкой", "строка «Что сейчас» — следующее действие одной фразой"],
];

const HELP_COLORS = [
  ["ok", "успевает, свободен и с допуском"],
  ["warn", "впритык: запас до срока меньше двух минут"],
  ["danger", "срок сорван или будет сорван — нужно вмешаться"],
  ["route", "маршрут и выбранный вызов"],
  ["busy", "сотрудник занят на вызове"],
  ["neutral", "свободен, но допуска на этот тип нет"],
];

function openHelp() {
  const backdrop = document.createElement("div");
  backdrop.className = "modal-backdrop";
  backdrop.innerHTML = `
    <div class="modal help">
      <h2>Как это работает</h2>

      <div class="section-title">Путь вызова</div>
      <ol class="help-steps">${HELP_STEPS.map(helpStep).join("")}</ol>
      <p class="help-note">${escapeHtml(TERMS.regulation.text)}.</p>

      <div class="section-title">Экран диспетчера</div>
      <dl class="help-list">${HELP_SCREEN.map(helpPair).join("")}</dl>

      <div class="section-title">Цвета</div>
      <div class="help-colors">${HELP_COLORS.map(helpColor).join("")}</div>

      <div class="section-title">Звук тревоги</div>
      <p class="help-note">${escapeHtml(SOUND_EXPLAIN)}</p>

      <div class="section-title">Учения</div>
      <p class="help-note">${escapeHtml(DRILL_EXPLAIN)} ${escapeHtml(DRILL_WHO)}</p>

      <div class="section-title">Словарь</div>
      <dl class="help-list">${Object.values(TERMS).map(helpTerm).join("")}</dl>

      <div class="modal-actions">
        <button id="help-close">Понятно</button>
      </div>
    </div>
  `;
  document.body.append(backdrop);
  backdrop.querySelector("#help-close").addEventListener("click", () => backdrop.remove());
  backdrop.addEventListener("click", (event) => {
    if (event.target === backdrop) {
      backdrop.remove();
    }
  });
}

function helpStep(item) {
  return `<li><b>${escapeHtml(item[0])}</b> ${escapeHtml(item[1])}</li>`;
}

function helpPair(item) {
  return `<dt>${escapeHtml(item[0])}</dt><dd>${escapeHtml(item[1])}</dd>`;
}

function helpTerm(entry) {
  return `<dt>${escapeHtml(entry.name)}</dt><dd>${escapeHtml(entry.text)}</dd>`;
}

function helpColor(item) {
  return (
    `<span class="help-color"><span class="legend-dot" style="background: var(--${item[0]})"></span>` +
    `${escapeHtml(item[1])}</span>`
  );
}

/* --- Учения: пояснения --- */

const DRILL_EXPLAIN =
  "Учения — показ без людей на перроне: сотрудники без телефона сами едут по маршруту " +
  "с расчётной скоростью, приём и прибытие отмечаются сами.";
const DRILL_WHO = "Включает и выключает учения администратор.";
const HOLD_EXPLAIN =
  "Имитация: инженер застрял в пути. Прогноз начнёт уходить, и вызов станет тревожным — " +
  "так проверяется, заметит ли это диспетчер.";
const HELD_EXPLAIN = "Исполнитель стоит. Смотрите на минуты до срока в карточке вызова слева.";
