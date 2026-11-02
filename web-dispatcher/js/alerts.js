/*
 * Красные уведомления о срыве регламента.
 *
 * Пятнадцать минут на прибытие — договорное условие со штрафами. Сервер
 * при каждом опросе пересчитывает прогноз прибытия по текущим координатам
 * исполнителя и помечает вызовы, по которым срок будет сорван, уже сорван,
 * исполнителя нет или его координатам нельзя верить. Здесь эти пометки
 * превращаются в то, что диспетчер не пропустит:
 *   - плашка над списком вызовов с причиной и подсказкой, что делать;
 *   - счётчик в шапке — виден, даже когда открыт другой вызов;
 *   - звуковой сигнал и мигающий заголовок вкладки при новой тревоге:
 *     диспетчер может смотреть на перрон в окно, а не в экран.
 *
 * Решает, что тревожно, сервер. Клиент только показывает — иначе
 * у диспетчера и в базе были бы разные представления о нарушении.
 */

const SOUND_STORAGE = "aeroflot.alarm-sound";
const ALARM_TITLE_PREFIX = "⚠ ";

const alertState = {
  // Уже показанные тревоги: «вызов:уровень». Сигнал звучит только на новую —
  // тревога, которая висит пять минут, не должна пищать каждые четыре секунды.
  seen: new Set(),
  first: true,
  audio: null,
  titleTimer: null,
  baseTitle: document.title,
};

const ALERT_TITLES = {
  late: "Не успевает",
  overdue: "Срок истёк",
  unassigned: "Без исполнителя",
  no_signal: "Нет сигнала",
};

/**
 * Перерисовывает уведомления по свежему списку вызовов.
 *
 * @param {Array} calls вызовы аэропорта с полем control от сервера
 * @param {Function} onSelect открыть вызов по щелчку на тревоге
 */
function renderAlerts(calls, onSelect) {
  const alerts = calls.filter((call) => call.control && call.control.alert);
  // Самые срочные первыми: меньше всего минут до срока.
  alerts.sort(compareByMinutesLeft);

  renderAlertBox(alerts, onSelect);
  renderAlertPill(alerts, onSelect);
  signalNewAlerts(alerts);
}

function compareByMinutesLeft(left, right) {
  return left.control.minutes_left - right.control.minutes_left;
}

function renderAlertBox(alerts, onSelect) {
  const box = document.getElementById("alert-box");
  box.replaceChildren();
  box.hidden = alerts.length === 0;

  for (const call of alerts) {
    const item = document.createElement("button");
    item.type = "button";
    item.className = "alert-item";
    // Две строки, а не одна: панель вызовов узкая, и в одну строку
    // «НЕ УСПЕВАЕТ», бортовой номер и минуты переносились по слогам.
    item.innerHTML = `
      <div class="alert-item-top">
        <span class="alert-kind">${escapeHtml(ALERT_TITLES[call.control.level] || "Тревога")}</span>
        <span class="alert-left num">${minutesLeftText(call.control)}</span>
      </div>
      <div class="alert-item-board">
        <span class="call-board">${escapeHtml(call.board_number)}</span>
        <span>${escapeHtml(call.aircraft_type)} · ст. ${escapeHtml(call.stand_ref || "—")}</span>
      </div>
      <div class="alert-reason">${escapeHtml(call.control.reason)}</div>
      ${
        call.control.advice
          ? `<div class="alert-advice">${escapeHtml(call.control.advice)}</div>`
          : ""
      }
    `;
    item.addEventListener("click", () => onSelect(call));
    box.append(item);
  }
}

function renderAlertPill(alerts, onSelect) {
  const pill = document.getElementById("alert-pill");
  pill.hidden = alerts.length === 0;
  pill.textContent = `${ALARM_TITLE_PREFIX}Под угрозой: ${alerts.length}`;
  pill.onclick = alerts.length ? () => onSelect(alerts[0]) : null;
}

/** «ост. 4 мин» или «просрочен 3 мин» — главная цифра тревоги. */
function minutesLeftText(control) {
  const left = control.minutes_left;
  if (left < 0) {
    return `просрочен ${Math.ceil(-left)} мин`;
  }
  return `ост. ${Math.floor(left)} мин`;
}

/* --- Сигнал о новой тревоге --- */

function signalNewAlerts(alerts) {
  const current = new Set(alerts.map(alertKey));
  const fresh = [...current].filter((key) => !alertState.seen.has(key));
  alertState.seen = current;

  // Тревоги, которые уже были при открытии экрана, не пищат: диспетчер
  // увидит их в плашке, а сигнал нужен на то, что появилось сейчас.
  if (alertState.first) {
    alertState.first = false;
    updateTitle(alerts.length);
    return;
  }

  updateTitle(alerts.length);
  if (fresh.length && soundEnabled()) {
    playAlarm();
  }
}

function alertKey(call) {
  return `${call.id}:${call.control.level}`;
}

/**
 * Мигающий заголовок вкладки, пока есть тревоги.
 *
 * Если экран диспетчера свёрнут или открыт в соседней вкладке, это
 * единственный признак беды, который виден на панели задач.
 */
function updateTitle(count) {
  clearInterval(alertState.titleTimer);
  if (!count) {
    document.title = alertState.baseTitle;
    return;
  }
  const marked = `${ALARM_TITLE_PREFIX}(${count}) ${alertState.baseTitle}`;
  document.title = marked;
  alertState.titleTimer = setInterval(toggleTitle, 1000, marked);
}

function toggleTitle(marked) {
  document.title = document.title === marked ? alertState.baseTitle : marked;
}

/* --- Звук --- */

function soundEnabled() {
  return localStorage.getItem(SOUND_STORAGE) !== "off";
}

function setSoundEnabled(enabled) {
  localStorage.setItem(SOUND_STORAGE, enabled ? "on" : "off");
  renderSoundToggle();
}

function renderSoundToggle() {
  const button = document.getElementById("sound-toggle");
  button.textContent = soundEnabled() ? "Звук тревоги: вкл" : "Звук тревоги: выкл";
  button.classList.toggle("is-off", !soundEnabled());
}

/**
 * Короткий двухтональный сигнал без звукового файла.
 *
 * Браузер разрешает звук только после действия пользователя на странице,
 * поэтому звуковой контекст создаётся при первом щелчке (см. unlockAudio).
 * До этого сигнал молча не звучит — плашка и заголовок всё равно видны.
 */
function playAlarm() {
  const audio = alertState.audio;
  if (!audio || audio.state !== "running") {
    return;
  }
  const start = audio.currentTime;
  playTone(audio, 880, start, 0.18);
  playTone(audio, 660, start + 0.22, 0.18);
  playTone(audio, 880, start + 0.44, 0.24);
}

function playTone(audio, frequency, start, duration) {
  const oscillator = audio.createOscillator();
  const gain = audio.createGain();
  oscillator.type = "square";
  oscillator.frequency.value = frequency;
  gain.gain.setValueAtTime(0.0001, start);
  gain.gain.exponentialRampToValueAtTime(0.12, start + 0.02);
  gain.gain.exponentialRampToValueAtTime(0.0001, start + duration);
  oscillator.connect(gain).connect(audio.destination);
  oscillator.start(start);
  oscillator.stop(start + duration + 0.02);
}

function unlockAudio() {
  if (!alertState.audio) {
    const AudioContextClass = window.AudioContext || window.webkitAudioContext;
    if (!AudioContextClass) {
      return Promise.resolve();
    }
    alertState.audio = new AudioContextClass();
  }
  if (alertState.audio.state === "suspended") {
    return alertState.audio.resume();
  }
  return Promise.resolve();
}

function initAlerts() {
  renderSoundToggle();
  document.getElementById("sound-toggle").addEventListener("click", () => {
    setSoundEnabled(!soundEnabled());
    // Включение сразу проигрывает сигнал: диспетчер слышит, как он звучит,
    // и убеждается, что колонки не выключены.
    if (soundEnabled()) {
      unlockAudio().then(playAlarm);
    }
  });
  // Любой щелчок по странице разблокирует звук на будущее.
  document.addEventListener("click", unlockAudio, { once: true });
}
