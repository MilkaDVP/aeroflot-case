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

function renderCalls(container, calls, selectedId, onSelect) {
  container.replaceChildren();

  if (!calls.length) {
    container.innerHTML = '<div class="empty">Активных вызовов нет</div>';
    return;
  }

  for (const call of calls) {
    const item = document.createElement("div");
    item.className = "call";
    item.classList.add(callStateClass(call));
    if (call.id === selectedId) {
      item.classList.add("selected");
    }

    const assignee = assigneeLine(call);

    item.innerHTML = `
      <div class="call-top">
        <span class="call-board">${escapeHtml(call.board_number)}</span>
        <span class="call-stand">${escapeHtml(call.aircraft_type)} · ст. ${escapeHtml(
          call.stand_ref || "—"
        )}</span>
      </div>
      <div class="call-defect">${escapeHtml(call.defect_name)}</div>
      <div class="call-meta">
        <span class="mark">${escapeHtml(call.required_mark)}</span>
        <span class="num">${formatClock(call.created_at)}</span>
        <span>${escapeHtml(statusTitle(call.status))}</span>
      </div>
      ${assignee}
    `;

    item.addEventListener("click", () => onSelect(call));
    container.append(item);
  }
}

function statusTitle(status) {
  const titles = {
    new: "новый",
    suggested: "подобран",
    queued: "в очереди",
    assigned: "назначен",
    accepted: "принят",
    arrived: "прибыл",
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
    container.append(sectionTitle(`Кандидаты (${result.candidates.length})`));
    for (const candidate of result.candidates) {
      container.append(
        buildCandidateRow(candidate, result.best, selectedEmployeeId, onPick)
      );
    }
  }

  if (result.busy.length) {
    container.append(sectionTitle("Заняты, допуск есть"));
    for (const busy of result.busy) {
      container.append(buildBusyRow(busy, queueOptions));
    }
  }

  if (result.rejected.length) {
    container.append(sectionTitle(`Не подходят (${result.rejected.length})`));
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
    block.innerHTML = `
      <div class="eta-value is-danger">—</div>
      <div class="eta-message is-danger">${escapeHtml(result.message)}</div>
      <div class="eta-sub">Требуется отметка <span class="mark">${escapeHtml(
        result.required_mark
      )}</span></div>
    `;
    return block;
  }

  const best = result.best;
  const cls = timeClass(best.minutes);

  block.innerHTML = `
    <div>
      <span class="eta-value ${cls}">${formatMinutes(best.minutes)}</span
      ><span class="eta-unit">мин</span>
    </div>
    <div class="eta-name">${escapeHtml(best.full_name)}</div>
    <div class="eta-sub">
      <span class="mark">${escapeHtml(best.mark)}</span>
      ${transportLabel(best)} ·
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
        <span class="mark">${escapeHtml(candidate.mark)}</span>
        ${transportLabel(candidate)}
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
  `;

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

/** Класс карточки вызова: новый, в очереди или назначенный. */
function callStateClass(call) {
  if (call.status === "queued") {
    return "is-queued";
  }
  return call.assigned_employee_id ? "is-assigned" : "is-new";
}

/**
 * Строка исполнителя под вызовом.
 *
 * Для вызова в очереди показывается абсолютное время прибытия, а не
 * «через N минут»: исполнитель сначала закончит текущие работы, и главный
 * вопрос диспетчера здесь — насколько будет нарушен регламент.
 */
function assigneeLine(call) {
  if (!call.assigned_employee_name) {
    return "";
  }
  const name = escapeHtml(call.assigned_employee_name);

  if (call.status === "queued") {
    return (
      `<div class="call-assignee is-queued">в очереди к ${name}` +
      (call.eta_at ? ` · прибытие ~<span class="num">${formatClock(call.eta_at)}</span>` : "") +
      `</div>`
    );
  }

  return (
    `<div class="call-assignee">→ ${name}` +
    (call.eta_minutes !== null
      ? ` · <span class="num">${formatMinutes(call.eta_minutes)}</span> мин`
      : "") +
    `</div>`
  );
}

function sectionTitle(text) {
  const element = document.createElement("div");
  element.className = "section-title";
  element.textContent = text;
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

  const deadline = new Date(call.created_at).getTime() + CONFIG.regulationLimitMin * 60000;
  const verdict = assignmentVerdict(call, deadline);

  const headline = call.status === "arrived"
    ? `<span class="eta-value is-ok">✓</span>`
    : `<span class="eta-value ${verdict.cls}">${formatClock(call.eta_at)}</span>`;

  const timing = call.status === "arrived"
    ? `на месте с <span class="num">${formatClock(call.eta_at)}</span>`
    : `прибытие ~<span class="num">${formatClock(call.eta_at)}</span>` +
      ` · срок <span class="num">${formatClock(new Date(deadline).toISOString())}</span>`;

  const block = document.createElement("div");
  block.className = "eta-block";
  block.innerHTML = `
    <div>${headline}</div>
    <div class="eta-name">${escapeHtml(call.assigned_employee_name || "—")}</div>
    <div class="eta-sub">${escapeHtml(statusTitle(call.status))} · ${timing}${
      call.vehicle_call_sign
        ? ` · <span class="num">${escapeHtml(call.vehicle_call_sign)}</span>`
        : ""
    }</div>
    <div class="eta-message ${verdict.cls}">${escapeHtml(verdict.text)}</div>
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

/** Успевает ли исполнитель к контрольному сроку — цвет и формулировка. */
function assignmentVerdict(call, deadline) {
  if (call.status === "arrived") {
    return { cls: "is-ok", text: "Исполнитель на месте" };
  }
  if (!call.eta_at) {
    return { cls: "", text: "Время прибытия не рассчитано" };
  }
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
