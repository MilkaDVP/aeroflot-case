/*
 * Настройки клиента.
 *
 * Адрес API выводится из адреса страницы: веб-диспетчер раздаётся
 * статикой на одном порту, сервер слушает на другом. Так одна и та же
 * сборка работает и на localhost, и на машине в сети во время демонстрации,
 * без пересборки образа.
 */

const API_PORT = "8000";

function detectApiBase() {
  // Явное переопределение через ?api=... — нужно, когда сервер поднят
  // на другой машине (например, телефон смотрит на ноутбук).
  const override = new URLSearchParams(window.location.search).get("api");
  if (override) {
    return override.replace(/\/$/, "");
  }
  return `${window.location.protocol}//${window.location.hostname}:${API_PORT}`;
}

const CONFIG = {
  apiBase: detectApiBase(),

  // Опрос данных с сервера. ТЗ допускает 3–5 секунд: WebSocket для
  // демонстрации не нужен, а polling проще и надёжнее.
  pollIntervalMs: 4000,

  // Нормативы дублируются здесь только для мгновенной подсветки
  // на клиенте. Решение всегда принимает сервер — эти числа ни на что
  // не влияют, кроме цвета.
  regulationLimitMin: 15,
  regulationWarnMin: 12,
};
