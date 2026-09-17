/*
 * Service worker: офлайн-оболочка приложения.
 *
 * Кэшируется только оболочка — разметка, стили, скрипты, иконки.
 * Данные (вызовы, координаты, статусы) не кэшируются никогда:
 * показать инженеру вызов из кэша опаснее, чем не показать ничего.
 * Он может выехать к борту, который уже обслужен, или не увидеть,
 * что вызов переназначен другому.
 *
 * Стратегия для оболочки — «сначала сеть, при её отказе кэш». Раньше было
 * «сначала кэш»: приложение открывалось мгновенно, но после обновления
 * инженер видел прежнюю версию, а новая применялась лишь со следующего
 * запуска — исправления просто не доходили до телефона. Теперь при связи
 * загружается свежая оболочка, а на перроне без связи приложение
 * открывается из кэша: сеть ждём не дольше NETWORK_TIMEOUT_MS.
 */

// Номер версии меняется вместе со сменой стратегии: activate удалит
// кэш прежней версии, и старая оболочка не всплывёт при отказе сети.
const CACHE_NAME = "oto-engineer-v2";

// Сколько ждать сеть, прежде чем открыть оболочку из кэша. На перроне
// связь не пропадает мгновенно, а «висит»: без предела приложение
// не открылось бы вовсе.
const NETWORK_TIMEOUT_MS = 3000;

const SHELL = [
  "index.html",
  "manifest.json",
  "css/app.css",
  "js/api.js",
  "js/geo.js",
  "js/routemap.js",
  "js/app.js",
  "icons/icon.svg",
];

/**
 * Кэширует оболочку по одному файлу, а не через addAll.
 *
 * addAll устроен по принципу «всё или ничего»: если хотя бы один файл
 * не отдался, отклоняется вся операция, установка срывается, и офлайн
 * не работает вообще. Один переименованный значок не должен оставлять
 * инженера без приложения на перроне, поэтому неудачи по отдельным
 * файлам переживаем и продолжаем.
 */
async function cacheShell() {
  const cache = await caches.open(CACHE_NAME);
  const failed = [];

  for (const url of SHELL) {
    try {
      await cache.add(url);
    } catch (error) {
      failed.push(url);
    }
  }

  if (failed.length) {
    console.warn("Не попали в офлайн-кэш:", failed.join(", "));
  }
}

self.addEventListener("install", (event) => {
  event.waitUntil(
    cacheShell()
      // Новая версия оболочки должна применяться сразу: инженер
      // не станет закрывать все вкладки ради обновления.
      .then(() => self.skipWaiting())
      .catch((error) => {
        // Хранилище может быть недоступно (приватный режим, нехватка
        // места). Приложение обязано работать и без офлайн-кэша.
        console.warn("Офлайн-кэш недоступен:", error.message);
        return self.skipWaiting();
      })
  );
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches
      .keys()
      .then((names) =>
        Promise.all(names.filter((name) => name !== CACHE_NAME).map((name) => caches.delete(name)))
      )
      .then(() => self.clients.claim())
  );
});

self.addEventListener("fetch", (event) => {
  const request = event.request;

  // Всё, что не GET, и всё, что идёт в API, — только по сети.
  if (request.method !== "GET" || request.url.includes("/api/")) {
    return;
  }

  event.respondWith(networkFirst(request));
});

/**
 * Свежий файл из сети с сохранением в кэш; при отказе или долгом ответе —
 * копия из кэша.
 */
async function networkFirst(request) {
  try {
    const response = await fetchWithTimeout(request, NETWORK_TIMEOUT_MS);
    if (response.ok) {
      const copy = response.clone();
      caches.open(CACHE_NAME).then((cache) => cache.put(request, copy));
    }
    return response;
  } catch (error) {
    const cached = await caches.match(request);
    if (cached) {
      return cached;
    }
    throw error;
  }
}

/** fetch, который сдаётся через заданное время. */
function fetchWithTimeout(request, timeoutMs) {
  return Promise.race([
    fetch(request),
    new Promise((resolve, reject) => {
      setTimeout(() => reject(new Error("сеть не ответила вовремя")), timeoutMs);
    }),
  ]);
}
