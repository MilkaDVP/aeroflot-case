/*
 * Рабочий экран диспетчера: связывает карту, панели и сервер.
 *
 * Схема работы простая и намеренно однонаправленная: опрос сервера
 * раз в несколько секунд обновляет состояние, состояние перерисовывает
 * экран. Клиент ничего не вычисляет сам — он показывает то, что решил
 * сервер, иначе на экране и в базе оказались бы разные картины.
 */

const state = {
  graph: null,
  employees: [],
  aircraft: [],
  calls: [],
  vehicles: [],
  defects: [],
  selectedCall: null,
  suggestion: null,
  // Кандидат, выбранный диспетчером вручную. null — согласие с системой.
  pickedEmployeeId: null,
};

const elements = {
  map: document.getElementById("map"),
  callsList: document.getElementById("calls-list"),
  callsCount: document.getElementById("calls-count"),
  suggestBody: document.getElementById("suggest-body"),
  assignButton: document.getElementById("assign"),
  calcTime: document.getElementById("calc-time"),
  clock: document.getElementById("clock"),
  connStatus: document.getElementById("conn-status"),
};

let airportMap = null;
let pollTimer = null;

if (Session.token) {
  start();
} else {
  window.location.href = "index.html";
}

async function start() {
  try {
    const user = await Api.me();
    if (!user.airport_icao || !user.shift) {
      window.location.href = "select-airport.html";
      return;
    }

    Session.user = user;
    document.getElementById("ctx-airport").textContent = user.airport_icao;
    document.getElementById("ctx-shift").textContent =
      user.shift === "day" ? "дневная" : "ночная";
    document.getElementById("ctx-user").textContent = user.full_name;

    airportMap = new AirportMap(elements.map);
    airportMap.onStandClick = openCallFormForStand;
    airportMap.attachTiles(showMapNotice);

    state.graph = await Api.airportGraph(user.airport_icao);
    airportMap.render(state.graph);
    applyBasemap(savedBasemap());

    state.defects = await Api.defectTypes();

    await refresh();
    startClock();
    pollTimer = setInterval(refresh, CONFIG.pollIntervalMs);
  } catch (error) {
    elements.suggestBody.innerHTML = `<div class="error">${error.detail}</div>`;
  }
}

/** Один цикл опроса: данные с сервера, затем перерисовка экрана. */
async function refresh() {
  try {
    const [employees, aircraft, calls, vehicles] = await Promise.all([
      Api.employees(),
      Api.aircraft(),
      Api.calls(),
      Api.vehicles(),
    ]);

    state.employees = employees;
    state.aircraft = aircraft;
    state.calls = calls;
    state.vehicles = vehicles;

    // Выбранный вызов мог быть закрыт другим диспетчером.
    if (state.selectedCall) {
      const fresh = calls.find((call) => call.id === state.selectedCall.id);
      state.selectedCall = fresh || null;
      if (!fresh) {
        clearSuggestion();
      }
    }

    renderAll();
    elements.connStatus.textContent = `обновлено ${new Date().toLocaleTimeString("ru-RU")}`;
  } catch (error) {
    elements.connStatus.textContent = error.detail || "нет связи с сервером";
  }
}

function renderAll() {
  elements.callsCount.textContent = state.calls.length;
  renderCalls(elements.callsList, state.calls, state.selectedCall?.id, selectCall);
  renderShiftStats(state.employees, state.vehicles);

  airportMap.drawAircraft(state.aircraft);
  airportMap.drawVehicles(state.vehicles);
  airportMap.drawEmployees(state.employees, employeeClass);
}

/**
 * Класс метки сотрудника на карте.
 *
 * Пока вызов не выбран, допуск определить не с чем — все свободные
 * показываются нейтрально. Как только подбор выполнен, зелёными
 * становятся только те, кто действительно может ехать на этот борт.
 */
function employeeClass(employee) {
  if (employee.status === "offline") {
    return "is-offline";
  }
  if (["assigned", "en_route", "busy"].includes(employee.status)) {
    return "is-busy";
  }
  if (!state.suggestion) {
    return "is-no-permit";
  }

  const isCandidate = state.suggestion.candidates.some(
    (candidate) => candidate.employee_id === employee.id
  );
  return isCandidate ? "is-ok" : "is-no-permit";
}

/* --- Работа с вызовом --- */

/**
 * Показ уже назначенного вызова: исполнитель и его маршрут.
 *
 * Новый подбор здесь не запускается: он предложил бы другого сотрудника
 * и нарисовал бы на карте чужой маршрут — диспетчер увидел бы тревогу
 * по вызову, который уже обслуживается.
 */
function showAssignment(call) {
  state.suggestion = null;
  state.pickedEmployeeId = null;
  elements.calcTime.textContent = "";
  elements.assignButton.disabled = true;

  renderAssignment(elements.suggestBody, call);
  if (call.route_node_ids && call.route_node_ids.length > 1) {
    airportMap.drawRoute(call.route_node_ids, call.pickup_node_id);
  } else {
    airportMap.clearRoute();
  }
  airportMap.drawEmployees(state.employees, employeeClass);
}

async function selectCall(call) {
  state.selectedCall = call;
  state.pickedEmployeeId = null;
  renderCalls(elements.callsList, state.calls, call.id, selectCall);

  airportMap.focusOn(call.stand_node_id, 4);

  if (!isAssignable(call)) {
    showAssignment(call);
    return;
  }

  elements.suggestBody.innerHTML = '<div class="empty">Подбор…</div>';

  try {
    const suggestion = await Api.suggest(call.id);
    state.suggestion = suggestion;
    elements.calcTime.textContent = `${suggestion.elapsed_ms} мс`;

    renderSuggestion(elements.suggestBody, suggestion, null, pickCandidate, queueOptions());
    airportMap.drawEmployees(state.employees, employeeClass);

    if (suggestion.best) {
      airportMap.drawRoute(
        suggestion.best.route.node_ids,
        suggestion.best.route.pickup_node_id
      );
      elements.assignButton.disabled = !isAssignable(call);
    } else {
      airportMap.clearRoute();
      elements.assignButton.disabled = true;
    }
  } catch (error) {
    elements.suggestBody.innerHTML = `<div class="error">${error.detail}</div>`;
  }
}

/** Диспетчер выбрал другого кандидата — показываем его маршрут. */
function pickCandidate(candidate) {
  state.pickedEmployeeId = candidate.employee_id;
  renderSuggestion(
    elements.suggestBody,
    state.suggestion,
    candidate.employee_id,
    pickCandidate,
    queueOptions()
  );
  airportMap.drawRoute(candidate.route.node_ids, candidate.route.pickup_node_id);
  elements.assignButton.disabled = !isAssignable(state.selectedCall);
}

/**
 * Параметры кнопки «В очередь» у занятых сотрудников.
 *
 * Право решает сервер; здесь роль нужна только для того, чтобы заранее
 * показать недоступную кнопку, а не дать нажать и получить отказ.
 */
function queueOptions() {
  const role = Session.user ? Session.user.role : null;
  const canOverride = role === "shift_supervisor" || role === "admin";
  const assignable = isAssignable(state.selectedCall);

  let denyReason = "";
  if (!assignable) {
    denyReason = "Вызов уже назначен";
  } else if (!canOverride) {
    denyReason = "Ставить вызов в очередь к занятому вправе только начальник смены";
  }

  return {
    allowed: canOverride && assignable,
    denyReason,
    onQueue: queueOnto,
  };
}

/**
 * Можно ли ещё назначить вызов.
 *
 * Сервер повторное назначение и так отклонит, но кнопка, которая ведёт
 * только к сообщению об ошибке, — дефект интерфейса: подбор по уже
 * назначенному вызову диспетчер открывает для справки, а не для решения.
 */
function isAssignable(call) {
  return Boolean(call) && (call.status === "new" || call.status === "suggested");
}

/**
 * Постановка выбранного вызова в очередь к занятому сотруднику.
 *
 * Причина обязательна: это решение человека о заведомом нарушении
 * регламента, и оно должно остаться в записи вызова.
 */
async function queueOnto(busy) {
  if (!state.selectedCall) {
    return;
  }

  const reason = window.prompt(
    `Поставить вызов в очередь к сотруднику ${busy.full_name}?\n` +
      `Он освободится около ${formatClock(busy.busy_until)} — регламент 15 минут, ` +
      "скорее всего, будет нарушен.\n\nУкажите причину:"
  );
  if (!reason) {
    return;
  }

  try {
    await Api.assign(state.selectedCall.id, busy.employee_id, reason);
    clearSuggestion();
    state.selectedCall = null;
    await refresh();
  } catch (error) {
    window.alert(error.detail);
  }
}

function clearSuggestion() {
  state.suggestion = null;
  state.pickedEmployeeId = null;
  elements.calcTime.textContent = "";
  elements.assignButton.disabled = true;
  airportMap.clearRoute();
  renderSuggestion(elements.suggestBody, null, null, pickCandidate);
}

elements.assignButton.addEventListener("click", async () => {
  if (!state.selectedCall || !state.suggestion) {
    return;
  }

  const best = state.suggestion.best;
  const employeeId = state.pickedEmployeeId || best?.employee_id;
  if (!employeeId) {
    return;
  }

  // Назначение не того, кого предложила система, требует причины —
  // это правило сервера, но спросить её должен клиент, иначе диспетчер
  // получит непонятную ошибку 422.
  let reason = null;
  if (best && employeeId !== best.employee_id) {
    reason = window.prompt(
      "Назначается не тот сотрудник, которого предложила система.\n" +
        "Укажите причину переопределения:"
    );
    if (!reason) {
      return;
    }
  }

  elements.assignButton.disabled = true;
  try {
    await Api.assign(state.selectedCall.id, employeeId, reason);
    clearSuggestion();
    state.selectedCall = null;
    await refresh();
  } catch (error) {
    window.alert(error.detail);
  } finally {
    elements.assignButton.disabled = false;
  }
});

/* --- Регистрация вызова --- */

document.getElementById("new-call").addEventListener("click", () => openCallForm(null));

/** Клик по стоянке на карте открывает форму с уже выбранным бортом. */
function openCallFormForStand(node) {
  const aircraft = state.aircraft.find((item) => item.stand_node_id === node.id);
  openCallForm(aircraft ? aircraft.id : null);
}

function openCallForm(preselectedAircraftId) {
  const backdrop = document.createElement("div");
  backdrop.className = "modal-backdrop";

  const options = state.aircraft
    .map(
      (item) =>
        `<option value="${item.id}" ${
          item.id === preselectedAircraftId ? "selected" : ""
        }>${item.board_number} · ${item.aircraft_type} · стоянка ${item.stand_ref}</option>`
    )
    .join("");

  const defects = state.defects
    .map(
      (item) =>
        `<option value="${item.code}">${item.name} — ATA ${item.ata}, требуется ${item.category}</option>`
    )
    .join("");

  backdrop.innerHTML = `
    <div class="modal">
      <h2>Регистрация вызова</h2>
      <div class="modal-field">
        <label for="form-aircraft">Воздушное судно</label>
        <select id="form-aircraft">${options}</select>
      </div>
      <div class="modal-field">
        <label for="form-defect">Характер неисправности</label>
        <select id="form-defect">${defects}</select>
      </div>
      <div id="form-error" class="error" hidden></div>
      <div class="modal-actions">
        <button id="form-cancel">Отмена</button>
        <button class="primary" id="form-submit">Зарегистрировать</button>
      </div>
    </div>
  `;

  document.body.append(backdrop);

  backdrop.querySelector("#form-cancel").addEventListener("click", () => backdrop.remove());
  backdrop.addEventListener("click", (event) => {
    if (event.target === backdrop) {
      backdrop.remove();
    }
  });

  backdrop.querySelector("#form-submit").addEventListener("click", async () => {
    const aircraftId = Number(backdrop.querySelector("#form-aircraft").value);
    const defectCode = backdrop.querySelector("#form-defect").value;
    const errorBox = backdrop.querySelector("#form-error");

    try {
      const call = await Api.createCall(aircraftId, defectCode);
      backdrop.remove();
      await refresh();
      // Сразу открываем подбор: диспетчер регистрирует вызов затем,
      // чтобы немедленно кого-то отправить.
      const created = state.calls.find((item) => item.id === call.id);
      if (created) {
        selectCall(created);
      }
    } catch (error) {
      errorBox.hidden = false;
      errorBox.textContent = error.detail;
    }
  });
}

/* --- Прочее --- */

/* --- Подложка карты --- */

const BASEMAP_STORAGE = "aeroflot.basemap";

/**
 * Выбор подложки запоминается между сеансами.
 *
 * Диспетчер работает всю смену и не должен каждый раз возвращать
 * привычный ему вид карты после перезагрузки страницы.
 */
function savedBasemap() {
  return localStorage.getItem(BASEMAP_STORAGE) || "scheme";
}

function applyBasemap(key) {
  airportMap.setBasemap(key);
  localStorage.setItem(BASEMAP_STORAGE, key);

  for (const button of document.querySelectorAll("#basemap-switch button")) {
    button.classList.toggle("active", button.dataset.basemap === key);
  }

  document.getElementById("map-credit").textContent = airportMap.tiles.credit;
  // Схема работает офлайн, поэтому прошлое предупреждение о сети снимаем.
  if (key === "scheme") {
    showMapNotice(null);
  }
}

function showMapNotice(message) {
  const notice = document.getElementById("map-notice");
  notice.hidden = !message;
  notice.textContent = message || "";
}

for (const button of document.querySelectorAll("#basemap-switch button")) {
  button.addEventListener("click", () => applyBasemap(button.dataset.basemap));
}

document.getElementById("zoom-in").addEventListener("click", () => {
  airportMap.zoomFromCentre(1.4);
});

document.getElementById("zoom-out").addEventListener("click", () => {
  airportMap.zoomFromCentre(1 / 1.4);
});

document.getElementById("zoom-reset").addEventListener("click", () => airportMap.resetView());

document.getElementById("logout").addEventListener("click", async () => {
  clearInterval(pollTimer);
  try {
    await Api.logout();
  } finally {
    Session.clear();
    window.location.href = "index.html";
  }
});

function startClock() {
  tickClock();
  setInterval(tickClock, 1000);
}

function tickClock() {
  elements.clock.textContent = new Date().toLocaleTimeString("ru-RU");
}
