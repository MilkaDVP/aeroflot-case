/*
 * Красные уведомления о срыве регламента.
 *
 * Пятнадцать минут на прибытие — договорное условие со штрафами. Сервер
 * при каждом опросе пересчитывает прогноз прибытия по текущим координатам
 * исполнителя и помечает вызовы, по которым срок будет сорван, уже сорван,
 * исполнителя нет или его координатам нельзя верить. Здесь эти пометки
 * превращаются в то, что диспетчер не пропустит:
 *   - счётчик в шапке — виден, даже когда открыт другой вызов; щелчок
 *     ведёт к самой срочной карточке;
 *   - звуковой сигнал и мигающий заголовок вкладки при новой тревоге:
 *     диспетчер может смотреть на перрон в окно, а не в экран.
 * Причина и подсказка, что делать, написаны на карточке вызова
 * (panels.js): отдельная плашка над списком повторяла бы её слово в слово.
 *
 * Решает, что тревожно, сервер. Клиент только показывает — иначе
 * у диспетчера и в базе были бы разные представления о нарушении.
 */

const SOUND_STORAGE = "aeroflot.alarm-sound";
const ALARM_TITLE_PREFIX = "⚠ ";

// Когда звучит сигнал — одним абзацем. Показывается в панели звука и в справке.
const SOUND_EXPLAIN =
  "Сигнал — три коротких тона. Звучит один раз, когда по вызову появляется новая тревога: " +
  "исполнитель не успевает, срок истёк, вызов долго без исполнителя или пропал сигнал. " +
  "Тревоги, которые уже были при открытии экрана, не звучат — они видны в списке вызовов.";
const SOUND_BLOCKED =
  "Браузер не даёт странице звучать, пока вы по ней не щёлкнули. Щёлкните в любом месте.";

const alertState = {
  // Уже показанные тревоги: «вызов:уровень». Сигнал звучит только на новую —
  // тревога, которая висит пять минут, не должна пищать каждые четыре секунды.
  seen: new Set(),
  first: true,
  audio: null,
  titleTimer: null,
  baseTitle: document.title,
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

  renderAlertPill(alerts, onSelect);
  signalNewAlerts(alerts);
}

function compareByMinutesLeft(left, right) {
  return left.control.minutes_left - right.control.minutes_left;
}

function renderAlertPill(alerts, onSelect) {
  const pill = document.getElementById("alert-pill");
  pill.hidden = alerts.length === 0;
  pill.textContent = `${ALARM_TITLE_PREFIX}Под угрозой: ${alerts.length}`;
  pill.title = "Вызовы, по которым срок прибытия сорван или будет сорван. Щелчок откроет самый срочный";
  pill.onclick = alerts.length ? () => openMostUrgent(alerts[0], onSelect) : null;
}

/** Самый срочный вызов стоит первым в списке — открыть его и показать начало списка. */
function openMostUrgent(call, onSelect) {
  onSelect(call);
  document.getElementById("calls-list").scrollTop = 0;
}

/** «ост. 4 мин» или «просрочен 1 ч 3 мин» — главная цифра карточки вызова. */
function minutesLeftText(control) {
  const left = control.minutes_left;
  if (left < 0) {
    return `просрочен ${durationText(Math.ceil(-left))}`;
  }
  return `ост. ${durationText(Math.floor(left))}`;
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

/**
 * Звук готов: включён и браузер его разрешил.
 *
 * Браузер разрешает звук только после действия пользователя на странице.
 * Раньше до первого щелчка сигнал молча не звучал, а кнопка при этом
 * показывала «вкл» — диспетчер думал, что звук сломан.
 */
function soundReady() {
  return Boolean(alertState.audio && alertState.audio.state === "running");
}

/** Кнопка в шапке и панель под ней: три состояния звука словами. */
function renderSoundToggle() {
  const button = document.getElementById("sound-toggle");
  const state = document.getElementById("sound-state");
  const enabled = soundEnabled();
  const waiting = enabled && !soundReady();

  button.textContent = !enabled ? "Звук: выкл" : waiting ? "Звук: ждёт щелчка" : "Звук: вкл";
  button.classList.toggle("is-off", !enabled);
  button.classList.toggle("is-waiting", waiting);
  button.title = "Звуковой сигнал тревоги: когда звучит и как проверить";

  state.textContent = !enabled
    ? "Звук выключен: о тревоге скажут только красная карточка и счётчик в шапке."
    : waiting
      ? SOUND_BLOCKED
      : "Звук включён и разрешён браузером.";
  state.classList.toggle("is-waiting", waiting);
  document.getElementById("sound-explain").textContent = SOUND_EXPLAIN;
  document.getElementById("sound-switch").textContent = enabled ? "Выключить звук" : "Включить звук";
}

/**
 * Короткий двухтональный сигнал без звукового файла.
 *
 * Браузер разрешает звук только после действия пользователя на странице,
 * поэтому звуковой контекст создаётся при первом щелчке (см. unlockAudio).
 * До этого сигнал не звучит — и кнопка в шапке прямо об этом говорит.
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
  document.getElementById("sound-toggle").addEventListener("click", toggleSoundPanel);
  document.getElementById("sound-test").addEventListener("click", testSound);
  document.getElementById("sound-switch").addEventListener("click", switchSound);
  // Любой щелчок по странице разблокирует звук на будущее;
  // кнопка в шапке сразу перестаёт показывать «ждёт щелчка».
  document.addEventListener("click", unlockAndRender, { once: true });
}

function unlockAndRender() {
  return unlockAudio().then(renderSoundToggle);
}

function toggleSoundPanel() {
  const panel = document.getElementById("sound-panel");
  panel.hidden = !panel.hidden;
}

/** Проверка звука: диспетчер слышит сигнал и убеждается, что колонки включены. */
function testSound() {
  unlockAndRender().then(playAlarm);
}

function switchSound() {
  setSoundEnabled(!soundEnabled());
  if (soundEnabled()) {
    testSound();
  }
}
