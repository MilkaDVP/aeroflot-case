/*
 * Определение местоположения инженера.
 *
 * Два источника: настоящий GPS телефона и режим имитации.
 *
 * Режим имитации нужен не для отладки. По условиям конкурсного задания
 * реальный GPS не требуется, координаты допускается задавать вручную,
 * а записать демонстрационное видео с настоящим перемещением по перрону
 * Шереметьево невозможно. Поэтому имитация — полноценный рабочий режим:
 * координаты задаются касанием карты или вводом с клавиатуры.
 */

const GEO_OPTIONS = {
  enableHighAccuracy: true,
  // На перроне между зданиями фиксация занимает время; лучше подождать,
  // чем получить отказ и оставить диспетчера без координат.
  timeout: 15000,
  maximumAge: 5000,
};

class Locator {
  /**
   * @param {Function} onPosition вызывается при каждой новой позиции
   * @param {Function} onError сообщает о проблеме с определением места
   */
  constructor(onPosition, onError) {
    this.onPosition = onPosition;
    this.onError = onError || (() => {});
    this.watchId = null;
    this.simulated = null;
    this.last = null;
  }

  get mode() {
    return this.simulated ? "simulation" : "gps";
  }

  /** Включает слежение за настоящими координатами. */
  startGps() {
    this.simulated = null;

    if (!("geolocation" in navigator)) {
      this.onError("Устройство не умеет определять местоположение");
      return false;
    }

    this.stopGps();
    this.watchId = navigator.geolocation.watchPosition(
      (position) => this.publish(position.coords.latitude, position.coords.longitude,
                                 position.coords.accuracy),
      (error) => this.onError(describeGeoError(error)),
      GEO_OPTIONS
    );
    return true;
  }

  stopGps() {
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
    this.stopGps();
    this.simulated = { lat, lon };
    this.publish(lat, lon, null);
  }

  publish(lat, lon, accuracy) {
    this.last = { lat, lon, accuracy };
    this.onPosition(this.last);
  }
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
