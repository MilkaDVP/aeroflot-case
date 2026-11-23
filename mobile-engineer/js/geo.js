/*
 * Определение местоположения инженера.
 *
 * Два источника: настоящий GPS телефона и режим имитации.
 *
 * Режим имитации нужен не для отладки. По условиям конкурсного задания
 * реальный GPS не требуется, координаты допускается задавать вручную,
 * а записать демонстрационное видео с настоящим перемещением по перрону
 * Шереметьево невозможно. Поэтому имитация — полноценный рабочий режим:
 * координаты задаются касанием карты.
 */

const GEO_OPTIONS = {
  enableHighAccuracy: true,
  // На перроне между зданиями фиксация занимает время; лучше подождать,
  // чем получить отказ и оставить диспетчера без координат.
  timeout: 15000,
  maximumAge: 5000,
};

// Через сколько фиксация считается постаревшей и как часто это проверять.
const GEO_STALE_MS = 6000;
const GEO_POLL_MS = 3000;

class Locator {
  /**
   * @param {Function} onPosition вызывается при каждой новой позиции
   * @param {Function} onError сообщает о проблеме с определением места
   */
  constructor(onPosition, onError) {
    this.onPosition = onPosition;
    this.onError = onError || (() => {});
    this.watchId = null;
    this.pollId = null;
    this.polling = false;
    this.mode = "gps";
    this.last = null;
  }

  startManual() {
    this.stopGps();
    this.mode = "simulation";
    this.last = null;
  }

  /** Включает слежение за настоящими координатами. */
  startGps() {
    this.mode = "gps";
    this.last = null;

    if (!("geolocation" in navigator)) {
      this.onError("Устройство не умеет определять местоположение");
      return false;
    }

    this.stopGps();
    // Неподвижный телефон сам новых точек почти не присылает: если фиксация
    // постарела, запрашиваем свежую, иначе прибытие не засчитается.
    this.pollId = setInterval(() => this.poll(), GEO_POLL_MS);
    this.watchId = navigator.geolocation.watchPosition(
      (position) => this.mode === "gps" && this.publish(position.coords.latitude, position.coords.longitude,
                                 position.coords.accuracy, position.timestamp, position.coords.heading),
      (error) => this.mode === "gps" && this.onError(describeGeoError(error)),
      GEO_OPTIONS
    );
    return true;
  }

  poll() {
    if (this.mode !== "gps" || document.hidden || this.polling) return;
    if (this.last && Date.now() - this.last.measuredAt < GEO_STALE_MS) return;
    this.polling = true;
    navigator.geolocation.getCurrentPosition(
      (position) => {
        this.polling = false;
        const { coords, timestamp } = position;
        if (this.mode !== "gps" || this.watchId === null) return;
        if (this.last && timestamp <= this.last.measuredAt) return;
        this.publish(coords.latitude, coords.longitude, coords.accuracy, timestamp, coords.heading);
      },
      () => { this.polling = false; },
      { enableHighAccuracy: true, timeout: 8000, maximumAge: 0 }
    );
  }

  stopGps() {
    clearInterval(this.pollId);
    this.polling = false;
    if (this.watchId !== null) {
      navigator.geolocation.clearWatch(this.watchId);
      this.watchId = null;
    }
  }

  /**
   * Переводит в режим имитации и ставит точку.
   *
   * Слежение за настоящим GPS при этом выключается: иначе очередная
   * фиксация спутников перетёрла бы заданную вручную точку прямо
   * во время демонстрации.
   */
  simulate(lat, lon) {
    this.mode = "simulation";
    this.publish(lat, lon, null, Date.now());
  }

  /**
   * Новая точка.
   *
   * measuredAt — момент фиксации по часам телефона. Серверу уходит не он,
   * а возраст фиксации: часы телефона могут расходиться с сервером на
   * минуты, а разность двух моментов на одном устройстве от этого не зависит.
   */
  publish(lat, lon, accuracy, measuredAt, heading = null) {
    this.last = {
      lat,
      lon,
      accuracy, heading,
      measuredAt: measuredAt || Date.now(),
      source: this.mode,
    };
    this.onPosition(this.last);
  }
}

/* --- Прибытие к борту --- */

const ARRIVAL_RADIUS_M = 25;
// Посредственный GPS: засчитываем, если простоять дольше и рядом нет
// стоянки ближе. Хуже — только кнопкой: соседние борта в 45 метрах.
const ARRIVAL_GOOD_M = 25;
const ARRIVAL_FAIR_M = 50;
const ARRIVAL_GOOD_MS = 10000;
const ARRIVAL_FAIR_MS = 20000;
const ARRIVAL_FRESH_MS = 15000;
// Начатый отсчёт переживает паузу в точках: остановился — телефон молчит.
const ARRIVAL_HOLD_MS = 60000;
// Рядом с бортом, с учётом погрешности, — можно отметить прибытие кнопкой.
const ARRIVAL_MANUAL_M = 100;

/**
 * Что с прибытием сейчас.
 * since — с какого момента инженер у борта (0 — не у борта).
 * Возвращает { since, done, left, manual, hint }.
 */
function arrivalCheck(pos, target, since, now, isNearest = true) {
  const none = (manual, hint) => ({ since: 0, done: false, left: null, manual, hint });
  if (!target) return none(true, "");
  if (!pos) return none(true, "Нет GPS");
  const distance = haversineM(pos.lat, pos.lon, target.lat, target.lon);
  const gps = pos.source === "gps";
  const accuracy = gps ? pos.accuracy ?? Infinity : 0;
  const age = gps ? now - pos.measuredAt : 0;
  const known = Number.isFinite(accuracy) ? accuracy : 0;
  if (age > (since ? ARRIVAL_HOLD_MS : ARRIVAL_FRESH_MS)) return none(true, "GPS давно не обновлялся");
  if (distance - known > ARRIVAL_MANUAL_M) return none(false, "");
  if (accuracy > ARRIVAL_FAIR_M) {
    return none(true, Number.isFinite(accuracy) ? `GPS неточный: ±${Math.round(accuracy)} м` : "GPS неточный");
  }
  if (distance > ARRIVAL_RADIUS_M) return none(true, `До борта ${Math.round(distance)} м`);
  const fair = accuracy > ARRIVAL_GOOD_M;
  if (fair && !isNearest) return none(true, `GPS неточный: ±${Math.round(accuracy)} м`);
  const start = since || now;
  const left = Math.max(0, (fair ? ARRIVAL_FAIR_MS : ARRIVAL_GOOD_MS) - (now - start));
  // Засчитываем только по свежей точке: прошёл мимо и потерял GPS — не прибыл.
  if (!left && age > ARRIVAL_FRESH_MS) return { since: start, done: false, left: 0, manual: true, hint: "Жду GPS" };
  return { since: start, done: left === 0, left, manual: true, hint: left ? `Прибытие через ${Math.ceil(left / 1000)} с` : "" };
}

function describeGeoError(error) {
  if (error.code === error.PERMISSION_DENIED) {
    return "Доступ к геопозиции запрещён. Разрешите его в настройках браузера";
  }
  if (error.code === error.POSITION_UNAVAILABLE) {
    return "Не удаётся определить местоположение";
  }
  if (error.code === error.TIMEOUT) {
    return "Определение местоположения заняло слишком долго";
  }
  return "Ошибка определения местоположения";
}

/* --- Геометрия --- */

const EARTH_RADIUS_M = 6371008.8;

/** Расстояние по прямой между двумя точками, в метрах. */
function haversineM(lat1, lon1, lat2, lon2) {
  const toRad = Math.PI / 180;
  const dPhi = (lat2 - lat1) * toRad;
  const dLambda = (lon2 - lon1) * toRad;
  const a =
    Math.sin(dPhi / 2) ** 2 +
    Math.cos(lat1 * toRad) * Math.cos(lat2 * toRad) * Math.sin(dLambda / 2) ** 2;
  return 2 * EARTH_RADIUS_M * Math.asin(Math.sqrt(a));
}

/**
 * Азимут на цель в градусах от севера.
 *
 * Нужен для стрелки «куда идти»: человек на ходу смотрит в телефон
 * секунду, и направление читается быстрее, чем ломаная маршрута.
 */
function bearingDeg(lat1, lon1, lat2, lon2) {
  const toRad = Math.PI / 180;
  const phi1 = lat1 * toRad;
  const phi2 = lat2 * toRad;
  const dLambda = (lon2 - lon1) * toRad;

  const y = Math.sin(dLambda) * Math.cos(phi2);
  const x =
    Math.cos(phi1) * Math.sin(phi2) - Math.sin(phi1) * Math.cos(phi2) * Math.cos(dLambda);
  return (Math.atan2(y, x) * 180) / Math.PI;
}
