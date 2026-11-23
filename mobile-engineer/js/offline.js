/*
 * Работа без связи: память о текущем вызове и очередь неотправленных действий.
 *
 * Эксперты Аэрофлота прямо указали: интернет на перроне нестабилен,
 * приложение обязано работать без него и досылать данные, когда связь
 * вернётся. Поэтому на телефоне хранятся две вещи:
 *
 *   - снимок текущего вызова с временем, когда он получен. Без связи
 *     инженер продолжает видеть, куда ехать и сколько осталось, — с честной
 *     пометкой «данные на 14:05», а не как будто они свежие;
 *   - очередь действий «принял / прибыл / завершил». Каждое получает
 *     идентификатор и время нажатия; сервер применит его один раз
 *     и засчитает по времени нажатия, а не доставки.
 *
 * Раньше данные намеренно не сохранялись вовсе: показать вызов из кэша
 * казалось опаснее, чем не показать ничего. Решение пересмотрено.
 * Опасен не сохранённый вызов, а сохранённый вызов без пометки о возрасте;
 * пустой же экран в зоне без связи оставляет инженера без маршрута ровно
 * тогда, когда идёт отсчёт пятнадцати минут. Сервис-воркер при этом
 * по-прежнему не кэширует ответы API: молчаливый кэш подменял бы данные
 * незаметно, а здесь возраст данных всегда на экране.
 *
 * Хранилище — IndexedDB: оно переживает закрытие приложения и перезагрузку
 * телефона. Если оно недоступно (приватный режим), используется localStorage.
 */

const OFFLINE_DB = "oto-engineer";
const OFFLINE_STORE = "kv";
const KEY_SNAPSHOT = "snapshot";
const KEY_OUTBOX = "outbox";

function openOfflineDb() {
  return new Promise((resolve, reject) => {
    if (!("indexedDB" in window)) {
      reject(new Error("IndexedDB недоступна"));
      return;
    }
    const request = indexedDB.open(OFFLINE_DB, 1);
    request.onupgradeneeded = () => request.result.createObjectStore(OFFLINE_STORE);
    request.onsuccess = () => resolve(request.result);
    request.onerror = () => reject(request.error);
  });
}

function idbRequest(mode, run) {
  return openOfflineDb().then(
    (db) =>
      new Promise((resolve, reject) => {
        const transaction = db.transaction(OFFLINE_STORE, mode);
        const request = run(transaction.objectStore(OFFLINE_STORE));
        transaction.oncomplete = () => resolve(request.result);
        transaction.onerror = () => reject(transaction.error);
      })
  );
}

const OfflineStore = {
  async get(key) {
    try {
      const value = await idbRequest("readonly", (store) => store.get(key));
      return value === undefined ? null : value;
    } catch (error) {
      const raw = localStorage.getItem(`aeroflot.offline.${key}`);
      return raw ? JSON.parse(raw) : null;
    }
  },

  async set(key, value) {
    try {
      await idbRequest("readwrite", (store) => store.put(value, key));
    } catch (error) {
      localStorage.setItem(`aeroflot.offline.${key}`, JSON.stringify(value));
    }
  },
};

/** Идентификатор действия. randomUUID есть только в защищённом контексте. */
function newActionId() {
  if (window.crypto && typeof window.crypto.randomUUID === "function") {
    return window.crypto.randomUUID();
  }
  return `act-${Date.now()}-${Math.random().toString(36).slice(2, 12)}`;
}

/**
 * Очередь неотправленных действий.
 *
 * Отправка строго по порядку: «прибыл» не может уйти раньше «принял».
 * Отказ сети останавливает отправку — действие остаётся в очереди.
 * Отказ сервера по существу (вызов сняли, пока телефон был без связи)
 * действие из очереди убирает: повторять его бессмысленно, а инженеру
 * об этом сообщается.
 */
const Outbox = {
  items: [],

  async load() {
    this.items = (await OfflineStore.get(KEY_OUTBOX)) || [];
  },

  async add(action, callId, fields = {}) {
    const item = { id: newActionId(), action, fields, call_id: callId, at: Date.now() };
    this.items.push(item);
    await OfflineStore.set(KEY_OUTBOX, this.items);
    return item;
  },

  /** Возвращает список отказов сервера; пустой — всё ушло или ждёт связи. */
  async flush() {
    const rejected = [];
    while (this.items.length) {
      const item = this.items[0];
      try {
        await Api.setStatus(item.action, {
          ...item.fields,
          action_id: item.id,
          call_id: item.call_id,
          age_s: Math.max(0, Math.round((Date.now() - item.at) / 1000)),
        });
      } catch (error) {
        if (error.status === 0 || error.status >= 500) {
          break;
        }
        rejected.push({ item, detail: error.detail });
      }
      // Сюда доходят и принятые, и отклонённые сервером действия:
      // и те и другие из очереди уходят.
      this.items.shift();
      await OfflineStore.set(KEY_OUTBOX, this.items);
    }
    return rejected;
  },
};
