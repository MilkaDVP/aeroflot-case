/* Расстояния в подсказках измеряются вдоль оставшейся линии маршрута. */
const STOP_GUIDANCE = {
  board: ["⇥", "Сядьте в транспорт"],
  alight: ["⇤", "Выйдите из транспорта"],
  warehouse: ["▣", "Заберите инструмент"],
  pass: ["●", "Пройдите контрольную точку"],
};

function remainingLine(payload, path, pos) {
  if (!pos || path.length < 2) return path;
  const call = payload?.call;
  const waypoint = call?.waypoints?.find(w => !w.done_at);
  // Склад системы, пока инструмент не взят, — тоже обязательная точка:
  // без него линия «перепрыгивала» на обратную дорогу.
  const depot = call?.needs_tools && !call.tools_taken_at && call.route_stops?.find(s => s.kind === "warehouse");
  const pending = new Set(payload?.instruction ? [] : [waypoint?.node_id, depot?.node_id,
    payload?.vehicle?.pickup && call?.pickup_node_id].filter(Boolean));
  const stop = path.findIndex(p => pending.has(p.id));
  const limit = stop < 0 ? path.length - 1 : stop;
  let next = 0, first = path[0], closest = Infinity;
  const scale = Math.cos(pos.lat * Math.PI / 180);
  for (let i = 1; i <= limit; i++) {
    const a = path[i-1], b = path[i];
    const dx = (b.lon-a.lon)*scale, dy = b.lat-a.lat;
    const t = Math.max(0, Math.min(1, ((pos.lon-a.lon)*scale*dx + (pos.lat-a.lat)*dy)/(dx*dx+dy*dy)));
    if (!Number.isFinite(t)) continue;
    const projected = {lat:a.lat+t*dy, lon:a.lon+t*(b.lon-a.lon)};
    const offset = haversineM(pos.lat,pos.lon,projected.lat,projected.lon);
    if (offset < closest) {
      closest = offset;
      next = i;
      first = projected;
      if (t === 0) { next = i-1; first = path[next]; }
    }
  }
  return next && first !== path[next] ? [first,...path.slice(next)] : path.slice(next);
}

function nextManeuver(payload, points, pos) {
  if (!pos || !points.length || !payload ||
      (!payload.instruction && payload.call?.status !== "accepted")) return null;
  // Сервер может прислать точку борта поверх последнего узла.
  const distance = (a, b) => haversineM(a.lat, a.lon, b.lat, b.lon);
  const path = [];
  for (const p of remainingLine(payload, points, pos)) {
    if (!path.length || distance(path.at(-1), p) > 0.5) path.push(p);
    else if (p.id && !path.at(-1).id) path[path.length-1] = p;
  }
  let next = 0, metres = distance(pos,path[0]);
  const call = payload.call;
  for (let i = next; i < path.length; i++) {
    const p = path[i], prev = path[i-1], after = path[i+1];
    if (i > next) metres += distance(prev,p);
    let icon, action;
    const stops = payload.instruction ? [] : (call?.route_stops || []).filter(s =>
      s.node_id === p.id && STOP_GUIDANCE[s.kind] &&
      !(s.kind === "warehouse" && call.tools_taken_at) &&
      !(s.kind === "board" && !payload.vehicle?.pickup && !s.final) &&
      !(call.waypoints || []).some(w => w.node_id === p.id && w.done_at && s.kind === "pass"));
    if (stops.length) {
      icon = STOP_GUIDANCE[stops[0].kind][0];
      action = stops.map(s => STOP_GUIDANCE[s.kind][1]).join(". ");
    } else if (!payload.instruction && call?.waypoints?.some(w => !w.done_at && w.node_id === p.id)) {
      icon = "●";
      const waypoint = call.waypoints.find(w => !w.done_at && w.node_id === p.id);
      action = waypoint.kind === "warehouse" ? "Заберите инструмент" : "Пройдите точку " + (waypoint.name || waypoint.node_id);
    } else if (!after) {
      icon = "⚑";
      action = payload.instruction || "Подойдите к борту";
    } else if (prev) {
      const angle = ((bearingDeg(p.lat,p.lon,after.lat,after.lon) -
        bearingDeg(prev.lat,prev.lon,p.lat,p.lon) + 540) % 360) - 180;
      if (Math.abs(angle) >= 35) {
        [icon, action] = Math.abs(angle) >= 150 ? ["↶","Развернитесь"] :
          angle > 0 ? ["↱","Поверните направо"] : ["↰","Поверните налево"];
      }
    }
    if (action) return {icon, action, metres, key:[call?.id,p.id || [p.lat,p.lon],action].join("/")};
  }
  return null;
}

const VOICE_BODY = '<svg viewBox="0 0 24 24" width="20" height="20" fill="none" stroke="currentColor" stroke-width="2" ' +
  'stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M11 5L6 9H2v6h4l5 4z"/>';
const VOICE_ON = VOICE_BODY + '<path d="M15.5 8.5a5 5 0 0 1 0 7M19 5a10 10 0 0 1 0 14"/></svg>';
const VOICE_OFF = VOICE_BODY + '<path d="M23 9l-6 6M17 9l6 6"/></svg>';

const voice = window.speechSynthesis;
function navigationVoice(voices, online) {
  const russian = voices.filter(v => /^ru(?:[-_]|$)/i.test(v.lang) && (online || v.localService));
  const score = v => (/svetlana|светлана/i.test(v.name) ? 4 : 0) +
    (/natural|neural|premium|enhanced/i.test(v.name) ? 8 : 0) + (v.default ? 1 : 0);
  return russian.sort((a,b) => score(b) - score(a))[0];
}
voice?.getVoices?.(); // Запускает загрузку списка до первой подсказки.
const metreForm = new Intl.PluralRules("ru");
const voiceButton = document.getElementById("nav-voice");
let voiceEnabled = localStorage.getItem("nav-voice") === "on";
let lastAnnouncement = "", latestCue = null;

function updateGuidance(cue) {
  latestCue = cue;
  document.getElementById("nav-guidance").hidden = !cue;
  if (!cue) {
    if (lastAnnouncement) voice?.cancel();
    lastAnnouncement = "";
    return;
  }
  const near = cue.metres <= 12;
  const metres = Math.max(5, Math.round(cue.metres / 5) * 5);
  const line = near ? "Сейчас" : "Прямо " + metres + " м";
  document.getElementById("nav-icon").textContent = near ? cue.icon : "↑";
  document.getElementById("nav-distance").textContent = line;
  document.getElementById("nav-next").textContent = (near ? "" : "Затем " + cue.icon + " ") + cue.action;
  voiceButton.hidden = !voice || !window.SpeechSynthesisUtterance;
  voiceButton.innerHTML = voiceEnabled ? VOICE_ON : VOICE_OFF;
  voiceButton.setAttribute("aria-label", voiceEnabled ? "Отключить озвучку" : "Включить озвучку");
  voiceButton.setAttribute("aria-pressed", String(voiceEnabled));
  const band = near ? "now" : cue.metres <= 50 ? "soon" : "ahead";
  const rank = {ahead: 1, soon: 2, now: 3}[band];
  if (voiceEnabled && !voiceButton.hidden &&
      (lastAnnouncement.key !== cue.key || lastAnnouncement.rank < rank)) {
    lastAnnouncement = {key: cue.key, rank};
    voice.cancel();
    const unit = {one:"метр", few:"метра", many:"метров"}[metreForm.select(metres)] || "метров";
    const message = new SpeechSynthesisUtterance(near ? cue.action :
      "Прямо " + metres + " " + unit + ". Затем. " + cue.action);
    message.lang = "ru-RU";
    const preferred = navigationVoice(voice.getVoices?.() || [], window.navigator?.onLine !== false);
    if (preferred) message.voice = preferred;
    message.rate = 0.97;
    voice.speak(message);
  }
}
voiceButton.onclick = () => {
  voiceEnabled = !voiceEnabled;
  localStorage.setItem("nav-voice", voiceEnabled ? "on" : "off");
  voice?.cancel();
  lastAnnouncement = "";
  updateGuidance(latestCue);
};
