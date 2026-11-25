/*
 * Настройки клиента.
 *
 * Адрес API выводится из адреса страницы: веб-диспетчер раздаётся
 * статикой на одном порту, сервер слушает на другом. Так одна и та же
 * сборка работает и на localhost, и на машине в сети во время демонстрации,
 * без пересборки образа.
 */

const API_PORT = "8000";
const API_STORAGE = "aeroflot.api";

function detectApiBase() {
  // Явное переопределение через ?api=... — нужно, когда сервер поднят
  // на другой машине (например, телефон смотрит на ноутбук).
  // Адрес запоминается: переходы между экранами (вход → смена → рабочий
  // экран) параметр из адресной строки теряют, и без этого он действовал
  // бы только на первой странице.
  const override = new URLSearchParams(window.location.search).get("api");
  if (override) {
    localStorage.setItem(API_STORAGE, override.replace(/\/$/, ""));
  }
  const saved = localStorage.getItem(API_STORAGE);
  if (saved) {
    return saved;
  }
  return `${window.location.protocol}//${window.location.hostname}:${API_PORT}`;
}

const CONFIG = {
  apiBase: detectApiBase(),

  pollIntervalMs: 1000,

  // Точки на карте — отдельным лёгким запросом, дважды в секунду.
  positionsIntervalMs: 500,

  // Нормативы дублируются здесь только для мгновенной подсветки
  // на клиенте. Решение всегда принимает сервер — эти числа ни на что
  // не влияют, кроме цвета.
  regulationLimitMin: 15,
  regulationWarnMin: 12,
  // Возраст GPS-фиксации, после которого метка на карте бледнеет.
  // Тот же порог, что у сервера (POSITION_STALE_S), — только для вида.
  positionStaleS: 90,
};
