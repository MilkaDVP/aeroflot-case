/*
 * Выбор аэропорта и смены.
 *
 * Аэропорт закрепляется за сессией на сервере, а не хранится в клиенте:
 * от него зависит вся выборка данных, и подменить его параметром запроса
 * быть не должно.
 */

const airportsBox = document.getElementById("airports");
const airportSection = document.getElementById("airport-section");
const shiftsBox = document.getElementById("shifts");
const continueButton = document.getElementById("continue");
const errorBox = document.getElementById("error");
const greeting = document.getElementById("greeting");

const selection = { airport: null, shift: null };
const shiftHint = document.getElementById("shift-hint");
let airportList = [];

const SHIFT_NAMES = { day: "дневная", night: "ночная" };

// Без токена дальше идти незачем: init() всё равно упал бы на первом
// запросе, а пользователь увидел бы ошибку вместо экрана входа.
if (Session.token) {
  init();
} else {
  window.location.href = "index.html";
}

async function init() {
  try {
    const user = await Api.me();
    Session.user = user;
    greeting.textContent = `${user.full_name} — ${roleTitle(user.role)}`;

    airportList = user.airports;
    renderAirports(user.airports);
    document.getElementById("admin-link-row").hidden = !roleAtLeast(user.role, "shift_supervisor");

    // Аэропорт один — шаг выбора не нужен, остаётся только смена.
    if (user.airports.length === 1) {
      selection.airport = user.airports[0].icao;
      airportSection.hidden = true;
      applyAutoShift(user.airports[0]);
    }
    refreshContinue();
  } catch (error) {
    showError(error.detail);
  }
}

function renderAirports(airports) {
  airportsBox.replaceChildren();

  for (const airport of airports) {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "choice";
    button.dataset.icao = airport.icao;
    button.innerHTML =
      `<span class="choice-code">${airport.icao}</span>` +
      `<span>${airport.name}</span>` +
      `<span class="choice-city">${airport.city}</span>`;

    button.addEventListener("click", () => {
      selection.airport = airport.icao;
      markSelected(airportsBox, button);
      applyAutoShift(airport);
      refreshContinue();
    });

    airportsBox.append(button);
  }
}

for (const button of shiftsBox.querySelectorAll("[data-shift]")) {
  button.addEventListener("click", () => {
    selection.shift = button.dataset.shift;
    markSelected(shiftsBox, button);
    describeShift();
    refreshContinue();
  });
}

/**
 * Смена по местному времени выбранного аэропорта. Её предлагает сервер
 * по расписанию, которое задают начальник смены и администраторы.
 */
function applyAutoShift(airport) {
  if (!airport || !airport.current_shift) return;
  selection.shift = airport.current_shift;
  markSelected(shiftsBox, shiftsBox.querySelector(`[data-shift="${airport.current_shift}"]`));
  describeShift();
}

function describeShift() {
  const airport = airportList.find((item) => item.icao === selection.airport);
  if (!airport || !airport.current_shift) {
    shiftHint.hidden = true;
    return;
  }
  const now = `Сейчас в ${airport.icao} ${airport.local_time} (${airport.timezone}) — ` +
    `идёт ${SHIFT_NAMES[airport.current_shift]} смена; дневная ${airport.day_start}–${airport.day_end}.`;
  shiftHint.textContent = selection.shift === airport.current_shift
    ? `${now} Выбрана автоматически, можно сменить вручную.`
    : `${now} Вы выбрали ${SHIFT_NAMES[selection.shift]} вручную.`;
  shiftHint.classList.toggle("is-manual", selection.shift !== airport.current_shift);
  shiftHint.hidden = false;
}

function markSelected(container, selected) {
  for (const item of container.querySelectorAll(".choice")) {
    item.classList.toggle("selected", item === selected);
  }
}

function refreshContinue() {
  continueButton.disabled = !(selection.airport && selection.shift);
}

continueButton.addEventListener("click", async () => {
  showError(null);
  continueButton.disabled = true;

  try {
    await Api.selectAirport(selection.airport, selection.shift);
    window.location.href = "dispatch.html";
  } catch (error) {
    showError(error.detail);
    continueButton.disabled = false;
  }
});

function showError(message) {
  errorBox.hidden = !message;
  errorBox.textContent = message || "";
}
