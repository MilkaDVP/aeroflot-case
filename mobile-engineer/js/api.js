/*
 * Клиент API для PWA инженера.
 *
 * Намеренно отдельный от клиента веб-диспетчера, а не общий модуль:
 * это два независимо разворачиваемых приложения, и связывать их общим
 * файлом означало бы, что правка ради диспетчера может сломать телефон
 * инженера на перроне. Приложению нужно шесть ручек из всего API,
 * и держать их здесь целиком дешевле, чем тянуть общую зависимость.
 */

const API_PORT = "8000";
const STORAGE_TOKEN = "aeroflot.engineer.token";
const STORAGE_USER = "aeroflot.engineer.user";

function detectApiBase() {
  // Телефон почти всегда смотрит на сервер по сети, а не на localhost,
  // поэтому адрес можно задать параметром и сохранить в закладке.
  const override = new URLSearchParams(window.location.search).get("api");
  if (override) {
    localStorage.setItem("aeroflot.engineer.api", override.replace(/\/$/, ""));
  }
  const saved = localStorage.getItem("aeroflot.engineer.api");
  if (saved) {
    return saved;
  }
  return `${window.location.protocol}//${window.location.hostname}:${API_PORT}`;
}

const CONFIG = {
  apiBase: detectApiBase(),

  // Опрос вызова. Реже, чем у диспетчера: телефон в кармане, экономим
  // батарею и мобильный трафик, а вызов приходит не каждую секунду.
  pollIntervalMs: 5000,

  // Как часто отправлять координаты. Чаще нет смысла: пешком за пять
  // секунд человек проходит около семи метров.
  locationIntervalMs: 5000,

  // Норматив прибытия. На клиенте нужен только для обратного отсчёта
  // и цвета; решение всегда принимает сервер.
  regulationLimitMin: 15,
};

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
    // На перроне связь пропадает регулярно. Это не ошибка приложения,
    // и сообщение должно отличаться от «сервер вернул ошибку».
    throw new ApiError(0, "Нет связи с сервером");
  }

  if (response.status === 401) {
    Session.clear();
    throw new ApiError(401, "Сессия истекла, войдите заново");
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
  if (payload && Array.isArray(payload.detail)) {
    return payload.detail.map((item) => item.msg).join("; ");
  }
  return `Ошибка ${status}`;
}

const Api = {
  login: (login, password) => request("POST", "/api/auth/login", { login, password }),
  logout: () => request("POST", "/api/auth/logout"),
  me: () => request("GET", "/api/auth/me"),

  currentCall: () => request("GET", "/api/me/current-call"),
  setStatus: (action) => request("POST", "/api/me/status", { action }),

  setShift: (employeeId, onShift) =>
    request("POST", `/api/employees/${employeeId}/shift`, { on_shift: onShift }),
  sendLocation: (employeeId, lat, lon) =>
    request("POST", `/api/employees/${employeeId}/location`, { lat, lon }),
};
