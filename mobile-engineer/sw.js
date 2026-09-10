/*
 * Service worker: офлайн-оболочка приложения.
 *
 * Кэшируется только оболочка — разметка, стили, скрипты, иконки.
 * Данные (вызовы, координаты, статусы) не кэшируются никогда:
 * показать инженеру вызов из кэша опаснее, чем не показать ничего.
 * Он может выехать к борту, который уже обслужен, или не увидеть,
 * что вызов переназначен другому.
 *
 * Стратегия для оболочки — «сначала кэш»: приложение обязано
 * открыться на перроне, где связь пропадает.
 */

const CACHE_NAME = "oto-engineer-v1";

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

  event.respondWith(
    caches.match(request).then((cached) => {
      if (cached) {
        // Обновляем кэш в фоне, чтобы следующий запуск был свежим.
        fetch(request)
          .then((response) => {
            if (response.ok) {
              caches.open(CACHE_NAME).then((cache) => cache.put(request, response));
            }
          })
          .catch(() => {});
        return cached;
      }
      return fetch(request);
    })
  );
});
