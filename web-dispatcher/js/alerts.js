/*
 * Сигнал о срыве регламента.
 *
 * Пятнадцать минут на прибытие — договорное условие со штрафами. Сервер
 * при каждом опросе пересчитывает прогноз прибытия и помечает вызовы,
 * по которым срок будет сорван или уже сорван. Причина и действие
 * написаны на карточке вызова (panels.js), она же поднимается наверх
 * списка. Здесь — то, что заметно, когда диспетчер смотрит не на экран:
 * звуковой сигнал и мигающий заголовок вкладки при новой тревоге.
 *
 * Звук включён всегда и не настраивается: тревогу по договорному сроку
 * нельзя выключить по ошибке. Кнопок и счётчиков в шапке нет — они
 * повторяли то, что и так видно в списке.
 *
 * Решает, что тревожно, сервер. Клиент только показывает.
 */

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

/**
 * Перерисовывает уведомления по свежему списку вызовов.
 *
 * @param {Array} calls вызовы аэропорта с полем control от сервера
 * @param {Function} onSelect открыть вызов по щелчку на тревоге
 */
function renderAlerts(calls) {
  signalNewAlerts(calls.filter((call) => call.control && call.control.alert));
}

function compareByMinutesLeft(left, right) {
  return left.control.minutes_left - right.control.minutes_left;
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
  if (fresh.length) {
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

/**
 * Короткий двухтональный сигнал без звукового файла.
 *
 * Браузер разрешает звук только после действия пользователя на странице,
 * поэтому звуковой контекст создаётся при первом щелчке (см. unlockAudio).
 * До этого сигнал не звучит — тревога видна на карточке и в заголовке вкладки.
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
  // Любой щелчок по странице разблокирует звук на будущее.
  document.addEventListener("click", unlockAudio, { once: true });
}
