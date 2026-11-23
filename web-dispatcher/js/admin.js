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
  // Кого правят в форме «Учётные записи»: { kind: "account", user } или
  // { kind: "engineer", employee, account }; null — форма заводит нового.
  editingUser: null,
  // Последний загруженный список людей.
  people: { users: [], employees: [] },
};

/*
 * Что видно на экране по ролям:
 *   начальник смены — учётные записи: сеансы младших, у инженеров —
 *                     смена, место, присутствие на смене; расписание смен;
 *   администратор аэропорта — плюс заведение и правка учётных записей
 *                     и инженеров с квалификацией, машины и борта;
 *   технический администратор — плюс аэропорты системы и любой аэропорт.
 */
const TAB_MIN_ROLE = {
  airports: "admin",
  users: "shift_supervisor",
  shifts: "shift_supervisor",
  vehicles: "local_admin",
  aircraft: "local_admin",
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

  if (!roleAtLeast(state.user.role, "shift_supervisor")) {
    // Экран доступен начальнику смены и старше: сервер и так откажет,
    // но показывать заведомо неработающие формы незачем.
    window.location.href = "select-airport.html";
    return;
  }

  el("ctx-user").textContent = `${state.user.full_name} · ${roleTitle(state.user.role)}`;
  fillAirportSwitch();
  bindEvents();

  for (const button of document.querySelectorAll("#tabs button")) {
    const minimum = TAB_MIN_ROLE[button.dataset.tab];
    button.hidden = minimum === "admin" ? state.user.role !== "admin" : !roleAtLeast(state.user.role, minimum);
  }

  // Аэропорты системы заводит только технический администратор.
  // Остальные работают в своём аэропорту.
  if (state.user.role !== "admin") {
    el("ctx-airport").disabled = true;
    showTab("users");
    return;
  }
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
  el("people-search").addEventListener("input", () => renderPeople(state.people.users, state.people.employees));
  for (const button of document.querySelectorAll("#tabs button")) {
    button.addEventListener("click", () => showTab(button.dataset.tab));
  }

  el("back-to-dispatch").addEventListener("click", async (event) => {
    if (state.user.airport_icao && state.user.shift) return;
    event.preventDefault();
    const icao = state.user.airport_icao || state.user.airports[0]?.icao;
    if (!icao) return window.alert("Нет доступного аэропорта");
    try {
      await Api.selectAirport(icao, state.user.shift || "day");
      window.location.href = "dispatch.html";
    } catch (error) {
      window.alert(error.detail);
    }
  });
  el("logout").addEventListener("click", logout);
  el("ctx-airport").addEventListener("change", switchAirport);
  el("import-form").addEventListener("submit", onImportSubmit);
  el("create-form").addEventListener("submit", onCreateSubmit);
  el("user-form").addEventListener("submit", onUserSubmit);
  el("user-cancel").addEventListener("click", cancelUserEdit);
  el("user-role").addEventListener("change", syncUserForm);
  el("shifts-form").addEventListener("submit", onShiftsSubmit);
  el("vehicle-form").addEventListener("submit", onVehicleSubmit);
  el("aircraft-form").addEventListener("submit", onAircraftSubmit);

  el("editor-back").addEventListener("click", closeEditor);
  el("editor-save").addEventListener("click", saveGraph);
  el("editor-clear").addEventListener("click", clearGraph);

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
  el("page-users").hidden = name !== "users";
  el("page-shifts").hidden = name !== "shifts";
  el("page-vehicles").hidden = name !== "vehicles";
  el("page-aircraft").hidden = name !== "aircraft";
  el("page-editor").hidden = true;

  if (name === "users") {
    loadUsers();
  } else if (name === "shifts") {
    loadShifts();
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
    erase:
      "Наведите на точку или связь — она покраснеет — и щёлкните, чтобы удалить именно её. " +
      "Точка удаляется вместе со своими связями, связь — одна, точки остаются.",
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

/* --- Учётные записи и сотрудники --- */

/*
 * Одна вкладка на всех людей аэропорта. Диспетчеры, начальники смены
 * и администраторы — это только учётные записи. Инженер — ещё и карточка
 * сотрудника (квалификация, смена, место), по которой идёт подбор; вход
 * в приложение у него необязателен. Поэтому в списке инженер — одна
 * строка, а в форме при роли «инженер» появляются поля карточки.
 */

// Роли учётных записей в порядке старшинства — для выбора в форме.
const ACCOUNT_ROLES = ["admin", "local_admin", "shift_supervisor", "dispatcher"];
const ENGINEER = "engineer";
const ENGINEER_CARD_FIELDS = ["user-name", "user-mark", "user-types", "user-valid"];

/** Роли, которые текущий пользователь вправе назначать: ниже своей. */
function assignableRoles() {
  const own = state.user.role;
  const roles =
    own === "admin"
      ? ACCOUNT_ROLES
      : ACCOUNT_ROLES.filter((role) => (ROLE_RANK[role] || 0) < (ROLE_RANK[own] || 0));
  // Инженера заводит администратор аэропорта и старше.
  return canManageAccounts() ? [...roles, ENGINEER] : roles;
}

function canManageAccounts() {
  return roleAtLeast(state.user.role, "local_admin");
}

async function loadUsers() {
  el("users-scope").textContent =
    state.user.role === "admin" ? "· вся система" : `· ${state.user.airport_icao || "—"}`;
  try {
    const users = await Api.users();
    // Карточки сотрудников есть только в выбранном аэропорту.
    const employees = await Api.adminRoster();
    if (state.user.airport_icao) {
      // Весь состав аэропорта, а не только смена текущей сессии:
      // в учётных записях нужны все люди.

      await loadGraphOnce();
      fillPlaces("user-place");
    }
    state.people = { users, employees };
    renderPeople(users, employees);
  } catch (error) {
    el("users-list").innerHTML = `<div class="error">${escapeHtml(error.detail)}</div>`;
  }
  if (state.editingUser === null) {
    resetUserForm();
  }
}

/**
 * Список: сначала учётные записи по старшинству, затем инженеры.
 * Учётная запись инженера показывается в строке его карточки.
 */
function renderPeople(users, employees) {
  const container = el("users-list");
  container.replaceChildren();

  const accountOf = new Map(
    users.filter((user) => user.employee_id).map((user) => [user.employee_id, user])
  );
  const airport = state.user.airport_icao;
  const terms = el("people-search").value.toLocaleLowerCase().trim().split(/\s+/);
  function matches(icao, fields) {
    const text = fields.join(" ").toLocaleLowerCase();
    return (!airport || airport === icao) && terms.every(term => text.includes(term));
  }
  const cardIds = new Set(employees.map((employee) => employee.id));
  const accounts = users.filter(
    (user) => !(user.role === ENGINEER && cardIds.has(user.employee_id)) && matches(user.airport_icao, [user.login, user.full_name, user.role, roleTitle(user.role)])
  );

  employees = employees.filter(e => matches(e.airport_icao, [e.full_name, accountOf.get(e.id)?.login,
    roleTitle(ENGINEER), ENGINEER, e.shift, ...e.qualifications.flatMap(q => [q.category, ...q.aircraft_types, q.valid_until])]));
  if (!accounts.length && !employees.length) {
    container.innerHTML = '<div class="empty">Никого нет</div>';
    return;
  }
  for (const user of accounts) {
    container.append(accountRow(user));
  }
  for (const employee of employees) {
    container.append(engineerRow(employee, accountOf.get(employee.id) || null));
  }
}

function personRow({ online, title, sub, role, meta }) {
  const row = document.createElement("div");
  row.className = "admin-row-item";
  row.innerHTML = `
    <div class="item-main">
      <span class="item-title">
        <span class="online-dot ${online ? "is-online" : ""}"
              title="${online ? "в системе" : "не в системе"}"></span>
        ${escapeHtml(title)}
      </span>
      <span class="item-sub">${escapeHtml(sub)}</span>
    </div>
    <div class="item-meta">
      ${meta ? `<span class="muted small">${escapeHtml(meta)}</span>` : ""}
      <span class="badge role-badge is-${escapeHtml(role)}">${escapeHtml(roleTitle(role))}</span>
    </div>
  `;
  const actions = document.createElement("div");
  actions.className = "item-actions";
  row.append(actions);
  return { row, actions };
}

function accountRow(user) {
  const { row, actions } = personRow({
    online: user.online,
    title: user.full_name,
    sub: `${user.login} · ${user.airport_icao || "все аэропорты"}`,
    role: user.role,
  });
  if (user.can_logout) {
    actions.append(actionButton("Завершить сеанс", "", () => logoutAccount(user)));
  }
  if (user.can_edit && canManageAccounts()) {
    if (user.role === ENGINEER) {
      // Инженер правится вместе с карточкой (квалификация, допуски), а она
      // есть только в аэропорту сессии. Сюда попадают инженеры, чья
      // карточка не загружена: аэропорт не выбран или он другой.
      actions.append(actionButton("Изменить", "", async () => {
        try {
          await Api.selectAirport(user.airport_icao, state.user.shift || "day");
          state.user = await Api.me();
          state.graph = null;
          fillAirportSwitch();
          await loadUsers();
          const employee = state.people.employees.find(item => item.id === user.employee_id);
          if (employee) startEngineerEdit(employee, user);
        } catch (error) {
          window.alert(error.detail);
        }
      }));
    } else {
      actions.append(actionButton("Изменить", "", () => startAccountEdit(user)));
    }
    actions.append(actionButton("Удалить", "danger", () => removeAccount(user)));
  }
  return row;
}

function engineerRow(employee, account) {
  const marks = employee.qualifications
    .map((item) => `${item.category} (${item.aircraft_types.join(", ")})`)
    .join("; ");
  const { row, actions } = personRow({
    online: Boolean(account && account.online),
    title: employee.full_name,
    sub: `${employee.airport_icao} · ${account ? account.login : "без входа в приложение"} · ${marks || "без отметок"}`,
    role: ENGINEER,
    meta: `${SHIFT_NAMES[employee.shift] || employee.shift} · ${statusTitle(employee.status)}`,
  });
  if (account && account.can_logout) {
    actions.append(actionButton("Завершить сеанс", "", () => logoutAccount(account)));
  }
  actions.append(actionButton("Изменить", "", () => startEngineerEdit(employee, account)));
  if (canManageAccounts()) {
    actions.append(actionButton("Удалить", "danger", () => removeEmployee(employee)));
  }
  return row;
}

function actionButton(text, className, onClick) {
  const button = document.createElement("button");
  button.type = "button";
  button.textContent = text;
  if (className) button.className = className;
  button.addEventListener("click", onClick);
  return button;
}

async function logoutAccount(user) {
  if (!window.confirm(`Завершить сеанс: ${user.full_name}? Ему придётся войти заново.`)) return;
  try {
    await Api.logoutUser(user.id);
    await loadUsers();
  } catch (error) {
    window.alert(error.detail);
  }
}

async function removeAccount(user) {
  if (!window.confirm(`Удалить учётную запись ${user.login} (${user.full_name})?`)) return;
  try {
    await Api.deleteUser(user.id);
    if (state.editingUser?.user?.id === user.id) cancelUserEdit();
    await loadUsers();
  } catch (error) {
    window.alert(error.detail);
  }
}

async function removeEmployee(employee) {
  if (!window.confirm(
    `Удалить сотрудника ${employee.full_name} из аэропорта ${state.user.airport_icao}? ` +
    "Его вход в приложение тоже перестанет работать."
  )) return;
  try {
    await Api.deleteEmployee(employee.id);
    if (state.editingUser?.employee?.id === employee.id) cancelUserEdit();
    await loadUsers();
  } catch (error) {
    window.alert(error.detail);
  }
}

/* --- Форма --- */

function fillRoleSelect(selected) {
  const roles = el("user-role");
  roles.replaceChildren();
  for (const role of assignableRoles()) {
    roles.append(new Option(roleTitle(role), role));
  }
  roles.value = assignableRoles().includes(selected) ? selected : "dispatcher";
}

function fillAirportSelect() {
  const airports = el("user-airport");
  airports.replaceChildren();
  for (const airport of state.user.airports || []) {
    airports.append(new Option(`${airport.icao} · ${airport.name}`, airport.icao));
  }
  airports.value = state.user.airport_icao || airports.value;
  // Аэропорт выбирает только технический администратор; остальные
  // заводят людей в своём.
  airports.disabled = state.user.role !== "admin";
}

/**
 * Поля формы под выбранную роль: у инженера — карточка сотрудника
 * и необязательный вход; у остальных — обязательные логин и пароль.
 */
function syncUserForm() {
  const editing = state.editingUser;
  const engineer = el("user-role").value === ENGINEER;
  el("user-engineer-fields").hidden = !engineer;
  // Инженер заводится в аэропорту сессии: без него места и карточки нет.
  const needAirport = engineer && editing === null && !state.user.airport_icao;
  el("user-submit").disabled = needAirport;
  if (needAirport) {
    setNote("user-note", "Выберите аэропорт в шапке: инженер заводится в нём вместе с квалификацией", "is-error");
  } else if (el("user-note").dataset.airportHint) {
    el("user-note").hidden = true;
  }
  el("user-note").dataset.airportHint = needAirport ? "1" : "";
  // Инженер заводится в аэропорту сессии: места берутся из его схемы.
  el("user-airport-box").hidden = engineer || el("user-role").value === "admin" || (editing !== null && state.user.role !== "admin");

  el("user-login-row").hidden = editing !== null && !canManageAccounts();
  el("user-login").required = !engineer && editing === null;
  el("user-password").required = !engineer && editing === null;
  el("user-login-label").textContent = engineer
    ? "Логин для приложения инженера (необязательно)"
    : "Логин";
  if (editing === null) {
    el("user-password-label").textContent = engineer ? "Пароль (вместе с логином)" : "Пароль";
  }
  el("user-types").required = engineer && editing === null;
}

function resetUserForm() {
  state.editingUser = null;
  el("user-form").reset();
  el("user-form-card").hidden = !canManageAccounts();
  fillRoleSelect("dispatcher");
  fillAirportSelect();
  el("user-role").disabled = false;
  el("user-password-row").hidden = false;
  el("user-on-shift").disabled = false;
  for (const id of ENGINEER_CARD_FIELDS) el(id).disabled = false;
  el("user-form-title").textContent = "Завести учётную запись";
  el("user-submit").textContent = "Завести";
  el("user-cancel").hidden = true;
  syncUserForm();
}

function cancelUserEdit() {
  resetUserForm();
  el("user-note").hidden = true;
}

/** Правка учётной записи диспетчера, начальника смены, администратора. */
function startAccountEdit(user) {
  resetUserForm();
  state.editingUser = { kind: "account", user };
  el("user-name").value = user.full_name;
  fillRoleSelect(user.role);
  el("user-password").value = "";
  el("user-password-label").textContent = "Новый пароль (пусто — не менять)";
  el("user-airport").value = user.airport_icao || el("user-airport").value;
  el("user-login").value = user.login;
  openUserEdit(`Изменить: ${user.login}`);
}

/**
 * Правка инженера: карточка и, если есть, вход. Начальник смены меняет
 * только смену, место и присутствие на смене; квалификацию и ФИО —
 * администратор аэропорта.
 */
async function startEngineerEdit(employee, account) {
  if (employee.airport_icao !== state.user.airport_icao) {
    try {
      await Api.selectAirport(employee.airport_icao, state.user.shift || "day");
      state.user = await Api.me();
      state.graph = null;
      fillAirportSwitch();
      await loadGraphOnce();
      fillPlaces("user-place");
    } catch (error) { window.alert(error.detail); return; }
  }
  resetUserForm();
  state.editingUser = { kind: "engineer", employee, account };
  el("user-form-card").hidden = false;

  el("user-name").value = employee.full_name;
  el("user-role").replaceChildren(new Option(roleTitle(ENGINEER), ENGINEER));
  el("user-role").value = ENGINEER;
  el("user-role").disabled = true;
  el("user-shift").value = employee.shift;
  const mark = employee.qualifications[0] || null;
  if (mark) {
    el("user-mark").value = mark.category;
    el("user-types").value = mark.aircraft_types.join(", ");
    el("user-valid").value = mark.valid_until;
  }
  el("user-place").value = nearestPlaceId(employee) || el("user-place").value;
  el("user-place").dataset.original = el("user-place").value;
  el("user-on-shift").checked = employee.always_on_shift;
  // Занятого снять со смены может только технический администратор:
  // сервер вернёт его вызовы к подбору (services/shifts.end_shift).
  // Остальным это недоступно — сервер ответит отказом.
  el("user-on-shift").disabled = !canManageAccounts();
  for (const id of ENGINEER_CARD_FIELDS) el(id).disabled = !canManageAccounts();

  // Новый пароль задаётся, только если вход у инженера есть.
  el("user-password-row").hidden = !canManageAccounts();
  el("user-password").value = "";
  el("user-password-label").textContent = "Новый пароль (пусто — не менять)";
  el("user-login").value = account?.login || "";
  openUserEdit(`Изменить: ${employee.full_name}`);
  setNote("user-note", engineerHint(employee), "");
}

function openUserEdit(title) {
  el("user-form-title").textContent = title;
  el("user-submit").textContent = "Сохранить";
  el("user-cancel").hidden = false;
  el("user-note").hidden = true;
  syncUserForm();
}

function engineerHint(employee) {
  if (isBusyStatus(employee.status)) {
    return state.user.role === "admin"
      ? "Сотрудник занят на вызове. При снятии со смены вызов вернётся к подбору"
      : "Сотрудник занят на вызове: снять его со смены может технический администратор — вызов вернётся к подбору";
  }
  if (!canManageAccounts()) {
    return "Начальник смены меняет смену, место и присутствие на смене; квалификацию — администратор аэропорта";
  }
  return "Меняются квалификация, смена, место и присутствие на смене";
}

/** Карточка инженера из полей формы, или текст ошибки. */
function engineerCard() {
  const place = selectedPlace("user-place");
  if (place === null) return { error: placeError() };
  const types = el("user-types")
    .value.split(",")
    .map((item) => item.trim())
    .filter((item) => item.length > 0);
  const qualifications = types.length ? [
    { category: el("user-mark").value, aircraft_types: types, valid_until: el("user-valid").value.trim() },
  ] : [];
  return {
    full_name: el("user-name").value.trim(),
    shift: el("user-shift").value,
    qualifications,
    lat: place.lat,
    lon: place.lon,
    on_shift: el("user-on-shift").checked,
  };
}

async function onUserSubmit(event) {
  event.preventDefault();
  const editing = state.editingUser;
  const engineer = el("user-role").value === ENGINEER;
  el("user-submit").disabled = true;
  try {
    let message;
    if (editing && editing.kind === "engineer") {
      message = await saveEngineer(editing);
    } else if (editing) {
      message = await saveAccount(editing.user);
    } else if (engineer) {
      message = await createEngineer();
    } else {
      message = await createAccount();
    }
    if (message === null) return;
    resetUserForm();
    setNote("user-note", message, "is-ok");
    await loadUsers();
  } catch (error) {
    setNote("user-note", error.detail, "is-error");
  } finally {
    el("user-submit").disabled = false;
  }
}

async function createAccount() {
  const role = el("user-role").value;
  const payload = {
    login: el("user-login").value.trim(),
    password: el("user-password").value,
    full_name: el("user-name").value.trim(),
    role,
  };
  if (role !== "admin") payload.airport_icao = el("user-airport").value;
  const created = await Api.createUser(payload);
  const where = created.airport_icao ? `, аэропорт ${created.airport_icao}` : "";
  return `${created.full_name} — ${roleTitle(created.role)}${where}. Логин: ${created.login}`;
}

async function saveAccount(user) {
  const changes = { full_name: el("user-name").value.trim(), login: el("user-login").value.trim() };
  if (el("user-password").value) changes.password = el("user-password").value;
  if (el("user-role").value !== user.role) changes.role = el("user-role").value;
  if (state.user.role === "admin" && el("user-role").value !== "admin" && el("user-airport").value !== user.airport_icao) changes.airport_icao = el("user-airport").value;
  const updated = await Api.updateUser(user.id, changes);
  return `${updated.login}: изменения сохранены`;
}

async function createEngineer() {
  if (!state.user.airport_icao) {
    setNote("user-note", "Выберите аэропорт в шапке: инженер заводится в нём", "is-error");
    return null;
  }
  const card = engineerCard();
  if (card.error) {
    setNote("user-note", card.error, "is-error");
    return null;
  }
  const login = el("user-login").value.trim() || null;
  const password = el("user-password").value || null;
  const employee = await Api.createEmployee({
    ...card,
    airport_icao: state.user.airport_icao,
    login,
    password,
  });
  const access = login ? ` Вход в приложение инженера: ${login}.` : " Без входа в приложение.";
  return `${employee.full_name} — инженер, ${state.user.airport_icao}.${access}`;
}

/**
 * Сохранение карточки инженера. Координаты идут отдельной ручкой — той
 * же, что шлёт телефон инженера. Статус занятого не трогаем: его
 * освобождает снятие вызова, иначе вызов остался бы без исполнителя.
 */
async function saveEngineer({ employee, account }) {
  const card = engineerCard();
  if (card.error) {
    setNote("user-note", card.error, "is-error");
    return null;
  }
  // Начальнику смены сервер разрешает только смену и статус.
  const changes = canManageAccounts()
    ? { full_name: card.full_name, shift: card.shift, qualifications: [...card.qualifications, ...employee.qualifications.slice(1)] }
    : { shift: card.shift };
  // Снятие со смены передаётся и для занятого (его вызов вернётся к
  // подбору); «на смене» для занятого не шлём — он и так на смене.
  if (canManageAccounts()) {
    changes.always_on_shift = card.on_shift;
    const login = el("user-login").value.trim();
    if (login && login !== account?.login) changes.login = login;
    if (el("user-password").value) changes.password = el("user-password").value;
  }
  const updated = await Api.updateEmployee(employee.id, changes);
  if (el("user-place").value !== el("user-place").dataset.original) {
    await Api.sendLocation(employee.id, card.lat, card.lon);
  }

  return `${updated.full_name}: изменения сохранены`;
}

function isBusyStatus(status) {
  return ["assigned", "en_route", "busy"].includes(status);
}

/** Точка графа, в которой сейчас стоит сотрудник, — по совпадению координат. */
function nearestPlaceId(employee) {
  if (state.graph === null || employee.lat === null) {
    return null;
  }
  let best = null;
  let bestDistance = Infinity;
  for (const node of state.graph.nodes || []) {
    if (node.type !== "tech_center" && node.type !== "stand") {
      continue;
    }
    const distance =
      Math.abs(node.lat - employee.lat) + Math.abs(node.lon - employee.lon);
    if (distance < bestDistance) {
      best = node.id;
      bestDistance = distance;
    }
  }
  return best;
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

/* --- Расписание смен --- */

const SHIFT_NAMES = { day: "дневная", night: "ночная" };

function sessionAirport() {
  return (state.user.airports || []).find((item) => item.icao === state.user.airport_icao) || null;
}

function loadShifts() {
  const airport = sessionAirport();
  el("shifts-airport").textContent = airport ? airport.icao : "—";
  el("shifts-form").hidden = !airport;
  if (!airport) {
    setNote("shifts-note", "Выберите аэропорт в шапке", "");
    return;
  }
  el("shift-tz").value = airport.timezone;
  el("shift-start").value = airport.day_start;
  el("shift-end").value = airport.day_end;
  setNote("shifts-note", shiftNow(airport), "");
}

function shiftNow(airport) {
  return `Сейчас в ${airport.icao} ${airport.local_time} — идёт ${SHIFT_NAMES[airport.current_shift]} смена.`;
}

async function onShiftsSubmit(event) {
  event.preventDefault();
  const airport = sessionAirport();
  if (!airport) return;
  el("shifts-submit").disabled = true;
  try {
    const updated = await Api.setShiftSchedule(airport.icao, {
      timezone: el("shift-tz").value.trim(),
      day_start: el("shift-start").value,
      day_end: el("shift-end").value,
    });
    state.user = await Api.me();
    loadShifts();
    setNote("shifts-note", `Сохранено. ${shiftNow(updated)}`, "is-ok");
  } catch (error) {
    setNote("shifts-note", error.detail, "is-error");
  } finally {
    el("shifts-submit").disabled = false;
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
    remove.textContent = "Удалить";
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
  if (!window.confirm(`Удалить машину ${vehicle.call_sign} из парка ${state.user.airport_icao}?`)) {
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
