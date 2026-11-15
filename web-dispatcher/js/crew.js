/*
 * Страница экипажа: сообщение о неисправности в диспетчерскую.
 *
 * Экипаж не входит в систему как пользователь: планшет — это внешняя
 * точка, и она работает по интеграционному ключу, тому же механизму,
 * каким сообщения передаёт система авиакомпании. Ключ и аэропорт
 * задаются в адресе один раз (crew.html?key=...&airport=UUEE)
 * и запоминаются на устройстве.
 */

const CREW_KEY_STORAGE = "aeroflot.crew.key";
const CREW_AIRPORT_STORAGE = "aeroflot.crew.airport";

const crew = {
  key: rememberParam("key", CREW_KEY_STORAGE),
  airport: (rememberParam("airport", CREW_AIRPORT_STORAGE) || "UUEE").toUpperCase(),
};

/** Параметр из адреса запоминается, чтобы закладка работала и без него. */
function rememberParam(name, storageKey) {
  const value = new URLSearchParams(window.location.search).get(name);
  if (value) {
    localStorage.setItem(storageKey, value);
  }
  return localStorage.getItem(storageKey);
}

async function crewRequest(method, path, body) {
  let response;
  try {
    response = await fetch(CONFIG.apiBase + path, {
      method,
      headers: { "Content-Type": "application/json", "X-API-Key": crew.key || "" },
      body: body === undefined ? undefined : JSON.stringify(body),
    });
  } catch (error) {
    throw new Error("Нет связи с диспетчерской. Сообщите о неисправности по радио");
  }
  const payload = await response.json().catch(() => null);
  if (!response.ok) {
    throw new Error(crewErrorText(response.status, payload));
  }
  return payload;
}

function crewErrorText(status, payload) {
  if (status === 401) {
    return "Планшет не подключён к диспетчерской: неверный ключ. Обратитесь к администратору";
  }
  if (payload && typeof payload.detail === "string") {
    return payload.detail;
  }
  return `Сообщение не принято (ошибка ${status}). Сообщите о неисправности по радио`;
}

async function startCrew() {
  document.getElementById("crew-airport").textContent = crew.airport;
  try {
    const aircraft = await crewRequest("GET", `/api/intake/aircraft?airport=${crew.airport}`);
    fillBoards(aircraft);
  } catch (error) {
    showCrewError(error.message);
  }
}

function fillBoards(aircraft) {
  const select = document.getElementById("crew-board");
  select.replaceChildren();
  for (const item of aircraft) {
    const option = document.createElement("option");
    option.value = item.board_number;
    option.textContent = `${item.board_number} · ${item.aircraft_type}`;
    select.append(option);
  }
}

async function sendReport(event) {
  event.preventDefault();
  showCrewError(null);
  const button = document.getElementById("crew-submit");
  button.disabled = true;

  try {
    const report = await crewRequest("POST", "/api/intake/reports", {
      airport_icao: crew.airport,
      board_number: document.getElementById("crew-board").value,
      description: document.getElementById("crew-description").value.trim(),
      reporter: document.getElementById("crew-reporter").value.trim() || null,
    });
    showCrewDone(report);
    document.getElementById("crew-description").value = "";
  } catch (error) {
    showCrewError(error.message);
  } finally {
    button.disabled = false;
  }
}

/**
 * Подтверждение экипажу: сообщение принято и что будет дальше.
 * Экипажу важно знать срок — по регламенту инженер у борта через 15 минут.
 */
function showCrewDone(report) {
  const deadline = new Date(report.deadline_at).toLocaleTimeString("ru-RU", {
    hour: "2-digit",
    minute: "2-digit",
  });
  const box = document.getElementById("crew-done");
  box.hidden = false;
  box.textContent =
    `Сообщение №${report.id} по борту ${report.board_number} принято диспетчерской. ` +
    `Инженер должен прибыть до ${deadline}.`;
}

function showCrewError(message) {
  const box = document.getElementById("crew-error");
  box.hidden = !message;
  box.textContent = message || "";
  if (message) {
    document.getElementById("crew-done").hidden = true;
  }
}

document.getElementById("crew-form").addEventListener("submit", sendReport);
startCrew();
