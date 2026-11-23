/*
 * Рабочий экран диспетчера: связывает карту, панели и сервер.
 *
 * Схема работы простая и намеренно однонаправленная: опрос сервера
 * раз в несколько секунд обновляет состояние, состояние перерисовывает
 * экран. Клиент ничего не вычисляет сам — он показывает то, что решил
 * сервер, иначе на экране и в базе оказались бы разные картины.
 */

// Как часто подтверждать захват вызова (сервер держит его 2 минуты).
const CLAIM_HEARTBEAT_MS = 30000;

const state = {
  graph: null,
  employees: [],
  aircraft: [],
  calls: [],
  vehicles: [],
  // Входящие сообщения экипажа, ещё не разобранные.
  reports: [],
  selectedCall: null,
  suggestion: null,
  // Кого диспетчер отправляет на вызов, в порядке выбора. Первый —
  // ответственный исполнитель, остальные едут ему в помощь. Сразу после
  // подбора здесь один человек — тот, кого предложила система.
  picked: [],
  // Как поедет каждый выбранный: employee_id -> auto, walk, with_lead.
  travel: {},
  // Все вызовы с сервера, включая вызовы поддержки (второй инженер).
  // В списке слева — только основные: state.calls.
  allCalls: [],
  // Идут ли учения: сервер сам двигает сотрудников без телефона.
  drill: false,
  // Вызов, который этот диспетчер держит в работе, и когда последний раз
  // подтвердил захват серверу (services/claim.py: без подтверждения
  // захват истекает через 2 минуты).
  claimedCallId: null,
  claimAt: 0,
};

const elements = {
  map: document.getElementById("map"),
  callsList: document.getElementById("calls-list"),
  callsCount: document.getElementById("calls-count"),
  suggestBody: document.getElementById("suggest-body"),
  assignButton: document.createElement("button"),
  executorMenu: document.getElementById("executor-menu"),
  clock: document.getElementById("clock"),
  connStatus: document.getElementById("conn-status"),
};

let airportMap = null;
let pollTimer = null;

elements.assignButton.id = "assign";
elements.assignButton.className = "primary";
elements.assignButton.type = "button";
let refreshVersion = 0;
let appliedRefresh = 0;

function selectionKey() {
  const user = Session.user;
  return `oto.call.${user.login}.${user.airport_icao}.${user.shift}`;
}

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
    // Смена выбрана вручную и не совпадает с идущей по часам аэропорта —
    // подсказка в шапке, чтобы диспетчер не работал не с той сменой.
    const here = (user.airports || []).find((item) => item.icao === user.airport_icao);
    if (here && here.current_shift && here.current_shift !== user.shift) {
      const shiftLabel = document.getElementById("ctx-shift");
      shiftLabel.textContent += ` (по часам ${here.local_time} — ${here.current_shift === "day" ? "дневная" : "ночная"})`;
      shiftLabel.classList.add("is-off-schedule");
    }
    document.getElementById("ctx-user").textContent = user.full_name;

    document.getElementById("admin-link").hidden = !roleAtLeast(user.role, "shift_supervisor");

    airportMap = new AirportMap(elements.map);
    airportMap.onStandClick = openCallFormForStand;
    airportMap.onAircraftClick = showAircraftCard;
    airportMap.onObjectDrop = dropObject;
    airportMap.onObjectRefused = (kind, item) =>
      showToast(`${item.call_sign} занята — переносить можно только свободную машину`, true);
    airportMap.attachTiles(showMapNotice);

    state.graph = await Api.airportGraph(user.airport_icao);
    airportMap.render(state.graph);
    applyBasemap(savedBasemap());
    new MapSearch(document.querySelector(".map-tools"), airportMap, () => ({
      graph: state.graph,
      aircraft: state.aircraft,
      employees: state.employees,
      vehicles: state.vehicles,
    }));

    initAlerts();
    await refresh();
    const saved = Number(sessionStorage.getItem(selectionKey()));
    const call = state.calls.find((item) => item.id === saved);
    if (call) await selectCall(call);
    startClock();
    pollTimer = setInterval(refresh, CONFIG.pollIntervalMs);
  } catch (error) {
    elements.suggestBody.innerHTML = `<div class="error">${error.detail}</div>`;
  }
}

/** Один цикл опроса: данные с сервера, затем перерисовка экрана. */
async function refresh() {
  const version = ++refreshVersion;
  try {
    const [employees, aircraft, calls, vehicles, drill, reports] = await Promise.all([
      Api.mapParticipants(),
      Api.aircraft(),
      Api.calls(),
      Api.vehicles(),
      Api.drillState(),
      Api.reports(),
    ]);
    if (version < appliedRefresh) return;
    appliedRefresh = version;
    const previous = state.selectedCall;
    state.drill = drill.enabled;
    state.reports = reports;

    state.employees = employees;
    state.aircraft = aircraft;
    state.allCalls = calls;
    // Вызов поддержки — не отдельный борт, а второй инженер основного
    // вызова: он показан строкой на его карточке, а не карточкой в списке.
    state.calls = calls.filter((call) => !call.parent_call_id);
    state.vehicles = vehicles;

    // Выбранный вызов мог быть закрыт другим диспетчером.
    if (state.selectedCall) {
      const fresh = calls.find((call) => call.id === state.selectedCall.id);
      state.selectedCall = fresh || null;
      if (!fresh) {
        sessionStorage.removeItem(selectionKey());
        clearSuggestion();
      }
    }

    renderAll();
    renderDrill();
    renderInbox(reports, processReport, rejectReport);

    // Прогноз по назначенному вызову меняется вместе с координатами
    // исполнителя, поэтому его карточка обновляется на каждом опросе.
    // Панель подбора для неназначенного вызова не трогаем: в ней диспетчер
    // мог выбрать другого кандидата, и опрос не должен сбрасывать его выбор.
    const selected = state.selectedCall;
    heartbeatClaim(selected);
    if (selected && (isAssignable(previous) !== isAssignable(selected)
        || (isAssignable(selected) && previous.stand_node_id !== selected.stand_node_id)
        // Вызов забрал другой диспетчер или освободился — панель другая.
        || (isAssignable(selected) && claimedByOther(previous) !== claimedByOther(selected)))) {
      // Фоновый опрос не уводит карту: к вызову переходим, только если
      // сменилась сама стоянка.
      await selectCall(selected, { keepView: previous.stand_node_id === selected.stand_node_id });
    } else if (selected && !isAssignable(selected)) {
      const changedTeam = previous.assigned_employee_id !== selected.assigned_employee_id ||
        String(previous.support.map((item) => item.call_id)) !== String(selected.support.map((item) => item.call_id));
      if (changedTeam || previous.stand_node_id !== selected.stand_node_id) {
        state.suggestion = null;
        state.picked = [];
        state.travel = {};
        refreshExecutorMenu();
        loadExtraCandidates(selected);
      }
      // Маршрут из одной вершины (исполнитель рядом с бортом или учебный
      // инженер после переноса) рисуется от его текущего места — оно
      // меняется на каждом опросе, поэтому перерисовываем всегда.
      if (previous.status !== selected.status ||
          selected.route_node_ids?.length === 1 ||
          previous.stand_node_id !== selected.stand_node_id ||
          previous.pickup_node_id !== selected.pickup_node_id ||
          String(previous.route_node_ids) !== String(selected.route_node_ids)) {
        showCallRoute(selected);
      }
      renderAssignedCard(state.selectedCall);
      updateAssignButton();
    }
    elements.connStatus.textContent = `обновлено ${new Date().toLocaleTimeString("ru-RU")}`;
  } catch (error) {
    elements.connStatus.textContent = error.detail || "нет связи с сервером";
  }
}

function renderAll() {
  elements.callsCount.textContent = state.calls.length;
  renderAlerts(state.calls);
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
  const base = employeeStatusClass(employee);
  return isPositionStale(employee) ? `${base} is-stale` : base;
}

/**
 * Устарели ли GPS-координаты сотрудника.
 *
 * Только для настоящего GPS: точку, поставленную вручную, не обновляют
 * намеренно. Устаревшая метка бледнеет — диспетчер видит, что человек
 * уже не там, где точка, ещё до того, как сработает тревога по вызову.
 */
function isPositionStale(employee) {
  if (employee.location_source !== "gps" || !employee.location_at) {
    return false;
  }
  const ageS = (Date.now() - new Date(employee.location_at).getTime()) / 1000;
  return ageS > CONFIG.positionStaleS;
}

function employeeStatusClass(employee) {
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

/**
 * Открывает вызов по идентификатору — щелчок по замечанию Наставника.
 * Закрытого вызова в списке уже нет: тогда замечание остаётся просто текстом.
 */
function openCallById(callId) {
  const call = state.calls.find((item) => item.id === callId);
  if (call) {
    selectCall(call);
  }
}

document.getElementById("call-history").addEventListener("click", async (event) => {
  const anchor = event.currentTarget;
  try {
    const calls = await Api.calls(true);
    const finished = calls.filter((call) => !call.parent_call_id && call.closed_at)
      .sort((a, b) => b.closed_at.localeCompare(a.closed_at));
    const rows = finished.map((call) => {
      const row = document.createElement("div");
      row.className = "history-call";
      const date = new Date(call.closed_at).toLocaleString("ru-RU");
      row.textContent = `${date} · №${call.id} · ${call.board_number} · ст. ${call.stand_ref || "—"} · ${statusTitle(call.status)}`;
      return row;
    });
    openPopover(anchor, "История вызовов", rows.length ? rows : ["Завершённых вызовов нет"]);
  } catch (error) {
    showToast(error.detail, true);
  }
});

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
  state.picked = [];
  state.travel = {};
  elements.assignButton.disabled = true;

  renderAssignedCard(call);
  showCallRoute(call);
  airportMap.drawEmployees(state.employees, employeeClass);
  loadExtraCandidates(call);
}

/**
 * Кого ещё можно отправить на уже назначенный вызов.
 *
 * Тот же подбор, что и для нового вызова: свободные с допуском и занятые,
 * к которым можно встать в очередь. Назначенные на этот вызов в нём уже
 * не значатся — они заняты.
 */
async function loadExtraCandidates(call) {
  if (call.status === "closed") {
    return;
  }
  try {
    const suggestion = await Api.suggest(call.id);
    // Пока ждали ответ, диспетчер мог открыть другой вызов.
    if (!state.selectedCall || state.selectedCall.id !== call.id) {
      return;
    }
    state.suggestion = suggestion;
    refreshExecutorMenu();
    renderAssignedCard(state.selectedCall);
    updateAssignButton();
  } catch (error) {
    // Без списка «кого добавить» карточка назначенного вызова всё равно полезна.
  }
}

/**
 * Открывает вызов и запускает подбор.
 *
 * keepView — не переводить карту к стоянке вызова. Нужен, когда подбор
 * лишь пересчитывается после переноса машины, борта или сотрудника:
 * диспетчер работает в другом месте карты, и прыжок к вызову сбивал его.
 */
async function selectCall(call, { keepView = false } = {}) {
  closePopover();
  state.selectedCall = call;
  sessionStorage.setItem(selectionKey(), call.id);
  document.getElementById("call-actions").hidden = false;
  state.suggestion = null;
  state.picked = [];
  state.travel = {};
  renderCalls(elements.callsList, state.calls, call.id, selectCall);

  if (!keepView) {
    airportMap.focusOn(call.stand_node_id, 4);
  }

  // Перешёл к другому вызову — прежний освобождается для коллег.
  if (state.claimedCallId !== null && state.claimedCallId !== call.id) {
    releaseClaim();
  }

  if (!isAssignable(call)) {
    state.claimedCallId = null;
    showAssignment(call);
    return;
  }

  // Ждущий назначения вызов берётся в работу. Если его уже разбирает
  // другой диспетчер — только просмотр.
  if (claimedByOther(call)) {
    showClaimedByOther(call);
    return;
  }
  try {
    await Api.claimCall(call.id);
    state.claimedCallId = call.id;
    state.claimAt = Date.now();
  } catch (error) {
    if (state.selectedCall?.id !== call.id) return;
    showClaimedByOther(call, error.detail);
    return;
  }

  elements.suggestBody.innerHTML = '<div class="empty">Подбор…</div>';

  try {
    const suggestion = await Api.suggest(call.id);
    if (state.selectedCall?.id !== call.id || !isAssignable(state.selectedCall)) return;
    state.suggestion = suggestion;
    state.picked = suggestion.best ? [suggestion.best.employee_id] : [];
    renderSuggestion(elements.suggestBody, suggestion);
    refreshExecutorMenu();
    updateAssignButton();
    airportMap.drawEmployees(state.employees, employeeClass);

    if (suggestion.best) {
      airportMap.showRoute(
        suggestion.best.route.node_ids,
        suggestion.best.route.pickup_node_id,
        call.stand_node_id
      );
      elements.assignButton.disabled = !isAssignable(call);
    } else {
      airportMap.drawRouteMissing(call.stand_node_id, "Маршрут не построен: некого отправить");
      elements.assignButton.disabled = true;
    }
  } catch (error) {
    elements.suggestBody.innerHTML = `<div class="error">${error.detail}</div>`;
  }
}

/**
 * Щелчок по кандидату добавляет его к выбранным или убирает.
 *
 * Так на один вызов отправляют двоих: щёлкнуть по обоим и нажать
 * «Назначить». Первый выбранный — ответственный, остальные едут
 * в помощь, каждый своим способом. Отдельной кнопки «добавить второго»
 * нет: выбор одного и выбор нескольких — одно и то же действие.
 */
function pickCandidate(candidate) {
  const index = state.picked.indexOf(candidate.employee_id);
  if (index === -1) {
    state.picked.push(candidate.employee_id);
  } else {
    state.picked.splice(index, 1);
    delete state.travel[candidate.employee_id];
  }
  // Ответственный сменился — «ехать с первым» могло потерять смысл.
  dropImpossibleTravel();
  refreshExecutorMenu();
  airportMap.showRoute(
    candidate.route && candidate.route.node_ids,
    candidate.route && candidate.route.pickup_node_id,
    state.selectedCall && state.selectedCall.stand_node_id
  );
  updateAssignButton();
}

/** Диспетчер выбрал, как поедет инженер. */
function chooseTravel(candidate, mode) {
  state.travel[candidate.employee_id] = mode;
  dropImpossibleTravel();
  refreshExecutorMenu();
}

/** «С первым» без машины у первого невозможно — такой выбор сбрасывается. */
function dropImpossibleTravel() {
  if (leadVehicle()) {
    return;
  }
  for (const employeeId of Object.keys(state.travel)) {
    if (state.travel[employeeId] === "with_lead") {
      delete state.travel[employeeId];
    }
  }
}

/**
 * Машина ответственного — на ней может поехать второй инженер.
 * Новый вызов: машина первого выбранного по расчёту, если он не идёт
 * пешком. Назначенный вызов: машина, записанная в вызов.
 */
function leadVehicle() {
  const call = state.selectedCall;
  if (!call) {
    return null;
  }
  if (!isAssignable(call)) {
    return call.vehicle_call_sign || null;
  }
  const leadId = state.picked[0];
  const lead = state.suggestion
    ? state.suggestion.candidates.find((item) => item.employee_id === leadId)
    : null;
  if (!lead || state.travel[leadId] === "walk") {
    return null;
  }
  return lead.vehicle_call_sign || null;
}

/** Всё, что нужно карточкам правой колонки от экрана. */
function panelContext() {
  return {
    picked: state.picked,
    travel: state.travel,
    leadVehicle: leadVehicle(),
    adding: !isAssignable(state.selectedCall),
    onPick: pickCandidate,
    onTravel: chooseTravel,
    queue: queueOptions(),
  };
}

function executorNodes() {
  const call = state.selectedCall;
  const ctx = panelContext();
  const nodes = [];
  if (ctx.adding) {
    nodes.push(linkButton("Снять исполнителя", unassignSelected, "is-danger"), ...holdButton(call));
    const person = state.employees.find(e => e.id === call.assigned_employee_id);
    if (person && !person.is_training && ["assigned", "accepted"].includes(call.status)) {
      const select = document.createElement("select");
      select.setAttribute("aria-label", "Транспорт исполнителя");
      select.add(new Option("Пешком", ""));
      for (const v of state.vehicles.filter(v => v.status === "free" || v.call_sign === call.vehicle_call_sign)) {
        select.add(new Option(v.call_sign, v.id, false, v.call_sign === call.vehicle_call_sign));
      }
      nodes.push(select, linkButton("Изменить транспорт", async () => {
        try {
          await Api.changeTransport(call.id, select.value);
          await refresh();
          refreshExecutorMenu();
        } catch (error) { showToast(error.detail, true); }
      }));
    }
    for (const member of call.support || []) {
      nodes.push(linkButton(`Снять помощника · ${member.employee_name}`, () => removeSupport(member), "is-danger"));
    }
  }
  const result = state.suggestion;
  if (result) {
    const team = [call.assigned_employee_id, ...(call.support || []).map((item) => item.employee_id)];
    const busy = ctx.adding ? result.busy.filter((item) => !team.includes(item.employee_id))
      .map((item) => ({ ...item, switchable: false })) : result.busy;
    nodes.push(buildCandidateList(result.candidates, ctx), buildMoreChips(result, busy, ctx));
    updateAssignButton();
    nodes.push(elements.assignButton);
  } else {
    nodes.push("Загрузка списка инженеров…");
  }
  return nodes.filter(Boolean);
}

function refreshExecutorMenu() {
  if (popoverState.anchor === elements.executorMenu) {
    popoverState.root.querySelector(".popover-body").replaceChildren(...executorNodes());
    placePopover(popoverState.root, elements.executorMenu);
  }
}

elements.executorMenu.addEventListener("click", () => {
  openPopover(elements.executorMenu, "Управление исполнителями", executorNodes());
});

document.getElementById("cancel-call").addEventListener("click", async () => {
  const call = state.selectedCall;
  if (!call || !await askDialog({
    title: `Отменить вызов №${call.id} к борту ${call.board_number}?`,
    text: "Назначения будут освобождены. Вызов останется в истории.",
    confirm: "Отменить вызов",
    danger: true,
  })) return;
  try {
    await Api.setCallStatus(call.id, "cancelled");
    await refresh();
  } catch (error) {
    showToast(error.detail, true);
  }
});

/** Кнопка в меню учитывает всех выбранных инженеров. */
function updateAssignButton() {
  const count = state.picked.length;
  const adding = !isAssignable(state.selectedCall);
  elements.assignButton.disabled = count === 0 || !state.selectedCall;
  // У назначенного вызова кнопка нужна, только когда кого-то выбрали
  // в «Отправить ещё»: неактивная кнопка внизу была бы лишним шумом.
  elements.assignButton.hidden = adding && count === 0;
  if (adding) {
    elements.assignButton.textContent = count > 1 ? `Отправить ещё ${count}` : "Отправить ещё одного";
  } else {
    elements.assignButton.textContent = count > 1 ? `Назначить ${count} инженеров` : "Назначить";
  }
}

/**
 * Параметры кнопки «В очередь» у занятых сотрудников.
 *
 * Право решает сервер; здесь роль нужна только для того, чтобы заранее
 * показать недоступную кнопку, а не дать нажать и получить отказ.
 */
function queueOptions() {
  const role = Session.user ? Session.user.role : null;
  const canOverride = roleAtLeast(role, "shift_supervisor");

  let denyReason = "";
  if (!canOverride) {
    denyReason =
      "Поставить в очередь к занятому или переключить его может начальник смены (вход supervisor)";
  }

  return {
    allowed: canOverride,
    denyReason,
    onQueue: queueOnto,
    onSwitch: switchOnto,
  };
}

/**
 * Можно ли ещё назначить вызов.
 *
 * Сервер повторное назначение и так отклонит, но кнопка, которая ведёт
 * только к сообщению об ошибке, — дефект интерфейса: подбор по уже
 * назначенному вызову диспетчер открывает для справки, а не для решения.
 */
/**
 * Маршрут уже назначенного вызова.
 *
 * Пока исполнитель в пути, маршрут обязан быть: если его нет, на стоянке
 * появляется отметка «маршрут не построен». Когда инженер уже у борта
 * или вызов закрыт, линия не нужна, и карта остаётся чистой.
 */
function showCallRoute(call) {
  // Исполнитель нужен, чтобы маршрут из одной вершины шёл от его места
  // (учебный инженер после ручного переноса).
  const employee = state.employees.find((item) => item.id === call.assigned_employee_id);
  if (call.status === "assigned" || call.status === "accepted") {
    airportMap.showRoute(
      call.route_node_ids, call.pickup_node_id, call.stand_node_id, undefined, employee
    );
  } else if (call.status === "queued") {
    airportMap.showRoute(
      call.route_node_ids,
      call.pickup_node_id,
      call.stand_node_id,
      "В очереди: маршрут появится, когда инженер освободится"
    );
  } else {
    airportMap.clearRoute();
  }
}

/* --- Захват вызова --- */

/** Вызов разбирает другой диспетчер. */
function claimedByOther(call) {
  return Boolean(
    call && call.claimed_by_login && Session.user && call.claimed_by_login !== Session.user.login
  );
}

/**
 * Вызов в работе у другого: подбор не запускается, назначить нельзя.
 * Начальник смены и старше может забрать вызов себе.
 */
function showClaimedByOther(call, detail) {
  state.suggestion = null;
  state.picked = [];
  state.travel = {};
  elements.assignButton.disabled = true;
  document.getElementById("call-actions").hidden = true;
  airportMap.clearRoute();
  refreshExecutorMenu();

  const who = call.claimed_by_name || "другой диспетчер";
  const box = document.createElement("div");
  box.className = "claim-notice";
  box.innerHTML = `
    <div>Вызов разбирает <b>${escapeHtml(who)}</b></div>
    <div class="muted">${escapeHtml(
      detail || "Назначить и изменить его может только он. Если он отойдёт, вызов освободится сам через 2 минуты."
    )}</div>`;
  if (roleAtLeast(Session.user.role, "shift_supervisor")) {
    const takeover = document.createElement("button");
    takeover.type = "button";
    takeover.className = "primary";
    takeover.textContent = "Забрать себе";
    takeover.addEventListener("click", () => takeOverCall(call));
    box.append(takeover);
  }
  elements.suggestBody.replaceChildren(box);
}

async function takeOverCall(call) {
  try {
    const updated = await Api.claimCall(call.id, true);
    state.claimedCallId = call.id;
    state.claimAt = Date.now();
    showToast(`Вызов ${updated.board_number} теперь у вас`);
    await selectCall(updated, { keepView: true });
  } catch (error) {
    showToast(error.detail, true);
  }
}

/** Отпустить свой вызов — фоном, ответ не нужен. */
function releaseClaim() {
  const id = state.claimedCallId;
  if (id === null) return;
  state.claimedCallId = null;
  Api.releaseCall(id).catch(() => {});
}

/**
 * Раз в полминуты подтверждаем серверу, что вызов всё ещё в работе.
 * Захват потерян (истёк, забрал начальник смены) — забываем его; панель
 * перерисуется по данным опроса.
 */
function heartbeatClaim(selected) {
  const id = state.claimedCallId;
  if (id === null) return;
  if (!selected || selected.id !== id || !isAssignable(selected)) {
    state.claimedCallId = null;
    return;
  }
  if (Date.now() - state.claimAt < CLAIM_HEARTBEAT_MS) return;
  state.claimAt = Date.now();
  Api.claimCall(id).catch(() => {
    if (state.claimedCallId === id) state.claimedCallId = null;
  });
}

// Закрыл вкладку — вызов сразу свободен для коллег, а не через 2 минуты.
window.addEventListener("pagehide", () => {
  const id = state.claimedCallId;
  if (id === null || !Session.token) return;
  fetch(`${CONFIG.apiBase}/api/calls/${id}/release`, {
    method: "POST",
    keepalive: true,
    headers: { Authorization: `Bearer ${Session.token}` },
  }).catch(() => {});
});

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
  const call = state.selectedCall;
  if (!call) {
    return;
  }
  closePopover();
  const reason = await askDialog({
    title: `В очередь к ${busy.full_name}?`,
    text: `Освободится около ${formatClock(busy.busy_until)}. Срок 15 минут, скорее всего, будет нарушен.`,
    placeholder: "Причина",
    confirm: "Поставить в очередь",
  });
  if (!reason) {
    return;
  }
  try {
    // У назначенного вызова занятый встаёт в очередь вторым инженером.
    if (isAssignable(call)) {
      await Api.assign(call.id, busy.employee_id, reason);
    } else {
      await Api.support(call.id, busy.employee_id, reason);
    }
    await afterAction(call.id);
  } catch (error) {
    showToast(error.detail, true);
  }
}

/**
 * Переключение на выбранный вызов сотрудника, который ждёт запчасти.
 *
 * Его текущий борт не теряет исполнителя: вызов встаёт в его же очередь,
 * и он вернётся туда после этого. Причина остаётся в записи.
 */
async function switchOnto(busy) {
  const call = state.selectedCall;
  if (!call) {
    return;
  }
  closePopover();
  const reason = await askDialog({
    title: `Переключить ${busy.full_name}?`,
    text: `Сейчас на ${busy.current_board}: ${busy.switch_note}. К тому борту он вернётся после.`,
    placeholder: "Причина",
    confirm: "Переключить",
  });
  if (!reason) {
    return;
  }
  try {
    await Api.assign(call.id, busy.employee_id, reason, true);
    await afterAction(call.id);
  } catch (error) {
    showToast(error.detail, true);
  }
}

/**
 * Снятие исполнителя с выбранного вызова.
 *
 * Причина обязательна и на стороне сервера: снятие означает, что борт
 * ждал зря, и в разборе должно быть видно, чьё это решение. После снятия
 * вызов сразу открывается заново — уже с новым подбором, чтобы диспетчер
 * не искал его в списке руками.
 */
async function unassignSelected() {
  const call = state.selectedCall;
  if (!call) {
    return;
  }
  const reason = await askDialog({
    title: `Снять ${call.assigned_employee_name || "исполнителя"}?`,
    text: `Вызов ${call.board_number} вернётся к подбору.`,
    placeholder: "Причина",
    confirm: "Снять",
    danger: true,
  });
  if (!reason) {
    return;
  }
  try {
    await Api.unassign(call.id, reason);
    await afterAction(call.id);
  } catch (error) {
    showToast(error.detail, true);
  }
}

/**
 * После действия список перечитывается, и тот же вызов открывается
 * заново — уже в новом состоянии: диспетчер не ищет его в списке руками.
 */
async function afterAction(callId) {
  clearSuggestion();
  state.selectedCall = null;
  await refresh();
  const fresh = state.calls.find((item) => item.id === callId);
  if (fresh) {
    await selectCall(fresh);
  }
}

function clearSuggestion() {
  releaseClaim();
  closePopover();
  state.suggestion = null;
  state.picked = [];
  state.travel = {};
  elements.assignButton.disabled = true;
  elements.assignButton.textContent = "Назначить";
  document.getElementById("call-actions").hidden = true;
  airportMap.clearRoute();
  renderSuggestion(elements.suggestBody, null);
}

elements.assignButton.addEventListener("click", assignPicked);

/**
 * Отправляет выбранных на вызов.
 *
 * Новый вызов: первый выбранный становится исполнителем, остальные —
 * вторыми инженерами. Уже назначенный вызов: все выбранные добавляются
 * вторыми. Каждый назначается отдельным запросом: если второго назначить
 * не удалось, первый всё равно едет, а диспетчер видит причину.
 */
async function assignPicked() {
  const call = state.selectedCall;
  if (!call || !state.suggestion || !state.picked.length) {
    return;
  }
  const picked = state.picked.slice();
  const travel = { ...state.travel };
  const fresh = isAssignable(call);
  const best = state.suggestion.best;

  // Назначение не того, кого предложила система, требует причины —
  // это правило сервера, но спросить её должен клиент, иначе диспетчер
  // получит непонятную ошибку 422.
  let reason = null;
  if (fresh && best && picked[0] !== best.employee_id) {
    reason = await askDialog({
      title: "Не тот, кого предложила система",
      text: `Система предлагает ${best.full_name}. Почему отправляете другого?`,
      placeholder: "Причина",
      confirm: "Назначить",
    });
    if (!reason) {
      return;
    }
  }

  elements.assignButton.disabled = true;
  try {
    if (fresh) {
      const lead = picked.shift();
      await Api.assign(call.id, lead, reason, false, travel[lead] || "auto");
    }
    for (const employeeId of picked) {
      await Api.support(call.id, employeeId, null, travel[employeeId] || "auto");
    }
  } catch (error) {
    showToast(error.detail, true);
  }
  await afterAction(call.id);
}

/** Снятие второго инженера: основной исполнитель остаётся на вызове. */
async function removeSupport(member) {
  const callId = state.selectedCall.id;
  closePopover();
  const reason = await askDialog({
    title: `Снять ${member.employee_name}?`,
    text: "Ответственный исполнитель останется на вызове.",
    placeholder: "Причина",
    confirm: "Снять",
    danger: true,
  });
  if (!reason) {
    return;
  }
  try {
    await Api.unassign(member.call_id, reason);
    await afterAction(callId);
  } catch (error) {
    showToast(error.detail, true);
  }
}

/** Ответ диспетчера инженерам вызова. */
async function sendCallMessage(call, text) {
  try {
    const updated = await Api.sendMessage(call.id, text);
    if (state.selectedCall?.id !== call.id) return;
    appliedRefresh = ++refreshVersion;
    state.selectedCall = updated;
    const input = document.getElementById("message-input");
    if (input && input.value.trim() === text) input.value = "";
    renderAssignedCard(updated, true);
  } catch (error) {
    showToast(error.detail, true);
  }
}

/* --- Регистрация вызова --- */

document.getElementById("new-call").addEventListener("click", () => openCallForm(null));

/**
 * Машину, сотрудника или борт перетащили мышью на новое место.
 *
 * Место сохраняется на сервере сразу. Если по открытому вызову ещё идёт
 * подбор, он пересчитывается: от того, где стоят люди и машины, зависит,
 * кто успеет к борту.
 */
async function dropObject(kind, item, position, done) {
  const point = { lat: position.lat, lon: position.lon };
  const name = item.board_number || item.call_sign || item.full_name;
  try {
    // Борту передаётся и указание «на стоянку N» или «свободно».
    if (kind === "aircraft") await Api.moveAircraft(item.id, position);
    if (kind === "vehicle") await Api.moveVehicle(item.id, point);
    if (kind === "employee") await Api.sendLocation(item.id, point.lat, point.lon);
  } catch (error) {
    // Отказ сервера: объект возвращается туда, где стоял.
    done();
    showToast(error.detail || `Не удалось переместить: ${name}`, true);
    return;
  }
  showToast(`${name}: новое место сохранено`);
  await refresh();
  done();
  if (isAssignable(state.selectedCall)) {
    await selectCall(state.selectedCall, { keepView: true });
  }
}

/**
 * Снять выбор вызова: справа «Выберите вызов слева», на карте — ни
 * маршрута, ни отметок вызова, как сразу после входа. Вызов, который
 * диспетчер держал в работе, освобождается для коллег.
 */
function deselectCall() {
  if (!state.selectedCall) return;
  closePopover();
  sessionStorage.removeItem(selectionKey());
  clearSuggestion();
  state.selectedCall = null;
  refreshExecutorMenu();
  renderAll();
}

// Щелчок по пустому месту списка вызовов (мимо карточек) снимает выбор.
elements.callsList.addEventListener("click", (event) => {
  if (!event.target.closest(".call")) {
    deselectCall();
  }
});

/** Щелчок по борту: кто это и кнопка регистрации вызова на него. */
function showAircraftCard(aircraft, marker) {
  const open = state.calls.find((call) => call.aircraft_id === aircraft.id);
  const items = [
    linkButton("Зарегистрировать вызов", () => {
      closePopover();
      openCallForm(aircraft.id);
    }),
  ];
  if (open) {
    items.unshift(linkButton(`Открыть вызов: ${open.defect_name}`, () => {
      closePopover();
      selectCall(open);
    }));
  }
  openPopover(
    marker,
    `${aircraft.board_number} · ${aircraft.aircraft_type} · ${
      aircraft.on_stand === false ? "вне стоянки" : `ст. ${aircraft.stand_ref}`
    }`,
    items
  );
}

function openCallFormForStand(node) {
  const aircraft = state.aircraft.find((item) => item.stand_node_id === node.id);
  openCallForm(aircraft ? aircraft.id : null);
}

function openCallForm(preselectedAircraftId) {
  showCallForm({
    aircraft: state.aircraft,
    preselectedAircraftId,
    onRegistered: openRegisteredCall,
  });
}

/**
 * Сразу после регистрации открывается подбор: диспетчер регистрирует
 * вызов затем, чтобы немедленно кого-то отправить.
 */
async function openRegisteredCall(call) {
  await refresh();
  const created = state.calls.find((item) => item.id === call.id);
  if (created) {
    selectCall(created);
  }
}

/* --- Прочее --- */

/* --- Входящие сообщения --- */

/** Разбор сообщения: та же форма вызова, но с тем, что сообщил экипаж. */
function processReport(report) {
  showCallForm({
    aircraft: state.aircraft,
    preselectedAircraftId: report.aircraft_id,
    report,
    onRegistered: openRegisteredCall,
  });
}

async function rejectReport(report) {
  const reason = await askDialog({
    title: `Отклонить сообщение по ${report.board_number}?`,
    text: "Вызов создан не будет.",
    placeholder: "Причина",
    confirm: "Отклонить",
    danger: true,
  });
  if (!reason) {
    return;
  }
  try {
    await Api.rejectReport(report.id, reason);
    await refresh();
  } catch (error) {
    showToast(error.detail, true);
  }
}

/* --- Режим учений --- */

/**
 * Значок учений в шапке и переключатель.
 *
 * Значок виден всем: диспетчер должен понимать, что сотрудники на карте
 * движутся по учебному сценарию и прибытие подтверждается само.
 * Включает и выключает учения администратор аэропорта и старше.
 */
function renderDrill() {
  document.getElementById("drill-badge").hidden = !state.drill;
  const toggle = document.getElementById("drill-toggle");
  toggle.hidden = !roleAtLeast(Session.user.role, "local_admin");
  toggle.textContent = state.drill ? "Завершить учения" : "Начать учения";
}

async function toggleDrill() {
  try {
    await Api.setDrill(!state.drill);
    await refresh();
  } catch (error) {
    showToast(error.detail, true);
  }
}

/**
 * Карточка назначенного вызова; в учениях — с кнопкой задержки.
 *
 * «Задержать» останавливает исполнителя на маршруте: так на тренировке
 * разыгрывается вводная «исполнитель встал», и видно, как меняется прогноз.
 */
function renderAssignedCard(call, scrollToEnd = false) {
  renderAssignedPanel(elements.suggestBody, call, sendCallMessage, scrollToEnd);
}

/** В учениях — ссылка «задержать»: разыгрывается вводная «исполнитель встал». */
function holdButton(call) {
  if (!state.drill || !call.assigned_employee_id || call.status === "arrived") {
    return [];
  }
  const employee = state.employees.find((item) => item.id === call.assigned_employee_id);
  if (!employee?.is_training) {
    return [];
  }
  const button = linkButton(
    employee.held ? "Отпустить (учения)" : "Задержать (учения)",
    () => toggleHold(employee),
    "is-warn"
  );
  button.id = "hold-button";
  return [button];
}

async function toggleHold(employee) {
  try {
    await Api.holdEmployee(employee.id, !employee.held);
    await refresh();
    refreshExecutorMenu();
  } catch (error) {
    showToast(error.detail, true);
  }
}

document.getElementById("drill-toggle").addEventListener("click", toggleDrill);

/* --- Подложка карты --- */

const BASEMAP_STORAGE = "aeroflot.basemap";

/**
 * Выбор подложки запоминается между сеансами.
 *
 * Диспетчер работает всю смену и не должен каждый раз возвращать
 * привычный ему вид карты после перезагрузки страницы.
 */
function savedBasemap() {
  const saved = localStorage.getItem(BASEMAP_STORAGE);
  // Режим мог исчезнуть после обновления: у диспетчера, работавшего
  // с прежней версией, в хранилище остаётся неизвестное значение,
  // и без этой проверки карта открылась бы без активной подложки.
  if (saved && document.querySelector(`#basemap-switch [data-basemap="${saved}"]`)) {
    return saved;
  }
  return "scheme";
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
