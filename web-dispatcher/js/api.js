/*
 * Клиент REST API.
 *
 * Единственное место, где формируются запросы к серверу. Токен хранится
 * в localStorage: сессия должна переживать перезагрузку вкладки —
 * диспетчер не обязан входить заново из-за случайного F5 за смену.
 */

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
  login: (login, password) => request("POST", "/api/auth/login", { login, password }),
  logout: () => request("POST", "/api/auth/logout"),
  me: () => request("GET", "/api/auth/me"),
  selectAirport: (airport_icao, shift) =>
    request("POST", "/api/session/airport", { airport_icao, shift }),

  airports: () => request("GET", "/api/airports"),
  airportGraph: (icao) => request("GET", `/api/airports/${icao}`),

  employees: (shift) =>
    request("GET", shift ? `/api/employees?shift=${shift}` : "/api/employees"),
  // Имитация координат с рабочего экрана: задание разрешает задавать
  // местоположение нажатием на карту вместо настоящего GPS.
  sendLocation: (employeeId, lat, lon) =>
    request("POST", `/api/employees/${employeeId}/location`, { lat, lon }),
  updateEmployee: (employeeId, payload) =>
    request("PATCH", `/api/employees/${employeeId}`, payload),

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

  calls: () => request("GET", "/api/calls"),
  createCall: (aircraft_id, defect_code, description, urgent) =>
    request("POST", "/api/calls", { aircraft_id, defect_code, description, urgent }),
  suggest: (callId) => request("POST", `/api/calls/${callId}/suggest`),
  // switchOver — переключить занятого, ждущего запчасти, на этот вызов.
  assign: (callId, employee_id, override_reason, switchOver) =>
    request("POST", `/api/calls/${callId}/assign`, {
      employee_id,
      override_reason,
      switch: Boolean(switchOver),
    }),
  // Ещё один инженер на тот же вызов: свободный выезжает сразу,
  // занятый встаёт в очередь (нужна причина).
  support: (callId, employee_id, override_reason) =>
    request("POST", `/api/calls/${callId}/support`, { employee_id, override_reason }),
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
  shiftFeed: (limit) => request("GET", `/api/shift/feed?limit=${limit || 30}`),

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
