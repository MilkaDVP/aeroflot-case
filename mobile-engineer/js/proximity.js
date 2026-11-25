/*
 * Приближение ВС: красная плашка, сигнал, вибрация и голос.
 *
 * Звучит всегда, даже при выключенной озвучке маршрута: это безопасность,
 * а не подсказка. Прогноз считает сервер (services/proximity.py).
 */

const PROXIMITY_POLL_MS = 2000;
const PROXIMITY_REPEAT_MS = 6000;

let proximityTimer = null;
let proximityAudio = null;
let proximityKey = "";
let proximityAt = 0;
let proximityBusy = false;

/* Браузер разрешает звук только после касания страницы. */
function unlockProximityAudio() {
  try {
    proximityAudio = proximityAudio || new (window.AudioContext || window.webkitAudioContext)();
    if (proximityAudio.state === "suspended") proximityAudio.resume();
  } catch (error) {
    // Звука нет — остаются плашка, вибрация и голос.
  }
}
document.addEventListener("pointerdown", unlockProximityAudio);

function proximityBeep(urgent) {
  const audio = proximityAudio;
  if (!audio || audio.state !== "running") return;
  for (let i = 0; i < (urgent ? 5 : 3); i += 1) {
    const at = audio.currentTime + i * 0.25;
    const tone = audio.createOscillator(), gain = audio.createGain();
    tone.type = "square";
    tone.frequency.value = i % 2 ? 700 : 1000;
    gain.gain.setValueAtTime(0.0001, at);
    gain.gain.exponentialRampToValueAtTime(0.25, at + 0.02);
    gain.gain.exponentialRampToValueAtTime(0.0001, at + 0.2);
    tone.connect(gain).connect(audio.destination);
    tone.start(at);
    tone.stop(at + 0.22);
  }
}

function showProximity(list) {
  const warning = list[0];
  document.getElementById("proximity").hidden = !warning;
  routeMap.setAircraft(list);
  if (!warning) {
    proximityKey = "";
    return;
  }
  document.getElementById("proximity").classList.toggle("is-urgent", warning.urgent);
  document.getElementById("proximity-text").textContent = warning.text;
  document.getElementById("proximity-more").textContent =
    `${warning.distance_m} м, ${warning.from}, ${warning.speed_kmh} км/ч. ${
      warning.crosses ? "Остановитесь и пропустите" : "Отойдите от дорожки"}`;
  // Новое или ставшее срочным — сразу, дальше с повтором.
  const key = `${warning.id}/${warning.urgent}`;
  if (key === proximityKey && Date.now() - proximityAt < PROXIMITY_REPEAT_MS) return;
  proximityKey = key;
  proximityAt = Date.now();
  proximityBeep(warning.urgent);
  navigator.vibrate?.(warning.urgent ? [400, 100, 400, 100, 400] : [250, 120, 250]);
  if (window.speechSynthesis && window.SpeechSynthesisUtterance) {
    window.speechSynthesis.cancel();
    const message = new SpeechSynthesisUtterance(`Внимание! Приближается самолёт, ${warning.from}. ${
      warning.crosses ? "Остановитесь, пропустите" : "Отойдите от дорожки"}`);
    message.lang = "ru-RU";
    window.speechSynthesis.speak(message);
  }
}

async function pollProximity() {
  if (proximityBusy) return;
  proximityBusy = true;
  try {
    showProximity(await Api.proximity());
  } catch (error) {
    // Нет связи — прогноз устарел, показывать его нельзя.
    showProximity([]);
  } finally {
    proximityBusy = false;
  }
}

function startProximity() {
  clearInterval(proximityTimer);
  proximityTimer = setInterval(pollProximity, PROXIMITY_POLL_MS);
}

function stopProximity() {
  clearInterval(proximityTimer);
  showProximity([]);
}
