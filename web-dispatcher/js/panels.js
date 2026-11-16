/*
 * Отрисовка панелей: список вызовов и результат подбора.
 *
 * Модуль только строит разметку по данным и сообщает наверх о кликах.
 * Логики принятия решений здесь нет — она вся на сервере.
 */

/** Класс окраски времени в пути по трём зонам регламента. */
function timeClass(minutes) {
  if (minutes > CONFIG.regulationLimitMin) {
    return "is-danger";
  }
  if (minutes >= CONFIG.regulationWarnMin) {
    return "is-warn";
  }
  return "is-ok";
}

/**
 * Как кандидат доберётся: пешком, на своей машине или дойдя до свободной.
 *
 * «пешком → ТМ-04» — главная подсказка диспетчеру: без неё непонятно,
 * почему пеший сотрудник вдруг успевает к дальнему борту.
 */
function transportLabel(candidate) {
  if (candidate.pickup) {
    return `пешком → ${escapeHtml(candidate.vehicle_call_sign)}`;
  }
  if (candidate.vehicle_call_sign) {
    return escapeHtml(candidate.vehicle_call_sign);
  }
  return "пешком";
}

/**
 * Длительность в минутах словами: «14 мин», «3 ч 24 мин».
 * От часа — часами: «204 мин» приходится пересчитывать в уме.
 */
function durationText(minutes) {
  if (minutes < 60) {
    return `${minutes} мин`;
  }
  const rest = minutes % 60;
  return `${Math.floor(minutes / 60)} ч` + (rest ? ` ${rest} мин` : "");
}

/** Человекочитаемое время: минуты без лишней точности. */
function formatMinutes(minutes) {
  return minutes >= 10 ? Math.round(minutes) : minutes.toFixed(1);
}

function formatClock(isoString) {
  if (!isoString) {
    return "—";
  }
  const date = new Date(isoString);
  return date.toLocaleTimeString("ru-RU", { hour: "2-digit", minute: "2-digit" });
}

/* --- Список вызовов --- */

// Сколько длится подсветка нового вызова; совпадает с анимацией call-appear.
const FRESH_CALL_MS = 2500;

function renderCalls(container, calls, selectedId, onSelect) {
  container.replaceChildren();

  if (!calls.length) {
    container.innerHTML =
      '<div class="empty">Вызовов нет.<br>Новый появится здесь, когда экипаж сообщит о неисправности ' +
      "или вы зарегистрируете его кнопкой ниже.</div>";
    return;
  }

  for (const call of attentionFirst(calls)) {
    container.append(buildCallCard(call, selectedId, onSelect));
  }
  rememberShownCalls(calls);
}

/**
 * Запоминает показанные вызовы — через время подсветки, а не сразу:
 * иначе следующая же перерисовка (щелчок по вызову) оборвала бы её.
 */
function rememberShownCalls(calls) {
  const ids = calls.map((call) => call.id);
  if (shownCallIds === null) {
    shownCallIds = new Set(ids);
    return;
  }
  setTimeout(addShownCalls, FRESH_CALL_MS, ids);
}

function addShownCalls(ids) {
  for (const id of ids) {
    shownCallIds.add(id);
  }
}

/**
 * Тревожные вызовы — наверх, самые срочные первыми.
 *
 * Раньше тревога жила отдельной плашкой над списком и повторяла карточку
 * того же вызова: борт, стоянку и минуты диспетчер читал дважды и не
 * понимал, куда нажимать. Теперь тревога — это сама карточка: она поднята
 * наверх и окрашена, а остальные идут в порядке сервера.
 */
function attentionFirst(calls) {
  const alerts = calls.filter(isAlertCall).sort(compareByMinutesLeft);
  return alerts.concat(calls.filter((call) => !isAlertCall(call)));
}

// Вызовы, которые диспетчер уже видел в списке. null — список ещё не рисовался.
let shownCallIds = null;

/**
 * Вызов появился в списке только что.
 *
 * Список перерисовывается на каждом опросе, и подсветка «новый вызов»,
 * привязанная к статусу, вспыхивала каждые четыре секунды у всех
 * неназначенных. Теперь она срабатывает один раз — когда вызов впервые
 * попал в список; то, что было на экране при открытии, не мигает.
 */
function isFreshCall(call) {
  return shownCallIds !== null && !shownCallIds.has(call.id);
}

function isAlertCall(call) {
  return Boolean(call.control && call.control.alert);
}

/**
 * Карточка вызова. Каждый факт — один раз:
 *   строка 1 — борт и главная цифра (минуты до срока);
 *   строка 2 — где стоит и какой нужен допуск;
 *   строка 3 — что случилось;
 *   строка 4 — кто занимается и на каком он шаге;
 *   строка 5 — только у тревожных: причина и что сделать.
 */
function buildCallCard(call, selectedId, onSelect) {
  const item = document.createElement("div");
  item.className = "call";
  item.dataset.callId = call.id;
  if (isFreshCall(call)) {
    item.classList.add("is-fresh");
  }
  const controlCls = controlClass(call);
  if (controlCls) {
    item.classList.add(controlCls);
  }
  if (call.id === selectedId) {
    item.classList.add("selected");
  }

  item.innerHTML = `
    <div class="call-top">
      <span class="call-board">${escapeHtml(call.board_number)}</span>
      ${callFlags(call)}
      ${deadlineLabel(call)}
    </div>
    <div class="call-where">
      ${escapeHtml(call.aircraft_type)} · ст. ${escapeHtml(call.stand_ref || "—")} ·
      <span class="mark"${term("mark")}>${escapeHtml(call.required_mark)}</span> ·
      <span class="num" title="${
        call.reported_at ? "Экипаж сообщил в это время" : "Вызов зарегистрирован в это время"
      }. От него идут 15 минут">с ${formatClock(call.reported_at || call.created_at)}</span>
    </div>
    <div class="call-defect">${escapeHtml(call.defect_name)}</div>
    ${descriptionLine(call)}
    ${whoLine(call)}
    ${controlNote(call)}
  `;

  item.addEventListener("click", () => onSelect(call));
  return item;
}

/**
 * Сообщение экипажа своими словами, если диспетчер его записал.
 *
 * Тип неисправности нужен системе для подбора, а подробности — людям:
 * какая стойка, где течь, что на индикации.
 */
function descriptionLine(call) {
  if (!call.defect_description) {
    return "";
  }
  return `<div class="call-description">«${escapeHtml(call.defect_description)}»</div>`;
}

/**
 * Статус вызова словами, которые нельзя понять двояко.
 *
 * «Подобран» читалось как «назначен», хотя исполнителя ещё нет:
 * система лишь нашла кандидата и ждёт решения диспетчера.
 */
function statusTitle(status) {
  const titles = {
    new: "ждёт назначения",
    suggested: "ждёт назначения",
    queued: "в очереди",
    assigned: "ещё не принял вызов",
    accepted: "принял, едет",
    arrived: "у борта",
    closed: "закрыт",
  };
  return titles[status] || status;
}

/* --- Панель подбора --- */

/**
 * Результат подбора.
 *
 * Показывается целиком, включая отсеянных и занятых: диспетчер отвечает
 * за назначение лично и должен видеть, кого система от него скрыла
 * и на каком основании.
 */
function renderSuggestion(container, result, selectedEmployeeId, onPick, queueOptions) {
  container.replaceChildren();

  if (!result) {
    container.innerHTML = '<div class="empty">Выберите вызов в списке слева</div>';
    return;
  }

  container.append(buildEtaBlock(result));

  if (result.candidates.length) {
    container.append(
      sectionTitle(
        `Могут выехать сейчас (${result.candidates.length})`,
        "Свободные сотрудники с допуском на этот борт, первым — кто приедет быстрее. " +
          "Щелчок по строке покажет маршрут; назначить не первого вправе начальник смены"
      )
    );
    for (const candidate of result.candidates) {
      container.append(
        buildCandidateRow(candidate, result.best, selectedEmployeeId, onPick)
      );
    }
  }

  if (result.busy.length) {
    container.append(sectionTitle("Заняты, допуск есть", TERMS.queue.text));
    for (const busy of result.busy) {
      container.append(buildBusyRow(busy, queueOptions));
    }
  }

  if (result.rejected.length) {
    container.append(
      sectionTitle(
        `Не подходят (${result.rejected.length})`,
        "Система показывает и тех, кого отсеяла, с причиной: решение остаётся за диспетчером"
      )
    );
    for (const rejected of result.rejected) {
      container.append(buildRejectedRow(rejected));
    }
  }
}

/** Главный блок: крупное время прибытия предложенного кандидата. */
function buildEtaBlock(result) {
  const block = document.createElement("div");
  block.className = "eta-block";

  if (!result.best) {
    // Не прочерк на месте цифры, а слова: прочерк выглядел как поломка.
    block.innerHTML = `
      <div class="eta-nobody">Назначить некого</div>
      <div class="eta-message is-danger">${escapeHtml(result.message)}</div>
      <div class="eta-sub">Нужна отметка <span class="mark"${term("mark")}>${escapeHtml(
        result.required_mark
      )}</span> и <span${term("rating")}>допуск на тип</span></div>
      <div class="eta-next">${
        result.busy.length
          ? "Что можно сделать: поставить вызов в очередь к занятому — список ниже."
          : "Что можно сделать: сообщить начальнику смены — нужен сотрудник с допуском."
      }</div>
    `;
    return block;
  }

  const best = result.best;
  const cls = timeClass(best.minutes);

  block.innerHTML = `
    <div>
      <span class="eta-value ${cls}" title="Время в пути до борта">${formatMinutes(best.minutes)}</span
      ><span class="eta-unit">мин в пути</span>
    </div>
    <div class="eta-name">${escapeHtml(best.full_name)}</div>
    <div class="eta-why">будет у борта быстрее всех свободных с допуском</div>
    <div class="eta-sub">
      <span class="mark"${term("mark")}>${escapeHtml(best.mark)}</span>
      <span${best.vehicle_call_sign ? term("vehicle") : ""}>${transportLabel(best)}</span> ·
      <span class="num">${Math.round(best.route.distance_m)}</span> м
    </div>
    <div class="eta-message ${result.within_regulation ? "is-ok" : "is-danger"}">
      ${escapeHtml(result.message)}
    </div>
  `;
  return block;
}

function buildCandidateRow(candidate, best, selectedEmployeeId, onPick) {
  const row = document.createElement("div");
  row.className = "candidate";
  if (best && candidate.employee_id === best.employee_id) {
    row.classList.add("is-best");
  }
  if (candidate.employee_id === selectedEmployeeId) {
    row.classList.add("is-best");
  }

  row.innerHTML = `
    <span class="candidate-time ${timeClass(candidate.minutes)}">
      ${formatMinutes(candidate.minutes)}
    </span>
    <span class="candidate-body">
      <span class="candidate-name">${escapeHtml(candidate.full_name)}</span>
      <span class="candidate-sub">
        <span class="mark"${term("mark")}>${escapeHtml(candidate.mark)}</span>
        <span${candidate.vehicle_call_sign ? term("vehicle") : ""}>${transportLabel(candidate)}</span>
        · <span class="num">${Math.round(candidate.route.distance_m)}</span> м
      </span>
    </span>
  `;

  row.addEventListener("click", () => onPick(candidate));
  return row;
}

function buildRejectedRow(rejected) {
  const row = document.createElement("div");
  row.className = "rejected-row";
  row.innerHTML = `
    <b>${escapeHtml(rejected.full_name)}</b>
    <span class="rejected-reason">${escapeHtml(rejected.reason)}</span>
  `;
  return row;
}

/**
 * Строка занятого сотрудника с допуском.
 *
 * Кнопка «В очередь» делает этот список рабочим инструментом, а не
 * справкой: во внештатной ситуации §7 диспетчер видит, кто освободится
 * первым, и может сразу закрепить за ним вызов. Обычному диспетчеру
 * кнопка видна, но недоступна — с объяснением, почему: пропавшая кнопка
 * выглядела бы как ошибка интерфейса.
 */
function buildBusyRow(busy, queueOptions) {
  const row = document.createElement("div");
  row.className = "busy-row";
  row.innerHTML = `
    <b>${escapeHtml(busy.full_name)} <span class="mark">${escapeHtml(busy.mark)}</span></b>
    <span class="busy-until">до ${formatClock(busy.busy_until)}</span>
    ${busyStageLine(busy)}
  `;

  // Того, кто ждёт запчасти, можно переключить на этот вызов сразу:
  // это быстрее очереди и не вредит прежнему борту.
  if (queueOptions && busy.switchable) {
    const button = document.createElement("button");
    button.className = "queue-button switch-button";
    button.textContent = "Переключить";
    button.disabled = !queueOptions.allowed;
    button.title = queueOptions.allowed
      ? "Сотрудник ждёт запчасти: переключить его на этот вызов, к прежнему борту он вернётся"
      : queueOptions.denyReason;
    button.addEventListener("click", () => queueOptions.onSwitch(busy));
    row.append(button);
  }

  if (queueOptions) {
    const button = document.createElement("button");
    button.className = "queue-button";
    button.textContent = "В очередь";
    button.disabled = !queueOptions.allowed;
    button.title = queueOptions.allowed
      ? "Поставить вызов в очередь к этому сотруднику"
      : queueOptions.denyReason;
    button.addEventListener("click", () => queueOptions.onQueue(busy));
    row.append(button);
  }
  return row;
}

/**
 * Чем занят сотрудник: борт, этап работ и можно ли его переключить.
 * Начатый ремонт не прерывают — это видно до того, как диспетчер решит.
 */
function busyStageLine(busy) {
  if (!busy.current_board) {
    return "";
  }
  const cls = busy.switchable ? "is-ok" : "";
  return (
    `<span class="busy-stage ${cls}">${escapeHtml(busy.current_board)} · ` +
    `${escapeHtml(busy.switch_note || "")}</span>`
  );
}

/**
 * Отметки вызова: AOG и срочность.
 * AOG ставит только инженер после осмотра; срочность — диспетчер.
 */
function callFlags(call) {
  return (
    (call.aog ? `<span class="flag flag-aog"${term("aog")}>AOG</span>` : "") +
    (call.urgent ? '<span class="flag flag-urgent">срочный</span>' : "")
  );
}

/* --- Контроль регламента --- */

// Короткие подписи уровней контроля для отметки на вызове.
const CONTROL_LABELS = {
  ok: "успевает",
  warn: "впритык",
  late: "не успевает",
  overdue: "срок истёк",
  unassigned: "без исполнителя",
  no_signal: "нет сигнала",
  pending: "",
  paused: "приостановлен",
  arrived: "",
  closed: "",
};

/** Цвет вызова в списке по оценке сервера: красный — тревога, жёлтый — впритык. */
function controlClass(call) {
  if (!call.control) {
    return "";
  }
  if (call.control.alert) {
    return "is-alert";
  }
  return call.control.level === "warn" ? "is-warn" : "";
}

/**
 * Главная цифра карточки: сколько осталось до контрольного срока.
 *
 * Показывается у каждого незавершённого вызова, а не только у тревожных:
 * «ост. 11 мин» у нового вызова — то, что подгоняет назначение. После
 * прибытия отсчёт окончен, и на его месте — итог: в срок или с опозданием.
 */
function deadlineLabel(call) {
  const control = call.control;
  if (!control || control.level === "closed") {
    return "";
  }
  if (control.level === "paused") {
    // Срок прибытия по приостановленному вызову уже исполнен.
    return '<span class="call-deadline">приостановлен</span>';
  }
  if (control.level === "arrived") {
    return arrivalLabel(control);
  }
  return (
    `<span class="call-deadline num level-${escapeHtml(control.level)}">` +
    `${escapeHtml(minutesLeftText(control))}</span>`
  );
}

function arrivalLabel(control) {
  if (control.margin_min === null || control.margin_min === undefined) {
    return "";
  }
  if (control.margin_min >= 0) {
    return '<span class="call-deadline is-done">прибыл в срок</span>';
  }
  return (
    '<span class="call-deadline num level-late">опоздал на ' +
    `${escapeHtml(durationText(Math.ceil(-control.margin_min)))}</span>`
  );
}

/**
 * Причина и действие — только там, где нужно вмешаться или присмотреться.
 *
 * Причина сервера показывается, когда она сообщает новое: прогноз опоздания,
 * потерю сигнала. Для вызова без исполнителя и с истёкшим сроком она лишь
 * повторила бы статус и цифру, которые уже есть на карточке.
 */
const NOTE_WITH_REASON = ["late", "warn", "no_signal", "paused"];

function controlNote(call) {
  const control = call.control;
  if (!control) {
    return "";
  }
  const reason = NOTE_WITH_REASON.includes(control.level) ? control.reason : "";
  if (!reason && !control.advice) {
    return "";
  }
  return (
    `<div class="call-note level-${escapeHtml(control.level)}">` +
    (reason ? `<div>${escapeHtml(reason)}</div>` : "") +
    (control.advice ? `<div class="call-note-advice">${escapeHtml(control.advice)}</div>` : "") +
    `</div>`
  );
}

/**
 * Кто занимается вызовом и на каком он шаге — одной строкой.
 *
 * Для вызова в очереди показывается абсолютное время прибытия, а не
 * «через N минут»: исполнитель сначала закончит текущие работы, и главный
 * вопрос диспетчера здесь — насколько будет нарушен регламент.
 */
function whoLine(call) {
  if (!call.assigned_employee_name) {
    // Подбор уже был и никого не нашёл — это не «ждёт», а «некого».
    const nobody = call.status === "suggested" && !call.suggested_employee_id;
    return `<div class="call-who${nobody ? " is-danger" : ""}">${
      nobody ? "некого назначить" : "ждёт назначения"
    }</div>`;
  }
  const name = escapeHtml(call.assigned_employee_name);

  if (call.status === "queued") {
    return (
      `<div class="call-who is-queued">в очереди к <b>${name}</b>` +
      (call.eta_at ? ` · прибытие ~<span class="num">${formatClock(call.eta_at)}</span>` : "") +
      `</div>`
    );
  }
  if (call.status === "arrived") {
    return (
      `<div class="call-who"><b>${name}</b> у борта` +
      (call.work_stage_title ? ` · ${escapeHtml(call.work_stage_title)}` : "") +
      `</div>`
    );
  }

  const forecast = call.control && call.control.forecast_at;
  return (
    `<div class="call-who"><b>${name}</b> · ${escapeHtml(statusTitle(call.status))}` +
    (forecast ? ` · будет ~<span class="num">${formatClock(forecast)}</span>` : "") +
    `</div>`
  );
}

function sectionTitle(text, hint) {
  const element = document.createElement("div");
  element.className = "section-title";
  element.textContent = text;
  if (hint) {
    element.title = hint;
    element.dataset.term = "section";
  }
  return element;
}

/* --- Назначенный вызов --- */

/**
 * Карточка уже назначенного вызова.
 *
 * Для назначенного вызова новый подбор не показывается. Он предложил бы
 * другого сотрудника (назначенный уже занят) и мог бы кричать красным
 * о нарушении регламента по вызову, который уже обслуживается, —
 * ложная тревога. Диспетчеру здесь нужно другое: кто едет, когда будет
 * на месте и успевает ли к контрольному сроку.
 */
function renderAssignment(container, call, onUnassign) {
  container.replaceChildren();

  const control = call.control;
  const deadlineIso = control ? control.deadline_at
    : new Date(new Date(call.created_at).getTime() + CONFIG.regulationLimitMin * 60000).toISOString();
  const verdict = assignmentVerdict(call, control);
  // Главная цифра — живой прогноз прибытия, а не расчёт на момент
  // назначения: план мог устареть, пока инженер ехал.
  const forecast = control && control.forecast_at ? control.forecast_at : call.eta_at;

  const headline = call.status === "arrived"
    ? `<span class="eta-value is-ok">✓</span>`
    : `<span class="eta-value ${verdict.cls}">${formatClock(forecast)}</span>`;

  const block = document.createElement("div");
  block.className = "eta-block";
  block.innerHTML = `
    <div${call.status === "arrived" ? "" : term("forecast")}>${headline}</div>
    <div class="eta-name">${escapeHtml(call.assigned_employee_name || "—")}</div>
    <div class="eta-sub">${escapeHtml(call.defect_name)}${
      call.defect_ata ? ` · <span${term("ata")}>ATA ${escapeHtml(call.defect_ata)}</span>` : ""
    }</div>
    ${descriptionLine(call)}
    <div class="eta-sub">${escapeHtml(statusTitle(call.status))} · ${assignmentTiming(
      call,
      forecast,
      deadlineIso
    )}${
      call.vehicle_call_sign
        ? ` · <span class="num">${escapeHtml(call.vehicle_call_sign)}</span>`
        : ""
    }</div>
    <div class="eta-message ${verdict.cls}">${escapeHtml(verdict.text)}</div>
    ${
      control && control.advice
        ? `<div class="assignment-advice">${escapeHtml(control.advice)}</div>`
        : ""
    }
    ${
      call.override_reason
        ? `<div class="assignment-reason">Причина решения: ${escapeHtml(call.override_reason)}</div>`
        : ""
    }
    ${unassignedNote(call)}
  `;
  container.append(block);

  // Снятие доступно, пока работа не закрыта: обстановка на перроне меняется
  // быстрее, чем едет инженер.
  if (onUnassign) {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "unassign-button";
    button.textContent = "Снять исполнителя";
    button.addEventListener("click", onUnassign);
    container.append(button);
  }
}

/**
 * Отметка о прошлом исполнителе, если вызов уже переназначали.
 *
 * Диспетчер, принимающий смену, должен видеть, что борт ждал дважды:
 * без этого время ожидания выглядит меньше, чем было на самом деле.
 */
function unassignedNote(call) {
  if (!call.previous_employee_name) {
    return "";
  }
  return (
    `<div class="assignment-reason is-unassigned">Снят ${escapeHtml(
      call.previous_employee_name
    )}` +
    (call.unassigned_at ? ` в <span class="num">${formatClock(call.unassigned_at)}</span>` : "") +
    (call.unassign_reason ? `: ${escapeHtml(call.unassign_reason)}` : "") +
    `</div>`
  );
}

/**
 * Строка времени в карточке: прогноз, план и контрольный срок.
 *
 * План показывается, только если прогноз от него заметно ушёл: тогда
 * диспетчеру важно видеть, что инженер отстаёт от расчёта при назначении.
 */
function assignmentTiming(call, forecast, deadlineIso) {
  if (call.status === "arrived") {
    return `на месте с <span class="num">${formatClock(call.eta_at)}</span>`;
  }
  let text = `прогноз ~<span class="num">${formatClock(forecast)}</span>`;
  const delay = call.control ? call.control.delay_min : null;
  if (delay !== null && delay !== undefined && Math.abs(delay) >= 1 && call.eta_at) {
    text += ` · план ~<span class="num">${formatClock(call.eta_at)}</span>`;
  }
  return text + ` · срок <span class="num">${formatClock(deadlineIso)}</span>`;
}

/**
 * Цвет и формулировка оценки.
 *
 * Формулировку даёт сервер: он пересчитывает прогноз по текущим координатам
 * исполнителя и знает, можно ли им верить. Без оценки сервера — запасной
 * расчёт по времени, записанному при назначении.
 */
function assignmentVerdict(call, control) {
  if (control) {
    const classes = {
      ok: "is-ok",
      arrived: "is-ok",
      warn: "is-warn",
      late: "is-danger",
      overdue: "is-danger",
      no_signal: "is-danger",
      unassigned: "is-danger",
    };
    return { cls: classes[control.level] || "", text: control.reason };
  }
  if (call.status === "arrived") {
    return { cls: "is-ok", text: "Исполнитель на месте" };
  }
  if (!call.eta_at) {
    return { cls: "", text: "Время прибытия не рассчитано" };
  }
  const deadline = new Date(call.created_at).getTime() + CONFIG.regulationLimitMin * 60000;
  if (new Date(call.eta_at).getTime() <= deadline) {
    return { cls: "is-ok", text: "Успевает к контрольному сроку" };
  }
  return { cls: "is-danger", text: "Прибытие позже контрольного срока" };
}

/* --- Строка состояния --- */

function renderShiftStats(employees, vehicles) {
  const free = employees.filter((item) => item.status === "free").length;
  const busy = employees.filter((item) =>
    ["assigned", "en_route", "busy"].includes(item.status)
  ).length;
  const freeVehicles = (vehicles || []).filter((item) => item.status === "free").length;

  document.getElementById("stat-total").textContent = employees.length;
  document.getElementById("stat-free").textContent = free;
  document.getElementById("stat-busy").textContent = busy;
  document.getElementById("stat-vehicle").textContent = freeVehicles;
}

/**
 * Экранирование текста, приходящего с сервера.
 *
 * ФИО и причины отказа подставляются в разметку, а значит любое имя
 * с угловой скобкой сломало бы страницу. Отдельная функция вместо
 * textContent — потому что строки собираются из шаблонов.
 */
function escapeHtml(value) {
  if (value === null || value === undefined) {
    return "";
  }
  return String(value)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;");
}
