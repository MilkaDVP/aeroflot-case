/*
 * Экран администратора: аэропорты, сотрудники, спецтранспорт.
 *
 * Здесь заводят то, что раньше существовало только в тестовых данных:
 * аэропорт (выгрузкой из OpenStreetMap по коду ИКАО или разметкой по точкам),
 * сотрудника с его квалификацией и машину парка.
 *
 * Все проверки — на сервере. Клиент лишь показывает их результат: правило,
 * проверенное только здесь, обошли бы прямым запросом к API.
 */

const state = {
  user: null,
  airports: [],
  // Аэропорт, открытый в редакторе карты.
  editing: null,
  // Граф аэропорта сессии — нужен формам как список мест.
  graph: null,
};

let editorMap = null;
let editor = null;

/* --- Запуск --- */

start();

async function start() {
  if (!Session.token) {
    window.location.href = "index.html";
    return;
  }

  try {
    state.user = await Api.me();
  } catch (error) {
    window.location.href = "index.html";
    return;
  }

  if (state.user.role !== "admin") {
    // Экран доступен только администратору: сервер и так откажет,
    // но показывать заведомо неработающие формы незачем.
    window.location.href = "select-airport.html";
    return;
  }

  el("ctx-user").textContent = state.user.full_name;
  fillAirportSwitch();
  bindEvents();
  await loadAirports();
}

/** Переключатель аэропорта сессии в шапке. */
function fillAirportSwitch() {
  const select = el("ctx-airport");
  select.replaceChildren();

  if (!state.user.airport_icao) {
    const placeholder = document.createElement("option");
    placeholder.value = "";
    placeholder.textContent = "не выбран";
    select.append(placeholder);
  }

  for (const airport of state.user.airports || []) {
    const option = document.createElement("option");
    option.value = airport.icao;
    option.textContent = `${airport.icao} · ${airport.name}`;
    select.append(option);
  }
  select.value = state.user.airport_icao || "";
}

/**
 * Смена аэропорта сессии.
 *
 * Сервер выбирает данные по контексту сессии, а не по параметру запроса:
 * клиент не должен иметь возможности подменить аэропорт. Поэтому
 * переключение — это тот же выбор контекста, что на экране входа.
 */
async function switchAirport() {
  const icao = el("ctx-airport").value;
  if (!icao || icao === state.user.airport_icao) {
    return;
  }

  try {
    await Api.selectAirport(icao, state.user.shift || "day");
    state.user = await Api.me();
    state.graph = null;
    fillAirportSwitch();
    refreshActiveTab();
  } catch (error) {
    window.alert(error.detail);
    fillAirportSwitch();
  }
}

/** Перечитывает открытую вкладку — после смены аэропорта её данные чужие. */
function refreshActiveTab() {
  const active = document.querySelector("#tabs button.active");
  if (active && el("page-editor").hidden) {
    showTab(active.dataset.tab);
  }
}

function el(id) {
  return document.getElementById(id);
}

function bindEvents() {
  for (const button of document.querySelectorAll("#tabs button")) {
    button.addEventListener("click", () => showTab(button.dataset.tab));
  }

  el("logout").addEventListener("click", logout);
  el("ctx-airport").addEventListener("change", switchAirport);
  el("import-form").addEventListener("submit", onImportSubmit);
  el("create-form").addEventListener("submit", onCreateSubmit);
  el("employee-form").addEventListener("submit", onEmployeeSubmit);
  el("vehicle-form").addEventListener("submit", onVehicleSubmit);
  el("aircraft-form").addEventListener("submit", onAircraftSubmit);

  el("editor-back").addEventListener("click", closeEditor);
  el("editor-save").addEventListener("click", saveGraph);
  el("editor-clear").addEventListener("click", clearGraph);
  el("editor-autolink").addEventListener("change", () => {
    if (editor !== null) {
      editor.autolink = el("editor-autolink").checked;
    }
  });

  for (const button of document.querySelectorAll("#editor-tools button")) {
    button.addEventListener("click", () => setEditorMode(button.dataset.mode));
  }
  for (const button of document.querySelectorAll("#editor-basemap button")) {
    button.addEventListener("click", () => setEditorBasemap(button.dataset.basemap));
  }

  el("editor-zoom-in").addEventListener("click", () => editorMap.zoomFromCentre(1.4));
  el("editor-zoom-out").addEventListener("click", () => editorMap.zoomFromCentre(1 / 1.4));
  el("editor-fit").addEventListener("click", () => editorMap.resetView());
}

async function logout() {
  try {
    await Api.logout();
  } finally {
    Session.clear();
    window.location.href = "index.html";
  }
}

/* --- Вкладки --- */

function showTab(name) {
  for (const button of document.querySelectorAll("#tabs button")) {
    button.classList.toggle("active", button.dataset.tab === name);
  }
  el("page-airports").hidden = name !== "airports";
  el("page-employees").hidden = name !== "employees";
  el("page-vehicles").hidden = name !== "vehicles";
  el("page-aircraft").hidden = name !== "aircraft";
  el("page-editor").hidden = true;

  if (name === "employees") {
    loadEmployees();
  } else if (name === "vehicles") {
    loadVehicles();
  } else if (name === "aircraft") {
    loadAircraft();
  }
}

/* --- Аэропорты --- */

async function loadAirports() {
  try {
    state.airports = await Api.adminAirports();
    // Созданный или удалённый аэропорт меняет и список доступных для
    // переключения в шапке.
    state.user = await Api.me();
  } catch (error) {
    el("airports-list").innerHTML = `<div class="error">${escapeHtml(error.detail)}</div>`;
    return;
  }
  fillAirportSwitch();
  renderAirports();
}

function renderAirports() {
  const container = el("airports-list");
  container.replaceChildren();

  if (!state.airports.length) {
    container.innerHTML = '<div class="empty">Аэропортов нет</div>';
    return;
  }

  for (const airport of state.airports) {
    container.append(airportRow(airport));
  }
}

function airportRow(airport) {
  const row = document.createElement("div");
  row.className = "admin-row-item";
  row.innerHTML = `
    <div class="item-main">
      <span class="item-title">${escapeHtml(airport.icao)}</span>
      <span class="item-sub">${escapeHtml(airport.name)}${
        airport.city ? ` · ${escapeHtml(airport.city)}` : ""
      }</span>
    </div>
    <div class="item-meta">
      <span class="badge ${sourceClass(airport.source)}">${sourceTitle(airport.source)}</span>
      <span class="num">${airport.stands}</span> ст. ·
      <span class="num">${airport.tech_centers}</span> ТЦ ·
      <span class="num">${airport.edges}</span> связей
    </div>
  `;

  const actions = document.createElement("div");
  actions.className = "item-actions";

  if (airport.editable) {
    const edit = document.createElement("button");
    edit.type = "button";
    edit.textContent = "Разметить на карте";
    edit.addEventListener("click", () => openEditor(airport));
    actions.append(edit);

    const remove = document.createElement("button");
    remove.type = "button";
    remove.className = "danger";
    remove.textContent = "Удалить";
    remove.addEventListener("click", () => removeAirport(airport));
    actions.append(remove);
  }

  row.append(actions);
  return row;
}

function sourceTitle(source) {
  const titles = { builtin: "в репозитории", osm: "из OSM", manual: "вручную" };
  return titles[source] || source;
}

function sourceClass(source) {
  return source === "builtin" ? "is-builtin" : "is-custom";
}

async function removeAirport(airport) {
  const confirmed = window.confirm(
    `Удалить аэропорт ${airport.icao} (${airport.name})?\n` +
      "Граф будет стёрт. Отменить удаление нельзя."
  );
  if (!confirmed) {
    return;
  }

  try {
    await Api.deleteAirport(airport.icao);
    await loadAirports();
  } catch (error) {
    window.alert(error.detail);
  }
}

async function onImportSubmit(event) {
  event.preventDefault();
  const icao = el("import-icao").value.trim().toUpperCase();
  if (!icao) {
    return;
  }

  // Выгрузка идёт через публичный Overpass и занимает от нескольких секунд
  // до минуты. Без явного сообщения это выглядит как зависшая кнопка.
  setNote("import-note", `Запрашиваю ${icao} у OpenStreetMap…`, "is-pending");
  el("import-submit").disabled = true;

  try {
    const airport = await Api.importAirport(icao, el("import-service-roads").checked);
    setNote(
      "import-note",
      `${airport.name}: ${airport.nodes} точек, ${airport.stands} стоянок, ` +
        `${airport.edges} связей. ${missingForWork(airport)}`,
      airport.stands && airport.tech_centers ? "is-ok" : "is-pending"
    );
    el("import-icao").value = "";
    await loadAirports();
  } catch (error) {
    setNote("import-note", error.detail, "is-error");
  } finally {
    el("import-submit").disabled = false;
  }
}

/**
 * Чего не хватает выгруженному аэропорту для работы.
 *
 * Техцентров в OSM не бывает вовсе, а стоянки размечены не везде
 * (у Пулково их нет ни одной). Без них вызов не на что регистрировать
 * и сотрудникам неоткуда выезжать — это нужно сказать сразу, а не
 * оставить администратора гадать, почему подбор пуст.
 */
function missingForWork(airport) {
  const missing = [];
  if (!airport.stands) {
    missing.push("стоянки");
  }
  if (!airport.tech_centers) {
    missing.push("техцентр");
  }
  if (!missing.length) {
    return "Аэропорт готов к работе.";
  }
  return (
    `В OpenStreetMap нет данных: ${missing.join(" и ")}. ` +
    "Откройте «Разметить на карте» и отметьте их — они сами присоединятся " +
    "к ближайшей рулёжке."
  );
}

async function onCreateSubmit(event) {
  event.preventDefault();

  const payload = {
    icao: el("create-icao").value.trim().toUpperCase(),
    name: el("create-name").value.trim(),
    city: el("create-city").value.trim(),
    lat: Number(el("create-lat").value.replace(",", ".")),
    lon: Number(el("create-lon").value.replace(",", ".")),
  };

  if (!isFinite(payload.lat) || !isFinite(payload.lon)) {
    setNote("create-note", "Координаты введены неверно", "is-error");
    return;
  }

  el("create-submit").disabled = true;
  try {
    const airport = await Api.createAirport(payload);
    setNote("create-note", `${airport.icao} создан. Откройте разметку на карте.`, "is-ok");
    el("create-icao").value = "";
    el("create-name").value = "";
    await loadAirports();
    openEditor(airport);
  } catch (error) {
    setNote("create-note", error.detail, "is-error");
  } finally {
    el("create-submit").disabled = false;
  }
}

/* --- Редактор карты --- */

async function openEditor(airport) {
  state.editing = airport;
  el("page-airports").hidden = true;
  el("page-editor").hidden = false;
  el("editor-title").textContent = `${airport.icao} · ${airport.name}`;

  if (editorMap === null) {
    editorMap = new AirportMap(el("editor-map"));
    editorMap.attachTiles(showEditorNotice);
    editor = new GraphEditor(editorMap, updateEditorCounts);
  }

  try {
    const graph = await Api.airportGraph(airport.icao);
    // Карта задаёт только вид и подложку, точки рисует редактор из своей
    // рабочей копии: рисунок меняется сразу, не дожидаясь сохранения.
    editorMap.frame(graph);
    editorMap.setBasemap("satellite");
    el("editor-credit").textContent = editorMap.tiles.credit;
    editor.load(graph);
    updateEditorCounts(editor.counts());
    setEditorMode("stand");
  } catch (error) {
    window.alert(error.detail);
    closeEditor();
  }
}

function closeEditor() {
  state.editing = null;
  el("page-editor").hidden = true;
  el("page-airports").hidden = false;
}

function setEditorMode(mode) {
  // Кнопки редактора остаются в разметке и когда он закрыт: нажатие
  // по ним не должно ронять страницу.
  if (editor === null) {
    return;
  }
  for (const button of document.querySelectorAll("#editor-tools button")) {
    button.classList.toggle("active", button.dataset.mode === mode);
  }
  editor.setMode(mode);
  el("editor-hint").textContent = hintFor(mode);
}

function hintFor(mode) {
  const hints = {
    stand:
      "Кликните по карте, чтобы поставить стоянку, или по серой точке сети, " +
      "чтобы сделать стоянкой её.",
    tech_center:
      "Кликните по карте или по серой точке, чтобы отметить техцентр — " +
      "откуда выезжают инженеры.",
    junction: "Кликните по карте, чтобы поставить перекрёсток рулёжек.",
    link: "Кликайте по точкам подряд: каждая пара соединяется рулёжкой.",
    erase: "Кликните по точке или связи, чтобы удалить её.",
  };
  return hints[mode] || "";
}

function updateEditorCounts(counts) {
  el("editor-counts").textContent =
    `точек ${counts.nodes} · стоянок ${counts.stands} · ` +
    `техцентров ${counts.tech} · связей ${counts.edges}`;
}

function setEditorBasemap(key) {
  for (const button of document.querySelectorAll("#editor-basemap button")) {
    button.classList.toggle("active", button.dataset.basemap === key);
  }
  editorMap.setBasemap(key);
  el("editor-credit").textContent = editorMap.tiles.credit;
  if (key === "scheme") {
    showEditorNotice(null);
  }
}

function showEditorNotice(message) {
  el("editor-notice").hidden = !message;
  el("editor-notice").textContent = message || "";
}

async function saveGraph() {
  if (editor === null || state.editing === null) {
    return;
  }

  const payload = editor.payload();
  el("editor-save").disabled = true;

  try {
    const airport = await Api.saveAirportGraph(
      state.editing.icao,
      payload.nodes,
      payload.edges
    );
    await loadAirports();
    // Списки мест во вкладках сотрудников и бортов строятся по графу;
    // после сохранения разметки прежний граф устарел.
    if (state.graph !== null && state.graph.icao === airport.icao) {
      state.graph = null;
    }
    // Перечитываем граф с сервера: длины рёбер и метровые координаты
    // считает он, и в редакторе должно быть ровно то, что сохранено.
    const graph = await Api.airportGraph(airport.icao);
    editorMap.frame(graph);
    editor.load(graph);
    window.alert(
      `Сохранено: ${airport.nodes} точек, ${airport.stands} стоянок, ` +
        `${airport.edges} связей.`
    );
  } catch (error) {
    window.alert(error.detail);
  } finally {
    el("editor-save").disabled = false;
  }
}

function clearGraph() {
  if (editor === null) {
    return;
  }
  if (window.confirm("Убрать все точки с карты? Сохранённый граф останется прежним.")) {
    editor.clear();
  }
}

/* --- Сотрудники --- */

async function loadEmployees() {
  el("employees-airport").textContent = state.user.airport_icao || "—";

  if (!state.user.airport_icao) {
    el("employees-list").innerHTML = noAirportMessage();
    return;
  }

  try {
    await loadGraphOnce();
    fillPlaces("emp-place");
    const employees = await Api.employees();
    renderEmployees(employees);
  } catch (error) {
    el("employees-list").innerHTML = `<div class="error">${escapeHtml(error.detail)}</div>`;
  }
}

function renderEmployees(employees) {
  const container = el("employees-list");
  container.replaceChildren();

  if (!employees.length) {
    container.innerHTML = '<div class="empty">Сотрудников нет</div>';
    return;
  }

  for (const employee of employees) {
    const marks = employee.qualifications
      .map((item) => `${item.category} (${item.aircraft_types.join(", ")})`)
      .join("; ");

    const row = document.createElement("div");
    row.className = "admin-row-item";
    row.innerHTML = `
      <div class="item-main">
        <span class="item-title">${escapeHtml(employee.full_name)}</span>
        <span class="item-sub">${escapeHtml(marks || "без отметок")}</span>
      </div>
      <div class="item-meta">
        ${employee.shift === "day" ? "дневная" : "ночная"} ·
        ${escapeHtml(statusTitle(employee.status))}
      </div>
    `;
    container.append(row);
  }
}

function statusTitle(status) {
  const titles = {
    free: "свободен",
    assigned: "назначен",
    en_route: "в пути",
    busy: "занят",
    offline: "не на смене",
  };
  return titles[status] || status;
}

async function onEmployeeSubmit(event) {
  event.preventDefault();

  const place = selectedPlace("emp-place");
  if (place === null) {
    setNote("employee-note", placeError(), "is-error");
    return;
  }

  const types = el("emp-types")
    .value.split(",")
    .map((item) => item.trim())
    .filter((item) => item.length > 0);

  if (!types.length) {
    setNote("employee-note", "Укажите хотя бы один тип ВС", "is-error");
    return;
  }

  const payload = {
    full_name: el("emp-name").value.trim(),
    airport_icao: state.user.airport_icao,
    shift: el("emp-shift").value,
    qualifications: [
      {
        category: el("emp-mark").value,
        aircraft_types: types,
        valid_until: el("emp-valid").value.trim(),
      },
    ],
    lat: place.lat,
    lon: place.lon,
    on_shift: el("emp-on-shift").checked,
    login: el("emp-login").value.trim() || null,
    password: el("emp-password").value || null,
  };

  el("employee-submit").disabled = true;
  try {
    const employee = await Api.createEmployee(payload);
    const access = payload.login
      ? ` Вход в приложение инженера: ${payload.login}.`
      : "";
    setNote("employee-note", `${employee.full_name} зарегистрирован.${access}`, "is-ok");
    el("emp-name").value = "";
    el("emp-login").value = "";
    el("emp-password").value = "";
    await loadEmployees();
  } catch (error) {
    setNote("employee-note", error.detail, "is-error");
  } finally {
    el("employee-submit").disabled = false;
  }
}

/* --- Спецтранспорт --- */

async function loadVehicles() {
  el("vehicles-airport").textContent = state.user.airport_icao || "—";

  if (!state.user.airport_icao) {
    el("vehicles-list").innerHTML = noAirportMessage();
    return;
  }

  try {
    await loadGraphOnce();
    fillPlaces("veh-place");
    const vehicles = await Api.vehicles();
    renderVehicles(vehicles);
  } catch (error) {
    el("vehicles-list").innerHTML = `<div class="error">${escapeHtml(error.detail)}</div>`;
  }
}

function renderVehicles(vehicles) {
  const container = el("vehicles-list");
  container.replaceChildren();

  if (!vehicles.length) {
    container.innerHTML = '<div class="empty">Машин нет</div>';
    return;
  }

  for (const vehicle of vehicles) {
    const row = document.createElement("div");
    row.className = "admin-row-item";
    row.innerHTML = `
      <div class="item-main">
        <span class="item-title">${escapeHtml(vehicle.call_sign)}</span>
        <span class="item-sub">${escapeHtml(vehicle.kind)}</span>
      </div>
      <div class="item-meta">
        <span class="badge ${vehicle.status === "free" ? "is-ok" : "is-busy"}">
          ${escapeHtml(vehicleStatus(vehicle.status))}
        </span>
        ${vehicle.employee_name ? escapeHtml(vehicle.employee_name) : ""}
      </div>
    `;

    const actions = document.createElement("div");
    actions.className = "item-actions";
    const remove = document.createElement("button");
    remove.type = "button";
    remove.className = "danger";
    remove.textContent = "Вывести из парка";
    remove.disabled = vehicle.status !== "free";
    remove.addEventListener("click", () => removeVehicle(vehicle));
    actions.append(remove);
    row.append(actions);

    container.append(row);
  }
}

function vehicleStatus(status) {
  const titles = { free: "свободна", reserved: "зарезервирована", in_use: "в работе" };
  return titles[status] || status;
}

async function removeVehicle(vehicle) {
  if (!window.confirm(`Вывести ${vehicle.call_sign} из парка?`)) {
    return;
  }
  try {
    await Api.deleteVehicle(vehicle.id);
    await loadVehicles();
  } catch (error) {
    window.alert(error.detail);
  }
}

async function onVehicleSubmit(event) {
  event.preventDefault();

  const place = selectedPlace("veh-place");
  if (place === null) {
    setNote("vehicle-note", placeError(), "is-error");
    return;
  }

  el("vehicle-submit").disabled = true;
  try {
    const vehicle = await Api.createVehicle({
      call_sign: el("veh-sign").value.trim(),
      kind: el("veh-kind").value.trim(),
      lat: place.lat,
      lon: place.lon,
    });
    setNote("vehicle-note", `${vehicle.call_sign} поставлена в парк`, "is-ok");
    el("veh-sign").value = "";
    await loadVehicles();
  } catch (error) {
    setNote("vehicle-note", error.detail, "is-error");
  } finally {
    el("vehicle-submit").disabled = false;
  }
}

/* --- Борта --- */

async function loadAircraft() {
  el("aircraft-airport").textContent = state.user.airport_icao || "—";

  if (!state.user.airport_icao) {
    el("aircraft-list").innerHTML = noAirportMessage();
    return;
  }

  try {
    await loadGraphOnce();
    fillStands("ac-stand");
    await fillAircraftTypes();
    const aircraft = await Api.aircraft();
    renderAircraft(aircraft);
  } catch (error) {
    el("aircraft-list").innerHTML = `<div class="error">${escapeHtml(error.detail)}</div>`;
  }
}

/**
 * Типы ВС из справочника сервера.
 *
 * Список не зашит в разметку: по типу определяется подкатегория допуска,
 * и расхождение со справочником дало бы борт, на который никого нельзя
 * назначить.
 */
async function fillAircraftTypes() {
  const select = el("ac-type");
  if (select.options.length) {
    return;
  }
  const types = await Api.aircraftTypes();
  for (const type of Object.keys(types).sort()) {
    const option = document.createElement("option");
    option.value = type;
    option.textContent = type;
    select.append(option);
  }
  select.value = "A320";
}

/** Только стоянки: борт на перекрёсток или в техцентр не ставится. */
function fillStands(selectId) {
  const select = el(selectId);
  select.replaceChildren();

  const stands = (state.graph.nodes || []).filter((node) => node.type === "stand");
  stands.sort(placeOrder);

  for (const node of stands) {
    const option = document.createElement("option");
    option.value = node.id;
    option.textContent = `Стоянка ${node.ref || node.id}`;
    select.append(option);
  }
}

function renderAircraft(aircraft) {
  const container = el("aircraft-list");
  container.replaceChildren();

  if (!aircraft.length) {
    container.innerHTML = '<div class="empty">Бортов нет</div>';
    return;
  }

  for (const item of aircraft) {
    const row = document.createElement("div");
    row.className = "admin-row-item";
    row.innerHTML = `
      <div class="item-main">
        <span class="item-title">${escapeHtml(item.board_number)}</span>
        <span class="item-sub">${escapeHtml(item.aircraft_type)}</span>
      </div>
      <div class="item-meta">стоянка <span class="num">${escapeHtml(
        item.stand_ref || "—"
      )}</span></div>
    `;

    const actions = document.createElement("div");
    actions.className = "item-actions";
    const remove = document.createElement("button");
    remove.type = "button";
    remove.className = "danger";
    remove.textContent = "Убрать";
    remove.addEventListener("click", () => removeAircraft(item));
    actions.append(remove);
    row.append(actions);

    container.append(row);
  }
}

async function removeAircraft(item) {
  if (!window.confirm(`Убрать борт ${item.board_number} со стоянки?`)) {
    return;
  }
  try {
    await Api.deleteAircraft(item.id);
    await loadAircraft();
  } catch (error) {
    window.alert(error.detail);
  }
}

async function onAircraftSubmit(event) {
  event.preventDefault();

  if (state.graph === null || !el("ac-stand").value) {
    setNote("aircraft-note", "В аэропорту нет стоянок: сначала разметьте их на карте", "is-error");
    return;
  }

  el("aircraft-submit").disabled = true;
  try {
    const item = await Api.createAircraft({
      board_number: el("ac-board").value.trim(),
      aircraft_type: el("ac-type").value,
      stand_node_id: el("ac-stand").value,
    });
    setNote(
      "aircraft-note",
      `${item.board_number} (${item.aircraft_type}) на стоянке ${item.stand_ref}`,
      "is-ok"
    );
    el("ac-board").value = "";
    await loadAircraft();
  } catch (error) {
    setNote("aircraft-note", error.detail, "is-error");
  } finally {
    el("aircraft-submit").disabled = false;
  }
}

/* --- Общее --- */

async function loadGraphOnce() {
  if (state.graph === null || state.graph.icao !== state.user.airport_icao) {
    state.graph = await Api.airportGraph(state.user.airport_icao);
  }
  return state.graph;
}

/**
 * Список мест для форм: техцентры и стоянки аэропорта.
 *
 * Координаты не вводят руками — их берут из графа. Точка, набранная
 * вручную, легко оказывается в стороне от рулёжной сети, и маршрут
 * к такому сотруднику не построится.
 */
function fillPlaces(selectId) {
  const select = el(selectId);
  const chosen = select.value;
  select.replaceChildren();

  const places = (state.graph.nodes || []).filter(
    (node) => node.type === "tech_center" || node.type === "stand"
  );
  places.sort(placeOrder);

  for (const node of places) {
    const option = document.createElement("option");
    option.value = node.id;
    option.textContent =
      node.type === "tech_center"
        ? `Техцентр ${node.ref || node.id}`
        : `Стоянка ${node.ref || node.id}`;
    select.append(option);
  }

  if (chosen) {
    select.value = chosen;
  }
}

/** Техцентры в начале списка: сотрудников заводят прежде всего в них. */
function placeOrder(left, right) {
  if (left.type !== right.type) {
    return left.type === "tech_center" ? -1 : 1;
  }
  return String(left.ref || "").localeCompare(String(right.ref || ""), "ru", {
    numeric: true,
  });
}

function selectedPlace(selectId) {
  // Графа может не быть вовсе: аэропорт не выбран или в нём ещё нет точек.
  // Тогда выбирать место не из чего, и форма должна сказать об этом,
  // а не падать на пустом объекте.
  if (state.graph === null) {
    return null;
  }
  const nodeId = el(selectId).value;
  const node = (state.graph.nodes || []).find((item) => item.id === nodeId);
  return node ? { lat: node.lat, lon: node.lon } : null;
}

/** Почему место выбрать не удалось — причина зависит от состояния. */
function placeError() {
  if (!state.user.airport_icao) {
    return "Аэропорт не выбран: откройте выбор аэропорта и вернитесь сюда";
  }
  if (state.graph === null || !(state.graph.nodes || []).length) {
    return "В аэропорту нет ни одной точки: сначала разметьте его на карте";
  }
  return "Выберите место из списка";
}

function noAirportMessage() {
  return (
    '<div class="empty">Аэропорт не выбран. ' +
    '<a href="select-airport.html">Выберите его</a>, ' +
    "чтобы управлять сотрудниками и машинами.</div>"
  );
}

function setNote(id, message, className) {
  const note = el(id);
  note.hidden = false;
  note.className = `admin-note ${className || ""}`;
  note.textContent = message;
}

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
