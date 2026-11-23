/*
 * Клиент REST API.
 *
 * Единственное место, где формируются запросы к серверу. Токен хранится
 * в localStorage: сессия должна переживать перезагрузку вкладки —
 * диспетчер не обязан входить заново из-за случайного F5 за смену.
 */

/*
 * Старшинство ролей — то же, что на сервере (models/user.py). Старшая роль
 * получает права младших; инженер в эту цепочку не входит. Решает всё
 * равно сервер: здесь роль нужна, чтобы не показывать заведомо
 * недоступные кнопки.
 */
const ROLE_RANK = {
  dispatcher: 1,
  shift_supervisor: 2,
  local_admin: 3,
  admin: 4,
};

const ROLE_TITLES = {
  admin: "технический администратор",
  local_admin: "администратор аэропорта",
  shift_supervisor: "начальник смены",
  dispatcher: "диспетчер",
  engineer: "инженер",
};

/** Не ниже ли роль пользователя заданной. */
function roleAtLeast(role, minimum) {
  return (ROLE_RANK[role] || 0) >= ROLE_RANK[minimum];
}

function roleTitle(role) {
  return ROLE_TITLES[role] || role;
}

const STORAGE_TOKEN = "aeroflot.token";
const STORAGE_USER = "aeroflot.user";

const Session = {
  get token() {
    return localStorage.getItem(STORAGE_TOKEN);
  },

  set token(value) {
    if (value) {
      localStorage.setItem(STORAGE_TOKEN, value);
    } else {
      localStorage.removeItem(STORAGE_TOKEN);
    }
  },

  get user() {
    const raw = localStorage.getItem(STORAGE_USER);
    return raw ? JSON.parse(raw) : null;
  },

  set user(value) {
    if (value) {
      localStorage.setItem(STORAGE_USER, JSON.stringify(value));
    } else {
      localStorage.removeItem(STORAGE_USER);
    }
  },

  clear() {
    localStorage.removeItem(STORAGE_TOKEN);
    localStorage.removeItem(STORAGE_USER);
  },
};

/**
 * Находимся ли мы уже на экране входа.
 *
 * Проверять только окончание «index.html» недостаточно: сервер статики
 * отдаёт тот же файл по корневому пути, и перенаправление со страницы
 * на саму себя дало бы лишнюю перезагрузку.
 */
function isLoginPage() {
  const path = window.location.pathname;
  return path === "/" || path.endsWith("/") || path.endsWith("index.html");
}

class ApiError extends Error {
  constructor(status, detail) {
    super(detail);
    this.status = status;
    this.detail = detail;
  }
}

async function request(method, path, body) {
  const headers = { "Content-Type": "application/json" };
  if (Session.token) {
    headers["Authorization"] = `Bearer ${Session.token}`;
  }

  let response;
  try {
    response = await fetch(CONFIG.apiBase + path, {
      method,
      headers,
      body: body === undefined ? undefined : JSON.stringify(body),
    });
  } catch (error) {
    // Сеть недоступна — это отдельный случай, его нельзя показывать
    // диспетчеру как «ошибка сервера»: причина и действия разные.
    throw new ApiError(0, "Сервер недоступен. Проверьте, запущен ли он.");
  }

  if (response.status === 401) {
    // Сессия истекла или отозвана: возвращаем на экран входа сразу,
    // иначе пользователь будет тыкать в кнопки, которые молча не работают.
    Session.clear();
    if (!isLoginPage()) {
      window.location.href = "index.html";
    }
    throw new ApiError(401, "Сессия истекла");
  }

  const payload = await response.json().catch(() => null);

  if (!response.ok) {
    throw new ApiError(response.status, extractDetail(payload, response.status));
  }

  return payload;
}

function extractDetail(payload, status) {
  if (payload && typeof payload.detail === "string") {
    return payload.detail;
  }
  // Ошибки валидации Pydantic приходят списком объектов.
  if (payload && Array.isArray(payload.detail)) {
    return payload.detail.map((item) => item.msg).join("; ");
  }
  return `Ошибка ${status}`;
}

const Api = {
  changeTransport: (id, vehicle) => request("POST", `/api/calls/${id}/transport${vehicle ? "?vehicle_id=" + vehicle : ""}`),
  login: (login, password) => request("POST", "/api/auth/login", { login, password }),
  logout: () => request("POST", "/api/auth/logout"),
  me: () => request("GET", "/api/auth/me"),
  selectAirport: (airport_icao, shift) =>
    request("POST", "/api/session/airport", { airport_icao, shift }),

  airports: () => request("GET", "/api/airports"),
  airportGraph: (icao) => request("GET", `/api/airports/${icao}`),

  adminRoster: () => request("GET", "/api/employees/roster"),
  airportRoster: () => request("GET", "/api/employees?all_shifts=true"),
  mapParticipants: () => request("GET", "/api/employees?include_training=true"),
  employees: (shift) =>
    request("GET", shift ? `/api/employees?shift=${shift}` : "/api/employees"),
  // Имитация координат с рабочего экрана: задание разрешает задавать
  // местоположение нажатием на карту вместо настоящего GPS.
  sendLocation: (employeeId, lat, lon) =>
    request("POST", `/api/employees/${employeeId}/location`, { lat, lon }),
  updateEmployee: (employeeId, payload) =>
    request("PATCH", `/api/employees/${employeeId}`, payload),

  moveVehicle: (id, point) => request("POST", `/api/vehicles/${id}/location`, point),
  moveAircraft: (id, point) => request("POST", `/api/aircraft/${id}/location`, point),
  deleteEmployee: (id) => request("DELETE", `/api/employees/${id}`),

  // Учётные записи: список и сеансы — начальник смены и старше,
  // заведение и правка — администратор аэропорта и старше.
  users: () => request("GET", "/api/admin/users"),
  createUser: (payload) => request("POST", "/api/admin/users", payload),
  updateUser: (id, payload) => request("PATCH", `/api/admin/users/${id}`, payload),
  deleteUser: (id) => request("DELETE", `/api/admin/users/${id}`),
  logoutUser: (id) => request("POST", `/api/admin/users/${id}/logout`),
  // Расписание смен аэропорта: часовой пояс и граница дневной смены.
  setShiftSchedule: (icao, payload) =>
    request("PUT", `/api/airports/${icao}/shift-schedule`, payload),

  aircraft: () => request("GET", "/api/aircraft"),
  // Открытый справочник неисправностей: поиск словами экипажа или номером
  // главы ATA и пополнение из формы вызова.
  searchDefects: (query, limit) =>
    request(
      "GET",
      `/api/defect-types?q=${encodeURIComponent(query || "")}&limit=${limit || 20}`
    ),
  createDefectType: (payload) => request("POST", "/api/defect-types", payload),
  ataChapters: () => request("GET", "/api/ata-chapters"),
  vehicles: () => request("GET", "/api/vehicles"),

  calls: (includeClosed = false) => request("GET", includeClosed ? "/api/calls?include_closed=true" : "/api/calls"),
  createCall: (aircraft_id, defect_code, description, urgent) =>
    request("POST", "/api/calls", { aircraft_id, defect_code, description, urgent }),
  suggest: (callId) => request("POST", `/api/calls/${callId}/suggest`),
  // План ИИ-диспетчера; explain=false — без пояснения GigaChat (быстрее).
  aiPlan: (explain = true) => request("GET", `/api/ai/plan?explain=${explain}`),
  aiAsk: (question) => request("POST", "/api/ai/ask", { question }),

  // Журнал событий: просмотр, типы, целостность, выгрузка файлом.
  events: (params) => request("GET", `/api/events?${new URLSearchParams(params)}`),
  eventTypes: () => request("GET", "/api/events/types"),
  eventsIntegrity: () => request("GET", "/api/events/integrity"),
  exportEvents: async (params) => {
    const response = await fetch(`${CONFIG.apiBase}/api/events/export?${new URLSearchParams(params)}`, {
      headers: { Authorization: `Bearer ${Session.token}` },
    });
    if (!response.ok) {
      const payload = await response.json().catch(() => null);
      throw new ApiError(response.status, extractDetail(payload, response.status));
    }
    const disposition = response.headers.get("Content-Disposition") || "";
    const name = (disposition.match(/filename="([^"]+)"/) || [])[1] || `journal.${params.format}`;
    return { blob: await response.blob(), name };
  },
  // Захват вызова: один вызов разбирает один диспетчер. takeover —
  // начальник смены забирает чужой вызов себе.
  claimCall: (callId, takeover = false) =>
    request("POST", `/api/calls/${callId}/claim`, { takeover }),
  releaseCall: (callId) => request("POST", `/api/calls/${callId}/release`),
  // switchOver — переключить занятого, ждущего запчасти, на этот вызов.
  // travel — как поедет: auto (как быстрее), walk, with_lead (с первым).
  assign: (callId, employee_id, override_reason, switchOver, travel) =>
    request("POST", `/api/calls/${callId}/assign`, {
      employee_id,
      override_reason,
      switch: Boolean(switchOver),
      travel: travel || "auto",
    }),
  // Ещё один инженер на тот же вызов: свободный выезжает сразу,
  // занятый встаёт в очередь (нужна причина).
  support: (callId, employee_id, override_reason, travel) =>
    request("POST", `/api/calls/${callId}/support`, {
      employee_id,
      override_reason,
      travel: travel || "auto",
    }),
  sendMessage: (callId, text) => request("POST", `/api/calls/${callId}/message`, { text }),
  setUrgency: (callId, urgent) =>
    request("POST", `/api/calls/${callId}/urgency`, { urgent }),
  unassign: (callId, reason) =>
    request("POST", `/api/calls/${callId}/unassign`, { reason }),
  setCallStatus: (callId, status) =>
    request("PATCH", `/api/calls/${callId}/status`, { status }),

  // Входящие сообщения экипажа и внешних систем: ящик диспетчера.
  reports: () => request("GET", "/api/reports"),
  acceptReport: (reportId, defect_code, aircraft_id, urgent) =>
    request("POST", `/api/reports/${reportId}/accept`, { defect_code, aircraft_id, urgent }),
  rejectReport: (reportId, reason) =>
    request("POST", `/api/reports/${reportId}/reject`, { reason }),

  // Режим учений: состояние, переключение (администратор), задержка сотрудника.
  drillState: () => request("GET", "/api/drill"),
  setDrill: (enabled) => request("POST", "/api/drill", { enabled }),
  holdEmployee: (employeeId, held) =>
    request("POST", `/api/drill/hold/${employeeId}`, { held }),

  // Лента Наставника и итог смены одним ответом.

  /* --- Администрирование --- */

  adminAirports: () => request("GET", "/api/admin/airports"),
  importAirport: (icao, service_roads) =>
    request("POST", "/api/admin/airports/import", { icao, service_roads }),
  createAirport: (payload) => request("POST", "/api/admin/airports", payload),
  saveAirportGraph: (icao, nodes, edges) =>
    request("PUT", `/api/admin/airports/${icao}/graph`, { nodes, edges }),
  deleteAirport: (icao) => request("DELETE", `/api/admin/airports/${icao}`),

  aircraftTypes: () => request("GET", "/api/aircraft-types"),
  createAircraft: (payload) => request("POST", "/api/aircraft", payload),
  deleteAircraft: (aircraftId) => request("DELETE", `/api/aircraft/${aircraftId}`),

  createEmployee: (payload) => request("POST", "/api/employees", payload),
  createVehicle: (payload) => request("POST", "/api/vehicles", payload),
  deleteVehicle: (vehicleId) => request("DELETE", `/api/vehicles/${vehicleId}`),
};
