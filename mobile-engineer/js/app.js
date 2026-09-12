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
  // Последние отправленные координаты: чтобы не слать одно и то же.
  sentAt: 0,
  onShift: false,
  // Удалось ли включить офлайн-оболочку.
  offlineReady: false,
  // Опорная точка аэропорта: на неё центрируется карта, пока нет
  // ни вызова, ни координат.
  airportPoint: null,
};

let locator = null;
let routeMap = null;
let pollTimer = null;
let countdownTimer = null;

// Действие инженера: что показывает кнопка и что уходит на сервер.
// Порядок в жизненном цикле вызова: назначен -> принял -> прибыл -> завершил.
const ACTIONS = {
  assigned: { label: "Принять вызов", action: "accepted" },
  accepted: { label: "Я на месте", action: "arrived" },
  arrived: { label: "Работы завершены", action: "closed" },
};

const el = (id) => document.getElementById(id);

/* --- Запуск --- */

start();

async function start() {
  registerServiceWorker();
  routeMap = new RouteMap(el("route-map"), onMapTap);
  locator = new Locator(onPosition, onGeoError);
  bindEvents();

  if (!Session.token) {
    showScreen("login");
    return;
  }

  try {
    const user = await Api.me();
    Session.user = user;
    state.user = user;
    afterLogin(user);
  } catch (error) {
    Session.clear();
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

  // Опорная точка аэропорта нужна карте до появления координат.
  const airport = (user.airports || []).find(
    (item) => item.icao === user.airport_icao
  ) || (user.airports || [])[0];
  // Центр рабочей зоны, а не опорная точка проекции: та смещена к краю
  // аэродрома, и без координат человек увидел бы забор вместо перрона.
  state.airportPoint = airport ? airport.centre || airport.ref_point : null;

  showScreen("shift");
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
  showScreen("work");
  locator.startGps();
  refresh();
  pollTimer = setInterval(refresh, CONFIG.pollIntervalMs);
  countdownTimer = setInterval(updateCountdown, 1000);
}

/* --- Опрос вызова --- */

async function refresh() {
  try {
    const payload = await Api.currentCall();
    // Сервер отдаёт либо вызов с маршрутом, либо сообщение «вызовов нет».
    state.call = payload && payload.call ? payload : null;
    renderWork();
  } catch (error) {
    if (error.status === 401) {
      logout();
      return;
    }
    showToast(error.detail, true);
  }
}

function renderWork() {
  const hasCall = Boolean(state.call);
  el("idle-block").hidden = hasCall;
  el("call-block").hidden = !hasCall;
  el("call-action").hidden = !hasCall;

  // Карта рисуется в любом случае: без вызова она показывает, где стоит
  // сам инженер, и позволяет поставить точку в режиме имитации.
  drawRoute();

  if (!hasCall) {
    renderIdle();
    return;
  }

  const call = state.call.call;
  el("call-stand").textContent = call.stand_ref || "—";
  el("call-board").textContent = call.board_number;
  el("call-type").textContent = call.aircraft_type;
  el("call-defect").textContent = call.defect_name;
  el("call-mark").textContent = call.required_mark;

  // Инженер должен знать, что после закрытия его не отпустят, а сразу
  // отправят к следующему борту.
  const queued = state.call.queued_count || 0;
  el("call-queue").hidden = queued === 0;
  el("call-queue").textContent = queued ? `После этого вызова ещё в очереди: ${queued}` : "";

  // Машина — главное указание в пути: без подсказки инженер пойдёт
  // пешком и опоздает, хотя маршрут посчитан через машину.
  const vehicle = state.call.vehicle;
  el("call-vehicle").hidden = !vehicle;
  el("call-vehicle").textContent = vehicleInstruction(vehicle);
  el("call-eta").textContent =
    call.eta_minutes === null ? "—" : `${formatMinutes(call.eta_minutes)} мин`;

  const step = ACTIONS[call.status];
  el("call-action").textContent = step ? step.label : "Ожидание";
  el("call-action").disabled = !step;

  el("work-status").textContent = statusTitle(call.status);
  el("work-status-dot").className = "status-dot busy";

  updateDistance();
  updateCountdown();
}

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
  el("work-status").textContent = state.onShift ? "на смене, свободен" : "на смене";
  el("work-status-dot").className = "status-dot";
  el("idle-coords").textContent = state.position
    ? `${state.position.lat.toFixed(5)}, ${state.position.lon.toFixed(5)}`
    : "не определены";
}

function statusTitle(status) {
  const titles = {
    assigned: "назначен вызов",
    accepted: "в пути",
    arrived: "на месте",
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
  const deadline =
    new Date(call.created_at).getTime() + CONFIG.regulationLimitMin * 60000;
  const leftMin = (deadline - Date.now()) / 60000;

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

  block.className = leftMin <= 3 ? "deadline is-warn" : "deadline";
  value.textContent = Math.floor(leftMin);
  label.textContent = "до контрольного срока";
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
  const points = state.call ? state.call.route_points : null;
  return points && points.length ? points[points.length - 1] : null;
}

/**
 * Отрисовка карты.
 *
 * Работает и без вызова: тогда рисуется только своя точка, а если
 * координат ещё нет — карта центрируется на аэропорте, чтобы человеку
 * было куда ткнуть пальцем в режиме имитации.
 */
function drawRoute() {
  const points = state.call ? state.call.route_points : [];
  routeMap.render(
    points,
    targetPoint(),
    state.position,
    state.airportPoint,
    state.call ? state.call.vehicle : null,
    state.call ? state.call.call.pickup_node_id : null
  );
  el("map-hint").hidden = locator.mode !== "simulation";
}

/* --- Действия инженера --- */

el("call-action").addEventListener("click", async () => {
  const call = state.call && state.call.call;
  const step = call && ACTIONS[call.status];
  if (!step) {
    return;
  }

  el("call-action").disabled = true;
  el("call-error").hidden = true;

  try {
    await Api.setStatus(step.action);
    const hadQueue = step.action === "closed" && (state.call.queued_count || 0) > 0;
    if (step.action === "closed") {
      state.call = null;
    }
    await refresh();
    if (step.action === "closed") {
      // Сообщение по факту ответа сервера, а не по предположению: при
      // очереди следующий вызов уже пришёл, и «вы свободны» было бы ложью.
      showToast(hadQueue && state.call ? "Вызов закрыт. Следующий вызов из очереди" : "Вызов закрыт. Вы снова свободны");
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
function onPosition(position) {
  state.position = position;
  updateDistance();
  drawRoute();
  if (!state.call) {
    renderIdle();
  }

  const now = Date.now();
  if (now - state.sentAt >= CONFIG.locationIntervalMs) {
    state.sentAt = now;
    Api.sendLocation(state.user.employee_id, position.lat, position.lon).catch(() => {
      // Молча: связь на перроне пропадает, следующая отправка пройдёт.
    });
  }
}

function onGeoError(message) {
  showToast(message, true);
  el("geo-error").hidden = false;
  el("geo-error").textContent = message;
}

/**
 * Касание по карте в режиме имитации задаёт координаты.
 *
 * В режиме GPS касание игнорируется молча было бы непонятно, поэтому
 * приложение объясняет, почему точка не переставилась.
 */
function onMapTap(point) {
  if (locator.mode !== "simulation") {
    showToast("Чтобы ставить точку вручную, включите режим имитации");
    return;
  }
  applySimulated(point.lat, point.lon);
}

function applySimulated(lat, lon) {
  locator.simulate(lat, lon);
  el("geo-lat").value = lat.toFixed(6);
  el("geo-lon").value = lon.toFixed(6);
  // Имитация — рабочий режим для демонстрации, поэтому координаты
  // уходят на сервер немедленно, без ожидания интервала.
  state.sentAt = 0;
  onPosition({ lat, lon, accuracy: null });
}

function bindEvents() {
  // Переключатель подложки: снимок требует сети, схема работает всегда.
  el("map-toggle").addEventListener("click", () => {
    const toImagery = el("map-toggle").textContent === "Спутник";
    routeMap.setImagery(toImagery);
    el("map-toggle").textContent = toImagery ? "Схема" : "Спутник";
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

  el("geo-apply").addEventListener("click", () => {
    const lat = Number(el("geo-lat").value.replace(",", "."));
    const lon = Number(el("geo-lon").value.replace(",", "."));
    if (!isFinite(lat) || !isFinite(lon) || Math.abs(lat) > 90 || Math.abs(lon) > 180) {
      el("geo-error").hidden = false;
      el("geo-error").textContent = "Координаты вне допустимых пределов";
      return;
    }
    el("geo-error").hidden = true;
    applySimulated(lat, lon);
    closeSheet("geo-sheet");
  });

  for (const backdrop of document.querySelectorAll(".sheet-backdrop")) {
    backdrop.addEventListener("click", (event) => {
      if (event.target === backdrop) {
        backdrop.hidden = true;
      }
    });
  }
}

function setGeoMode(mode) {
  for (const button of document.querySelectorAll("#geo-switch button")) {
    button.classList.toggle("active", button.dataset.mode === mode);
  }

  el("geo-manual").hidden = mode !== "simulation";
  el("geo-error").hidden = true;

  if (mode === "gps") {
    locator.startGps();
    el("geo-mode").textContent = "GPS";
    el("geo-explain").textContent = "Координаты берутся с приёмника телефона.";
  } else {
    el("geo-mode").textContent = "Имитация";
    el("geo-explain").textContent =
      "Координаты задаются вручную. Реальный GPS по условиям задания " +
      "не обязателен, и этот режим используется для демонстрации.";
    // Отталкиваемся от последней известной точки, чтобы не вводить
    // координаты Шереметьево с нуля.
    const base = state.position || (state.call ? targetPoint() : null);
    if (base) {
      el("geo-lat").value = base.lat.toFixed(6);
      el("geo-lon").value = base.lon.toFixed(6);
      locator.simulate(base.lat, base.lon);
    }
  }

  if (state.call) {
    drawRoute();
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
    stopWork();
    closeSheet("menu-sheet");
    showScreen("shift");
    showToast("Вы ушли со смены");
  } catch (error) {
    showToast(error.detail, true);
  }
}

async function logout() {
  stopWork();
  try {
    await Api.logout();
  } catch (error) {
    // Токен всё равно стираем: остаться в приложении с мёртвой сессией хуже.
  }
  Session.clear();
  state.user = null;
  state.call = null;
  closeSheet("menu-sheet");
  showScreen("login");
}

function stopWork() {
  clearInterval(pollTimer);
  clearInterval(countdownTimer);
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
