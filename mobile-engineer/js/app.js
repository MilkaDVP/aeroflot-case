/*
 * Логика приложения инженера.
 *
 * Три экрана по сценарию из ТЗ: вход, подтверждение выхода на смену,
 * рабочий экран. Между ними приложение переключается само, исходя
 * из ответа сервера, — «где я нахожусь» не хранится в клиенте,
 * иначе после перезагрузки телефона состояние разошлось бы с базой.
 */

const state = {
  user: null,
  call: null,
  position: null,
  returnLeg: null,
  // Последние отправленные координаты: чтобы не слать одно и то же.
  sentAt: 0,
  onShift: false,
  // Нет связи с сервером: экран живёт на сохранённом вызове.
  offline: false,
  // Когда получены данные, которые сейчас на экране.
  snapshotAt: null,
  // Удалось ли включить офлайн-оболочку.
  offlineReady: false,
  // Опорная точка аэропорта: на неё центрируется карта, пока нет
  // ни вызова, ни координат.
  airportPoint: null,
  // Самое позднее сообщение, которое инженер уже видел в листе «Диспетчеру».
  chatSeenId: 0,
  // Ожидающее ответа подтверждение (см. askConfirm).
  confirmResolve: null,
};

let locator = null;
let routeMap = null;
let pollTimer = null;
let countdownTimer = null;
let heartbeatTimer = null;

// Действие инженера: что показывает кнопка и что уходит на сервер.
// Порядок в жизненном цикле вызова: назначен -> принял -> прибыл -> завершил.
const ACTIONS = {
  assigned: { label: "Принять вызов", action: "accepted" },

  arrived: { label: "Закрыть вызов", action: "closed" },
};

// Этапы работ, которые отмечает инженер. Остальное он пишет словами.
const STAGE_WAITING_PARTS = "waiting_parts";
const STAGE_WORKING = "repair";
const CLOSE_CONFIRM = "Работы завершены? Закрытый вызов вернуть нельзя.";

const el = (id) => document.getElementById(id);

/* --- Запуск --- */

start();

async function start() {
  registerServiceWorker();
  routeMap = new RouteMap(el("route-map"), onMapTap);
  // «◎» подсвечивается, когда инженер приблизил или сдвинул карту сам.
  routeMap.onViewChange = (custom) => el("map-reset").classList.toggle("active", custom);
  locator = new Locator(onPosition, onGeoError);
  bindEvents();
  watchMapSize();

  if (!Session.token) {
    showScreen("login");
    return;
  }

  try {
    await Outbox.load();
    const user = await Api.me();
    Session.user = user;
    state.user = user;
    afterLogin(user);
  } catch (error) {
    // Нет связи при запуске — не повод выбрасывать из учётной записи:
    // с сохранёнными данными инженер продолжает работу.
    if (error.status !== 401 && Session.user) {
      state.user = Session.user;
      afterLogin(state.user);
      return;
    }
    showScreen("login");
  }
}

/**
 * Регистрация service worker — офлайн-оболочки приложения.
 *
 * Сбой не мешает работе, но и замалчивать его нельзя: без service worker
 * приложение не откроется там, где пропала связь, а именно ради этого
 * оно и делалось как PWA. Частая причина отказа — открытие по http
 * не с localhost: браузер считает такой адрес небезопасным контекстом.
 */
/**
 * Перерисовка карты при изменении её размера.
 *
 * Карта вписывает снимок и маршрут в свой текущий размер. Но размер
 * меняется уже после отрисовки: в карточке вызова появляются сообщение
 * экипажа, строка про машину, плашка прогноза — и карта сжимается.
 * Старые пропорции оставляли по краям полосы без снимка. Наблюдатель
 * за размером закрывает это целиком, включая поворот телефона.
 */
function watchMapSize() {
  if (!("ResizeObserver" in window)) {
    return;
  }
  new ResizeObserver(onMapResize).observe(el("route-map"));
}

function onMapResize() {
  if (state.user && !el("screen-work").hidden) {
    drawRoute();
  }
}

function registerServiceWorker() {
  if (!("serviceWorker" in navigator)) {
    state.offlineReady = false;
    return;
  }

  navigator.serviceWorker
    .register("sw.js")
    .then(() => {
      state.offlineReady = true;
    })
    .catch((error) => {
      state.offlineReady = false;
      console.warn("Офлайн-режим недоступен:", error.message);
    });
}

function showScreen(name) {
  for (const screen of ["login", "shift", "work"]) {
    el(`screen-${screen}`).hidden = screen !== name;
  }
}

/**
 * Куда идти после успешного входа.
 *
 * Если сотрудник уже в статусе «свободен» или занят вызовом, он на смене,
 * и подтверждать выход заново незачем: приложение могли просто закрыть.
 */
function afterLogin(user) {
  if (user.role !== "engineer" || !user.employee_id) {
    showLoginError("Эта учётная запись не связана с карточкой инженера");
    Session.clear();
    showScreen("login");
    return;
  }

  el("shift-name").textContent = user.full_name;
  el("menu-name").textContent = user.full_name;

  state.airportPoint = airportPointOf(user);
  state.onShift = user.on_shift;
  if (state.onShift) enterWork();
  else showScreen("shift");
}

function airportPointOf(user) {
  const airport = (user.airports || []).find((item) => item.icao === user.airport_icao);
  return airport ? airport.centre || airport.ref_point : null;
}

/* --- Вход --- */

el("login-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  showLoginError(null);
  el("login-submit").disabled = true;

  try {
    const result = await Api.login(
      el("login").value.trim(),
      el("password").value
    );
    Session.token = result.token;

    const user = await Api.me();
    Session.user = user;
    state.user = user;
    afterLogin(user);
  } catch (error) {
    showLoginError(error.detail || "Не удалось войти");
  } finally {
    el("login-submit").disabled = false;
  }
});

function showLoginError(message) {
  el("login-error").hidden = !message;
  el("login-error").textContent = message || "";
}

/* --- Выход на смену --- */

el("shift-start").addEventListener("click", async () => {
  el("shift-error").hidden = true;
  el("shift-start").disabled = true;

  try {
    await Api.setShift(state.user.employee_id, true);
    state.onShift = true;
    Session.user = state.user = await Api.me();
    enterWork();
  } catch (error) {
    el("shift-error").hidden = false;
    el("shift-error").textContent = error.detail;
  } finally {
    el("shift-start").disabled = false;
  }
});

el("shift-logout").addEventListener("click", logout);

/**
 * Переход на рабочий экран.
 *
 * Здесь же запускается GPS: по сценарию из ТЗ передача координат
 * начинается именно с момента выхода на смену, а не со входа
 * в приложение — до заступления диспетчер не должен видеть человека
 * на карте как доступного.
 */
function enterWork() {
  localNavigation.load(state.user.airport_icao, graph => routeMap.setGraph(graph));
  showScreen("work");
  // Связь вернулась — досылаем накопленное сразу, не дожидаясь опроса.
  window.addEventListener("online", refresh);
  setGeoMode("gps");
  refresh();
  pollTimer = setInterval(refresh, CONFIG.pollIntervalMs);
  function tick() {
    checkAircraftArrival();
    updateCountdown();
    const remaining = Date.parse(state.user?.shift_ends_at) - Date.now();
    el("extend-shift").hidden = !(remaining <= 15 * 60000);
  }
  tick();
  countdownTimer = setInterval(tick, 1000);
  heartbeatTimer = setInterval(sendHeartbeat, CONFIG.locationIntervalMs);
}

/* --- Опрос вызова --- */

let refreshing;
function refresh() {
  if (!refreshing) refreshing = refreshOnce().finally(() => { refreshing = null; });
  return refreshing;
}
async function refreshOnce() {
  try {
    // Сначала досылаем накопленное: иначе ответ сервера не учёл бы
    // действия, нажатые без связи, и «откатил» бы экран назад.
    const user = await Api.me();
    if (!state.onShift) return;
    Session.user = state.user = user;
    if (!user.on_shift) {
      state.onShift = false;
      stopWork();
      showScreen("shift");
      return;
    }
    await flushOutbox();
    if (Outbox.items.length) {
      throw new ApiError(0, "Нет связи с сервером");
    }
    const payload = await Api.currentCall();
    // Сервер отдаёт либо вызов с маршрутом, либо сообщение «вызовов нет».
    const previous = state.call ? state.call.call : null;
    if (Outbox.items.length) return;
    state.returnLeg = payload?.instruction && !payload.call ? payload : null;
    state.call = payload && payload.call ? payload : null;
    state.offline = false;
    state.snapshotAt = Date.now();
    saveSnapshot();
    noticeIfTaken(previous);
    renderWork();
  } catch (error) {
    if (error.status === 401) {
      logout();
      return;
    }
    if (error.status === 0 || error.status >= 500) {
      await goOffline();
      return;
    }
    showToast(error.detail, true);
  }
}

/* --- Работа без связи --- */

function saveSnapshot() {
  OfflineStore.set(KEY_SNAPSHOT, {
    payload: state.call,
    returnLeg: state.returnLeg,
    at: state.snapshotAt,
    employeeId: state.user.employee_id,
  });
}

/**
 * Связи нет: показываем последний известный вызов с пометкой о возрасте.
 *
 * Снимок читается с диска один раз за период без связи — дальше экран
 * живёт на состоянии в памяти, к которому уже применены нажатые действия.
 */
async function goOffline() {
  if (!state.offline) {
    const snapshot = await OfflineStore.get(KEY_SNAPSHOT);
    if (snapshot && snapshot.employeeId === state.user.employee_id && state.call === null) {
      state.call = snapshot.payload;
      // На телефоне может оставаться снимок от версии с возвратом пешком.
      state.returnLeg = snapshot.returnLeg?.instruction?.startsWith("Вернитесь на место ожидания")
        ? null : snapshot.returnLeg || null;
      state.snapshotAt = snapshot.at;
    }
    state.offline = true;
  }
  renderWork();
}

/** Полоса состояния связи: возраст данных и неотправленные действия. */
function renderLink() {
  const banner = el("link-banner");
  const pending = Outbox.items.length;
  banner.hidden = !state.offline && pending === 0;
  if (banner.hidden) {
    return;
  }
  // Простыми словами: можно ли работать и что будет с отметками.
  const parts = [];
  if (state.offline) {
    parts.push("Нет связи. Работайте как обычно — отметки уйдут сами, когда появится сеть");
    if (state.snapshotAt) {
      parts.push(`Данные на ${clockText(state.snapshotAt)}`);
    }
  }
  if (pending) {
    parts.push(state.offline ? `Ждут отправки: ${pending}` : `Отправляем отметки: ${pending}`);
  }
  banner.textContent = parts.join(". ");
}

function clockText(timestamp) {
  return new Date(timestamp).toLocaleTimeString("ru-RU", { hour: "2-digit", minute: "2-digit" });
}

/** Досылает накопленные действия; об отказах сервера сообщает инженеру. */
async function flushOutbox() {
  if (!Outbox.items.length) {
    return;
  }
  const rejected = await Outbox.flush();
  if (rejected.length) {
    showToast(`Действие не принято: ${rejected[0].detail}`, true);
  }
}

/**
 * Применяет действие к вызову на экране, не дожидаясь сервера.
 *
 * Инженер без связи должен видеть результат нажатия сразу: кнопка
 * переходит к следующему шагу, а действие ждёт отправки в очереди.
 */
function applyLocally(action) {
  const call = state.call.call;
  if (action === "closed" || action === "declined") {
    state.call = null;
    return;
  }
  call.status = action;
  if (action === "arrived") {
    call.work_stage = call.work_stage || "inspection";
    if (call.control) {
      call.control.level = "arrived";
      call.control.alert = false;
    }
  }
}

/**
 * Сообщение, если вызов у инженера забрали.
 *
 * Диспетчер может снять исполнителя, пока тот в пути. Без сообщения вызов
 * просто исчезает с экрана — инженер решит, что приложение сломалось,
 * и поедет к борту, на который его уже не ждут.
 */
function noticeIfTaken(previous) {
  if (!previous || state.call) {
    return;
  }
  // Свои действия инженера (завершение работ) обрабатываются отдельно
  // и до опроса обнуляют state.call сами.
  showToast(`Вызов ${previous.board_number} снят диспетчером`, true);
}

function renderWork() {
  renderLink();
  const hasCall = Boolean(state.call);
  el("idle-block").hidden = hasCall;
  el("call-block").hidden = !hasCall;
  const current = state.call?.call;
  el("call-action").hidden = !current || !ACTIONS[current.status];
  el("decline-call").hidden = current?.status !== "assigned";
  el("repair-panel").hidden = current?.status !== "arrived";
  if (current?.status === "arrived") {
    const form = el("repair-form");
    const key = [current.id, current.mel_category].join("/");
    if (form.dataset.key !== key) {
      el("repair-mel").value = current.mel_category || "";
      form.dataset.key = key;
    }
    el("missing-tools").checked = !!current.missing_tools;
  }
  el("chat-open").hidden = !hasCall;
  if (!hasCall) {
    closeSheet("chat-sheet");
  }

  // Карта рисуется в любом случае: без вызова она показывает, где стоит
  // сам инженер, и позволяет поставить точку в режиме имитации.
  drawRoute();

  if (!hasCall) {
    renderIdle();
    return;
  }

  const call = state.call.call;
  el("call-stand").textContent = call.stand_ref || "—";
  const details = el("call-details");
  if (details.dataset.call !== String(call.id)) details.open = false;
  details.dataset.call = call.id;
  el("dock-board").textContent = call.board_number;
  el("dock-stand").textContent = call.stand_ref ? "Ст. " + call.stand_ref : "Вне стоянки";
  el("call-board").textContent = call.board_number;
  el("call-type").textContent = call.aircraft_type;
  el("call-defect").textContent = call.defect_name;
  // Сообщение экипажа своими словами — то, что инженеру нужно у борта:
  // какая стойка, где течь, что на индикации.
  el("call-description").hidden = !call.defect_description;
  el("call-description").textContent = call.defect_description
    ? `«${call.defect_description}»`
    : "";
  el("call-mark").textContent = call.required_mark;

  // Инженер должен знать, что после закрытия его не отпустят, а сразу
  // отправят к следующему борту.
  const queued = state.call.queued_count || 0;
  el("call-queue").hidden = queued === 0;
  el("call-queue").textContent = queued ? `После этого вызова ещё в очереди: ${queued}` : "";

  // Машина — главное указание в пути: без подсказки инженер пойдёт
  // пешком и опоздает, хотя маршрут посчитан через машину.
  const vehicle = state.call.vehicle;
  el("call-vehicle").hidden = !vehicle && !state.call.instruction;
  el("call-vehicle").textContent = state.call.instruction || vehicleInstruction(vehicle);
  el("call-eta").textContent =
    call.eta_minutes === null ? "—" : `${formatMinutes(call.eta_minutes)} мин`;
  renderTools(call);

  const step = ACTIONS[call.status];
  el("call-action").textContent = step ? step.label : "Ожидание";
  el("call-action").disabled = !step || (step.action === "closed" && !call.mel_category);

  el("work-status").textContent = statusTitle(call.status);
  el("work-status-dot").className = "status-dot busy";

  updateDistance();
  updateCountdown();
  renderForecast(call.control);
  renderAtAircraft(call);
  renderChat(call);
}

/**
 * Блок «у борта»: этап работ и AOG. Виден только после прибытия —
 * до осмотра ни этапа, ни диагноза ещё нет.
 */
function renderAtAircraft(call) {
  // Отметки имеют смысл только у борта: до осмотра ни запчастей, ни AOG нет.
  el("quick-marks").hidden = call.status !== "arrived";
  el("parts-toggle").classList.toggle("active", call.work_stage === STAGE_WAITING_PARTS);
  el("aog-toggle").classList.toggle("active", call.aog);
}

/**
 * «Жду запчасти» — переключатель: пока он включён, диспетчер может
 * отправить инженера на срочный борт, и к этому он потом вернётся.
 */
function toggleWaitingParts() {
  const call = state.call && state.call.call;
  if (!call) {
    return;
  }
  changeStage(call.work_stage === STAGE_WAITING_PARTS ? STAGE_WORKING : STAGE_WAITING_PARTS);
}

/* --- Связь с диспетчером --- */

/** Переписка по вызову: свои справа, диспетчера слева. Значок — о новых ответах. */
function renderChat(call) {
  renderChatBadge(call);
  const list = el("chat-list");
  if (list.dataset.callId !== String(call.id)) {
    list.replaceChildren();
    list.dataset.callId = call.id;
    list.dataset.lastId = 0;
    el("chat-input").value = "";
  }
  const follow = list.scrollHeight - list.clientHeight - list.scrollTop <= 3;
  const last = Number(list.dataset.lastId);
  for (const message of call.messages || []) {
    if (message.id <= last) continue;
    const item = document.createElement("div");
    item.className = `chat-item is-${message.author_role}`;
    item.textContent =
      message.author_role === "dispatcher" ? `Диспетчер: ${message.text}` : message.text;
    list.append(item);
    list.dataset.lastId = message.id;
  }
  if (follow) list.scrollTop = list.scrollHeight;
}

/**
 * Число ответов диспетчера, которых инженер ещё не видел.
 * «Видел» — открывал лист сообщений после того, как ответ пришёл.
 */
function renderChatBadge(call) {
  const fromDispatcher = (call.messages || []).filter((item) => item.author_role === "dispatcher");
  const seen = state.chatSeenId || 0;
  const unread = fromDispatcher.filter((item) => item.id > seen).length;
  el("chat-badge").hidden = unread === 0;
  el("chat-badge").textContent = unread;
}

function openChat() {
  const call = state.call && state.call.call;
  if (call) {
    const ids = (call.messages || []).map((item) => item.id);
    state.chatSeenId = ids.length ? Math.max(...ids) : 0;
    renderChatBadge(call);
  }
  openSheet("chat-sheet");
  el("chat-list").scrollTop = el("chat-list").scrollHeight;
}

async function sendChatMessage(event) {
  if (event) {
    event.preventDefault();
  }
  const input = el("chat-input");
  const text = input.value.trim();
  if (!text) {
    return;
  }
  el("chat-send").disabled = true;
  try {
    const call = await Api.sendMessage(text);
    if (state.call?.call.id !== call.id) return;
    if (input.value.trim() === text) input.value = "";
    state.call.call = call;
    renderChat(call);
    el("chat-list").scrollTop = el("chat-list").scrollHeight;
    // Своё сообщение прочитано сразу; новые ответы считаются от него.
    state.chatSeenId = Math.max(...(call.messages || []).map((item) => item.id), 0);
    renderChatBadge(call);
  } catch (error) {
    // Текст остаётся в поле: без связи сообщение не теряется, его можно
    // отправить ещё раз, когда сеть вернётся.
    showToast(state.offline ? "Нет связи — сообщение не отправлено" : error.detail, true);
  } finally {
    el("chat-send").disabled = false;
  }
}

async function changeStage(stage) {
  try {
    await Api.setStage(stage);
    await refresh();
  } catch (error) {
    showToast(error.detail, true);
  }
}

/**
 * AOG — серьёзный статус: борт снимается с рейса. Поэтому постановка
 * подтверждается, а снятие — нет: ошибочно поставленный статус нужно
 * уметь убрать быстро.
 */
async function toggleAog() {
  const call = state.call && state.call.call;
  if (!call) {
    return;
  }
  if (!call.aog && !(await askConfirm("Поставить AOG? Борт не сможет выполнять полёт.", "Поставить AOG"))) {
    return;
  }
  try {
    await Api.setAog(!call.aog);
    await refresh();
  } catch (error) {
    showToast(error.detail, true);
  }
}

/**
 * Инструмент со склада: маршрут сначала ведёт на склад (30 с на
 * получение). Получил — отметка, и маршрут перестраивается к борту.
 */
function renderTools(call) {
  // Сначала — промежуточные точки, выбранные диспетчером, по порядку.
  const points = call.waypoints || [];
  const next = points.find((point) => !point.done_at);
  if (points.length) {
    el("call-tools").hidden = false;
    el("call-tools").classList.toggle("is-done", !next);
    el("call-tools").dataset.mode = "waypoint";
    if (!next) {
      el("call-tools-text").textContent = "Все точки маршрута пройдены";
      el("call-tools-toggle").hidden = true;
      return;
    }
    const number = points.indexOf(next) + 1;
    const name = next.name || "точка на карте";
    const what = next.kind === "warehouse" ? `${name}: получить инструмент`
      : next.kind === "parking" ? `${name}: пересесть на машину` : `через ${name}`;
    el("call-tools-text").textContent = `Точка ${number} из ${points.length} — ${what}`;
    el("call-tools-toggle").hidden = false;
    el("call-tools-toggle").textContent = next.kind === "warehouse" ? "Инструмент получен" : "Пройдено";
    return;
  }
  // Без точек — только отметка «нужен инструмент» (склад выбрала система).
  el("call-tools").dataset.mode = "tools";
  el("call-tools").hidden = !call.needs_tools;
  if (!call.needs_tools) return;
  const warehouse = (call.route_stops || []).find((stop) => stop.kind === "warehouse");
  const taken = Boolean(call.tools_taken_at);
  el("call-tools").classList.toggle("is-done", taken);
  el("call-tools-toggle").hidden = false;
  el("call-tools-text").textContent = taken
    ? "Инструмент получен"
    : `Сначала на ${warehouse?.name || "склад"}: получить инструмент`;
  el("call-tools-toggle").textContent = taken ? "Отменить" : "Инструмент получен";
}

el("call-tools-toggle").addEventListener("click", async () => {
  const call = state.call && state.call.call;
  if (!call) return;
  try {
    if (el("call-tools").dataset.mode === "waypoint") {
      await Api.waypointDone();
    } else {
      await Api.setTools(!call.tools_taken_at);
    }
    await refresh();
  } catch (error) {
    showToast(error.detail, true);
  }
});

/** Строка про машину: забрать по пути или уже едем на ней. */
function vehicleInstruction(vehicle) {
  if (!vehicle) {
    return "";
  }
  if (vehicle.pickup) {
    return `Заберите машину ${vehicle.call_sign} — она отмечена на карте`;
  }
  return `На машине ${vehicle.call_sign}`;
}

function renderIdle() {
  el("work-status").textContent = state.returnLeg?.instruction || (state.onShift ? "на смене, свободен" : "на смене");
  el("work-status-dot").className = "status-dot";
  el("idle-coords").textContent = state.position
    ? `${state.position.lat.toFixed(5)}, ${state.position.lon.toFixed(5)}`
    : "не определены";
}

function statusTitle(status) {
  const titles = {
    assigned: "назначен вызов",
    accepted: "в пути",
    arrived: "у борта",
  };
  return titles[status] || status;
}

/* --- Контрольный срок --- */

/**
 * Обратный отсчёт до истечения регламента.
 *
 * Отсчёт ведётся от момента поступления вызова, а не от назначения:
 * пятнадцать минут по регламенту Аэрофлота отсчитываются с обращения
 * экипажа. Инженеру важно именно оставшееся время — оно прямо отвечает
 * на вопрос «успеваю ли я», ради которого норматив и существует.
 */
function updateCountdown() {
  if (!state.call) {
    return;
  }

  const call = state.call.call;
  // Срок берётся у сервера: для вызова из сообщения экипажа он считается
  // от сообщения, а не от регистрации, и клиент этого сам не знает.
  const deadline = call.control
    ? new Date(call.control.deadline_at).getTime()
    : new Date(call.created_at).getTime() + CONFIG.regulationLimitMin * 60000;
  const leftMin = (deadline - Date.now()) / 60000;
  el("dock-deadline").textContent = call.status === "arrived" ? "У борта" :
    leftMin < 0 ? `+${Math.ceil(-leftMin)} мин` : `Осталось ${Math.floor(leftMin)} мин`;

  const block = el("deadline-block");
  const value = el("deadline-minutes");
  const label = el("deadline-label");

  if (call.status === "arrived") {
    block.className = "deadline is-done";
    value.textContent = "✓";
    label.textContent = "прибытие подтверждено";
    return;
  }

  if (leftMin < 0) {
    block.className = "deadline is-danger";
    value.textContent = `+${Math.ceil(-leftMin)}`;
    label.textContent = "превышение регламента";
    return;
  }

  block.className = `deadline ${countdownClass(leftMin, call.control)}`;
  value.textContent = Math.floor(leftMin);
  label.textContent = "осталось, чтобы быть у борта";
}

/**
 * Цвет обратного отсчёта.
 *
 * Учитывается не только оставшееся время, но и прогноз сервера: зелёные
 * «5 мин» над красным «вы опоздаете» противоречили друг другу, а на ходу
 * человек видит прежде всего крупную цифру — и она успокаивала.
 */
function countdownClass(leftMin, control) {
  const level = control ? control.level : null;
  if (level === "late" || level === "no_signal") {
    return "is-danger";
  }
  if (level === "warn" || leftMin <= 3) {
    return "is-warn";
  }
  return "";
}

/**
 * Прогноз сервера под обратным отсчётом.
 *
 * Обратный отсчёт говорит, сколько осталось, а прогноз — успеваете ли вы
 * при том, где вы сейчас. Если нет, инженер должен узнать это от
 * приложения раньше, чем по рации от диспетчера.
 */
function renderForecast(control) {
  const box = el("deadline-forecast");
  const text = forecastText(control);
  box.hidden = !text;
  box.textContent = text || "";
  box.className = control && control.alert ? "deadline-forecast is-alert" : "deadline-forecast";
}

function forecastText(control) {
  if (!control) {
    return "";
  }
  if (control.level === "late" && control.margin_min !== null) {
    return `По прогнозу вы опоздаете на ${Math.ceil(-control.margin_min)} мин. Диспетчер это видит`;
  }
  if (control.level === "warn" && control.margin_min !== null) {
    return `Впритык: запас ${Math.max(0, Math.floor(control.margin_min))} мин`;
  }
  if (control.level === "no_signal") {
    return "Нет сигнала GPS: диспетчер видит вашу последнюю точку";
  }
  return "";
}

function updateDistance() {
  if (!state.call || !state.position) {
    el("call-distance").textContent = "—";
    return;
  }
  const target = targetPoint();
  if (!target) {
    el("call-distance").textContent = "—";
    return;
  }
  const metres = haversineM(
    state.position.lat, state.position.lon, target.lat, target.lon
  );
  el("call-distance").textContent =
    metres >= 1000 ? `${(metres / 1000).toFixed(1)} км` : `${Math.round(metres)} м`;
}

/** Точка назначения — последний узел маршрута, то есть стоянка борта. */
function targetPoint() {
  const points = state.call?.route_points || state.returnLeg?.route_points;
  return points && points.length ? points[points.length - 1] : null;
}

/**
 * Отрисовка карты.
 *
 * Работает и без вызова: тогда рисуется только своя точка, а если
 * координат ещё нет — карта центрируется на аэропорте, чтобы человеку
 * было куда ткнуть пальцем в режиме имитации.
 */
function drawRoute(position = state.position) {
  const payload = state.call || state.returnLeg;
  const points = state.offline || Date.now() - state.snapshotAt > CONFIG.pollIntervalMs * 2
    ? localNavigation.route(payload, state.position) : payload?.route_points || [];
  const choosing = locator.mode === "simulation" && !locator.last && state.airportPoint;
  updateGuidance(nextManeuver(payload, points, state.position));
  routeMap.render(
    points,
    targetPoint(),
    position,
    state.airportPoint,
    state.call ? state.call.vehicle : null,
    state.call ? state.call.call.pickup_node_id : null,
    choosing ? state.airportPoint : null,
    state.call && !state.call.call.tools_taken_at
      ? ((state.call.call.waypoints || []).find((point) => !point.done_at)?.node_id
        || (state.call.call.route_stops || []).find((stop) => stop.kind === "warehouse")?.node_id)
      : null,
    remainingLine(payload, points, position)
  );
  el("map-hint").hidden = locator.mode !== "simulation";
}

/* --- Действия инженера --- */

let savingAction = false;
async function recordAction(action, fields = {}) {
  const call = state.call?.call;
  if (!call || savingAction) return;
  savingAction = true;
  try {
    await Outbox.add(action, call.id, fields);
    if (state.call?.call.id === call.id) {
      if (action === "assessment" || action === "equipment") Object.assign(call, fields);
      else if (action === "checkpoint") {
        if (fields.checkpoint_id === "vehicle") state.call.vehicle.pickup = false;
        else {
          const waypoint = call.waypoints.find(p => p.node_id === fields.checkpoint_id && !p.done_at);
          if (waypoint) {
            waypoint.done_at = new Date().toISOString();
            if (waypoint.kind === "warehouse") call.tools_taken_at = waypoint.done_at;
          }
        }
      } else applyLocally(action);
      saveSnapshot();
      renderWork();
    }
    refresh();
  } catch (error) { showToast(error.detail || error.message, true); }
  finally { savingAction = false; }
}
el("decline-call").onclick = async () => {
  const id = state.call?.call.id;
  if (await askConfirm("Вернуть вызов диспетчеру?", "Отказаться") && state.call?.call.id === id && state.call.call.status === "assigned") await recordAction("declined");
};
el("repair-form").onsubmit = event => {
  event.preventDefault();
  recordAction("assessment", {mel_category: el("repair-mel").value});
};
el("missing-tools").onchange = event => recordAction("equipment", {missing_tools: event.target.checked});

let arrivalKey = "", insideSince = 0;
function checkAircraftArrival() {
  const payload = state.call, call = payload?.call, pos = locator.last, target = payload?.aircraft_point;
  const key = [call?.id, target?.lat, target?.lon, pos?.source].join("/");
  const valid = call?.status === "accepted" && pos && target && !payload.instruction &&
    !payload.vehicle?.pickup && !(call.waypoints || []).some(p => !p.done_at) &&
    (pos.source !== "gps" || (Date.now() - pos.measuredAt <= 15000 && pos.accuracy <= 25)) &&
    haversineM(pos.lat, pos.lon, target.lat, target.lon) <= 25;
  if (!valid || key !== arrivalKey) insideSince = 0;
  arrivalKey = key;
  if (!valid) return;
  insideSince ||= Date.now();
  if (Date.now() - insideSince >= 10000) recordAction("arrived");
}

el("call-action").addEventListener("click", async () => {
  const call = state.call && state.call.call;
  const step = call && ACTIONS[call.status];
  if (!step) {
    return;
  }
  // Закрытие необратимо для инженера, а кнопка — во всю ширину экрана:
  // случайное касание в перчатке закрывало вызов без вопросов.
  if (step.action === "closed" && !(await askConfirm(CLOSE_CONFIRM, "Закрыть вызов"))) {
    return;
  }

  el("call-action").disabled = true;
  el("call-error").hidden = true;

  try {
    // Действие сначала записывается на телефон и применяется к экрану,
    // и только потом отправляется: без связи оно не теряется и не
    // блокирует работу, а уйдёт на сервер с исходным временем нажатия.
    const hadQueue = step.action === "closed" && (state.call.queued_count || 0) > 0;
    await Outbox.add(step.action, call.id);
    applyLocally(step.action);
    saveSnapshot();
    renderWork();
    await refresh();
    if (step.action === "closed" && !state.offline) {
      // Сообщение по факту ответа сервера, а не по предположению: при
      // очереди следующий вызов уже пришёл, и «вы свободны» было бы ложью.
      showToast(hadQueue && state.call ? "Вызов закрыт. Следующий вызов из очереди" : "Вызов закрыт. Вы снова свободны");
    } else if (step.action === "closed" && hadQueue) {
      showToast("Вызов закрыт. Следующий вызов придёт, когда появится связь");
    }
  } catch (error) {
    el("call-error").hidden = false;
    el("call-error").textContent = error.detail;
  } finally {
    el("call-action").disabled = false;
  }
});

/* --- Местоположение --- */

/**
 * Новая позиция: рисуем сразу, отправляем не чаще заданного интервала.
 *
 * GPS выдаёт точки чаще, чем нужно диспетчеру, а каждая отправка —
 * это радио и батарея. Ограничение частоты здесь, а не на сервере.
 */
let animationFrame = 0, displayedPosition = null;
function animatePosition(position, previous) {
  cancelAnimationFrame(animationFrame);
  if (!previous || haversineM(previous.lat, previous.lon, position.lat, position.lon) > 150) {
    displayedPosition = position;
    drawRoute(position);
    return;
  }
  const start = performance.now();
  const from = displayedPosition || previous;
  function frame(now) {
    const fraction = Math.min(1, (now-start)/450);
    displayedPosition = {...position, lat:from.lat+(position.lat-from.lat)*fraction,
      lon:from.lon+(position.lon-from.lon)*fraction};
    drawRoute(displayedPosition);
    if (fraction < 1) animationFrame = requestAnimationFrame(frame);
  }
  animationFrame = requestAnimationFrame(frame);
}
function onPosition(position) {
  const previous = state.position;
  state.position = position;
  const payload = state.call;
  if ((state.offline || Date.now() - state.snapshotAt > 2 * CONFIG.pollIntervalMs) &&
      payload?.call.status === "accepted" && !payload.instruction &&
      (position.source !== "gps" || (position.accuracy <= 25 && Date.now() - position.measuredAt <= 15000))) {
    const wp = payload.call.waypoints?.find(p => !p.done_at);
    const node = wp && localNavigation.nodes?.get(wp.node_id);
    const near = p => p && haversineM(position.lat, position.lon, p.lat, p.lon) <= 25;
    const id = payload.vehicle?.pickup && near(payload.vehicle) ? "vehicle" : near(node) ? wp.node_id : null;
    if (id) recordAction("checkpoint", {checkpoint_id: id, lat: position.lat, lon: position.lon});
  }
  if (position.source === "gps" && sensorHeading === null) routeMap.setHeading(position.heading);
  checkAircraftArrival();
  if (!el("geo-notice").hidden) {
    clearGeoNotice();
  }
  updateDistance();
  animatePosition(position, previous);
  if (!state.call) {
    renderIdle();
  }

  if (position.source === "simulation" || Date.now() - state.sentAt >= CONFIG.locationIntervalMs) {
    sendPosition(position);
  }
}

/**
 * Отправка точки на сервер вместе с её источником и возрастом.
 *
 * По источнику сервер решает, может ли точка устареть: имитацию не
 * обновляют намеренно, а GPS-фиксация, которой больше полутора минут, — уже
 * повод для тревоги у диспетчера.
 */
function sendPosition(position) {
  state.sentAt = Date.now();
  const fixAgeS = Math.max(0, (Date.now() - position.measuredAt) / 1000);
  Api.sendLocation(
    state.user.employee_id,
    position.lat,
    position.lon,
    position.source,
    Math.round(fixAgeS),
    position.accuracy
  ).catch(() => {
    // Молча: связь на перроне пропадает, следующая отправка пройдёт.
  });
}

/**
 * Повторная отправка последней точки, даже если GPS ничего нового не дал.
 *
 * Стоящий на месте телефон новых фиксаций почти не получает, и без этого
 * сервер решил бы, что телефон пропал. Возраст фиксации при повторе растёт —
 * так сервер отличает «на связи, но GPS потерян» от «на связи и стоит».
 */
function sendHeartbeat() {
  if (locator.last?.source === "gps" && Date.now() - state.sentAt >= CONFIG.locationIntervalMs) {
    sendPosition(locator.last);
  }
}

function onGeoError(message) {
  // Отказ GPS на перроне — обычное дело, а не авария: вместо красной
  // плашки поверх маршрута — спокойная строка с действием.
  el("geo-notice").hidden = false;
  el("geo-mode").textContent = "GPS";
  el("geo-mode").classList.add("is-warn");
  el("geo-error").hidden = false;
  el("geo-error").textContent = message;
}

/** Место определено — напоминание про GPS больше не нужно. */
function clearGeoNotice() {
  el("geo-notice").hidden = true;
  el("geo-mode").classList.remove("is-warn");
  el("geo-mode").textContent = locator.mode === "simulation" ? "Ручной" : "GPS";
}

/** Из строки «GPS недоступен»: сразу ручной режим и подсказка, что делать. */
function switchToManualPlace() {
  setGeoMode("simulation");
  clearGeoNotice();
  showToast("Коснитесь карты там, где вы стоите");
}

/**
 * Касание по карте в режиме имитации задаёт координаты.
 *
 * В режиме GPS касание игнорируется молча было бы непонятно, поэтому
 * приложение объясняет, почему точка не переставилась.
 */
function onMapTap(point) {
  if (locator.mode !== "simulation") {
    showToast("Чтобы указать место пальцем, нажмите «Место» вверху и выберите «Вручную»");
    return;
  }
  applySimulated(point.lat, point.lon);
}

function applySimulated(lat, lon) {
  // Имитация — рабочий режим для демонстрации, поэтому координаты
  // уходят на сервер немедленно, без ожидания интервала. Сброс — до
  // установки точки: locator сразу передаёт её в onPosition.
  state.sentAt = 0;
  locator.simulate(lat, lon);
}

function bindEvents() {
  el("parts-toggle").addEventListener("click", toggleWaitingParts);
  el("aog-toggle").addEventListener("click", toggleAog);
  el("chat-form").addEventListener("submit", sendChatMessage);
  el("map-zoom-in").addEventListener("click", () => routeMap.zoomBy(1.6));
  el("map-zoom-out").addEventListener("click", () => routeMap.zoomBy(1 / 1.6));
  el("map-reset").addEventListener("click", () => routeMap.resetView());
  el("chat-open").addEventListener("click", openChat);
  el("chat-close").addEventListener("click", () => closeSheet("chat-sheet"));
  el("confirm-yes").addEventListener("click", () => answerConfirm(true));
  el("confirm-no").addEventListener("click", () => answerConfirm(false));
  el("geo-notice").addEventListener("click", switchToManualPlace);

  // Переключатель подложки: снимок требует сети, схема работает всегда.
  el("map-toggle").addEventListener("click", () => {
    const toImagery = !routeMap.showImagery;
    routeMap.setImagery(toImagery);
    el("map-toggle").setAttribute("aria-pressed", String(toImagery));
    drawRoute();
  });

  el("geo-mode").addEventListener("click", () => openSheet("geo-sheet"));
  el("geo-close").addEventListener("click", () => closeSheet("geo-sheet"));
  el("work-menu").addEventListener("click", () => openSheet("menu-sheet"));
  el("menu-close").addEventListener("click", () => closeSheet("menu-sheet"));
  el("menu-logout").addEventListener("click", logout);
  el("menu-leave").addEventListener("click", leaveShift);

  for (const button of document.querySelectorAll("#geo-switch button")) {
    button.addEventListener("click", () => setGeoMode(button.dataset.mode));
  }

  for (const backdrop of document.querySelectorAll(".sheet-backdrop")) {
    backdrop.addEventListener("click", (event) => {
      if (event.target === backdrop) {
        backdrop.hidden = true;
      }
    });
  }
}

let sensorHeading = null, lastAlpha = null;
function updateOrientation(event) {
  if (locator?.mode !== "gps" || !state.onShift) return;
  const alpha = event.alpha;
  if (Number.isFinite(event.webkitCompassHeading) && event.webkitCompassHeading >= 0)
    sensorHeading = event.webkitCompassHeading;
  else if (event.absolute && Number.isFinite(alpha)) sensorHeading = 360 - alpha;
  else if (Number.isFinite(alpha) && lastAlpha !== null) {
    const base = sensorHeading ?? state.position?.heading;
    if (Number.isFinite(base)) sensorHeading = (base + lastAlpha - alpha + 360) % 360;
  }
  if (Number.isFinite(alpha)) lastAlpha = alpha;
  if (Number.isFinite(sensorHeading)) routeMap.setHeading(sensorHeading + (screen.orientation?.angle || 0));
}
function enableOrientation() {
  const type = window.DeviceOrientationEvent;
  if (!type) return;
  const attach = () => window.addEventListener("ondeviceorientationabsolute" in window
    ? "deviceorientationabsolute" : "deviceorientation", updateOrientation);
  if (typeof type.requestPermission === "function") {
    if (navigator.userActivation?.isActive) type.requestPermission().then(s => { if (s === "granted") attach(); }).catch(() => {});
  } else attach();
}
function setGeoMode(mode) {
  sensorHeading = lastAlpha = null;
  routeMap.setHeading(null);
  for (const button of document.querySelectorAll("#geo-switch button")) {
    button.classList.toggle("active", button.dataset.mode === mode);
  }

  el("geo-error").hidden = true;

  if (mode === "gps") {
    enableOrientation();
    locator.startGps();
    el("geo-mode").textContent = "GPS";
    el("geo-explain").textContent = "Ваше место определяет приёмник телефона.";
  } else {
    el("geo-mode").textContent = "Ручной";
    el("geo-mode").classList.remove("is-warn");
    el("geo-notice").hidden = true;
    el("geo-explain").textContent = "Нажмите «Готово» и коснитесь карты там, где вы стоите.";
    locator.startManual();
  }
  drawRoute();
}

/**
 * Подтверждение своим листом, а не window.confirm: встроенные браузеры
 * (предпросмотр в редакторе, киоск на стенде) confirm блокируют, и он
 * сразу отвечает «нет» — кнопка «Закрыть вызов» просто не работала.
 */
function askConfirm(question, yesLabel) {
  el("confirm-title").textContent = question;
  el("confirm-yes").textContent = yesLabel;
  openSheet("confirm-sheet");
  return new Promise(rememberConfirm);
}

function rememberConfirm(resolve) {
  state.confirmResolve = resolve;
}

function answerConfirm(answer) {
  closeSheet("confirm-sheet");
  const resolve = state.confirmResolve;
  state.confirmResolve = null;
  if (resolve) {
    resolve(answer);
  }
}

function openSheet(id) {
  el(id).hidden = false;
  // Уведомление висит выше листа и перекрывает его текст, а сообщение
  // об отказе геопозиции лист показывает и сам — убираем дубль.
  hideToast();
  if (id === "menu-sheet" && state.user) {
    el("menu-marks").textContent = state.user.full_name;
  }
}

function closeSheet(id) {
  el(id).hidden = true;
}

/* --- Завершение работы --- */

async function leaveShift() {
  try {
    await Api.setShift(state.user.employee_id, false);
    state.onShift = false;
    Session.user = state.user = { ...state.user, on_shift: false, shift_ends_at: null };
    stopWork();
    closeSheet("menu-sheet");
    showScreen("shift");
    showToast("Вы ушли со смены");
  } catch (error) {
    showToast(error.detail, true);
  }
}

el("extend-shift").onclick = async () => {
  try {
    await Api.setShift(state.user.employee_id, true, true);
    await refresh();
  } catch (error) {
    showToast(error.detail, true);
  }
};

async function logout() {
  updateGuidance(null);
  try {
    await Api.logout();
  } catch (error) {
    if (Session.token) { showToast(error.detail, true); return; }
  }
  state.onShift = false;
  stopWork();
  Session.clear();
  state.returnLeg = null;
  state.user = null;
  state.position = null;
  state.sentAt = 0;
  state.call = null;
  closeSheet("menu-sheet");
  showScreen("login");
}

function stopWork() {
  cancelAnimationFrame(animationFrame);
  displayedPosition = null;
  updateGuidance(null);
  window.removeEventListener("deviceorientationabsolute", updateOrientation);
  window.removeEventListener("deviceorientation", updateOrientation);
  sensorHeading = lastAlpha = null;
  insideSince = 0;
  window.removeEventListener("online", refresh);
  clearInterval(pollTimer);
  clearInterval(countdownTimer);
  clearInterval(heartbeatTimer);
  locator.stopGps();
}

/* --- Мелочи --- */

function formatMinutes(minutes) {
  return minutes >= 10 ? Math.round(minutes) : minutes.toFixed(1);
}

let toastTimer = null;

/** Открыт ли сейчас какой-нибудь выдвижной лист. */
function anySheetOpen() {
  for (const backdrop of document.querySelectorAll(".sheet-backdrop")) {
    if (!backdrop.hidden) {
      return true;
    }
  }
  return false;
}

function hideToast() {
  clearTimeout(toastTimer);
  el("toast").hidden = true;
}

/**
 * Уведомление внизу экрана.
 *
 * Пока открыт лист, уведомление не показывается: оно лежит выше листа
 * и накладывается на его текст, а сообщение об отказе геопозиции лист
 * дублирует внутри себя отдельной строкой.
 */
function showToast(message, isAlert) {
  if (anySheetOpen()) {
    return;
  }

  const toast = el("toast");
  toast.hidden = false;
  toast.textContent = message;
  toast.className = isAlert ? "toast is-alert" : "toast";

  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => {
    toast.hidden = true;
  }, 4000);
}
