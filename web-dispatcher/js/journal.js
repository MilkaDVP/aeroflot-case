/**
 * Вкладка «Журнал» в администрировании.
 *
 * Хронология событий системы (п. 3.2): фиксация заявок, подбор
 * кандидатов с затраченным временем, назначения и переназначения,
 * статусы, маршруты, прогнозы и фактическое прибытие, риски и нарушения
 * норматива, ошибки приёма данных, действия пользователей.
 *
 * Кто что видит, решает сервер: технический администратор — всё,
 * администратор аэропорта и начальник смены — свои аэропорты.
 * Выгрузка в CSV и Excel идёт с теми же фильтрами, что на экране.
 */
const Journal = {
  PAGE: 100,
  user: null,
  items: [],
  total: 0,
  ready: false,
  timer: null,
  open: async function (user) {
    this.user = user;
    if (!this.ready) {
      await this.setup();
      this.ready = true;
    }
    await Promise.all([this.load(false), this.checkIntegrity()]);
  },

  async setup() {
    const types = await Api.eventTypes().catch(() => []);
    const categories = new Map();
    for (const item of types) categories.set(item.category, item.category_title);
    const select = byId("journal-category");
    for (const [value, title] of categories) select.add(new Option(title, value));

    if (this.user.role === "admin") {
      byId("journal-airport-box").hidden = false;
      for (const airport of this.user.airports || []) {
        byId("journal-airport").add(new Option(airport.icao, airport.icao));
      }
    }

    const reload = () => this.load(false);
    for (const id of ["journal-from", "journal-to", "journal-category", "journal-airport"]) {
      byId(id).addEventListener("change", reload);
    }
    for (const id of ["journal-q", "journal-call"]) {
      byId(id).addEventListener("input", () => {
        clearTimeout(this.timer);
        this.timer = setTimeout(reload, 300);
      });
    }
    byId("journal-filters").addEventListener("submit", (event) => event.preventDefault());
    byId("journal-reset").addEventListener("click", () => {
      byId("journal-filters").reset();
      reload();
    });
    byId("journal-more").addEventListener("click", () => this.load(true));
    byId("journal-csv").addEventListener("click", () => this.download("csv"));
    byId("journal-xlsx").addEventListener("click", () => this.download("xlsx"));
  },

  /** Фильтры экрана в параметры запроса. Время — местное браузера → UTC. */
  filters() {
    const params = {};
    const from = byId("journal-from").value;
    const to = byId("journal-to").value;
    if (from) params.date_from = new Date(from).toISOString();
    if (to) params.date_to = new Date(to).toISOString();
    const category = byId("journal-category").value;
    if (category) params.category = category;
    const airport = byId("journal-airport").value;
    if (airport) params.airport = airport;
    const q = byId("journal-q").value.trim();
    if (q) params.q = q;
    const call = byId("journal-call").value;
    if (call) params.call_id = call;
    return params;
  },

  async load(more) {
    const offset = more ? this.items.length : 0;
    try {
      const page = await Api.events({ ...this.filters(), limit: this.PAGE, offset });
      this.items = more ? this.items.concat(page.items) : page.items;
      this.total = page.total;
      byId("journal-scope").textContent = `Доступно: ${page.scope}`;
      this.render(more ? page.items : null);
    } catch (error) {
      byId("journal-count").textContent = error.detail;
    }
  },

  async checkIntegrity() {
    const badge = byId("journal-integrity");
    try {
      const result = await Api.eventsIntegrity();
      badge.className = `journal-integrity ${result.ok ? "is-ok" : "is-broken"}`;
      badge.textContent = result.ok
        ? `Журнал неизменен · цепочка из ${result.count} записей проверена`
        : `Нарушена целостность с записи №${result.broken_id}`;
    } catch (error) {
      badge.textContent = error.detail;
    }
  },

  render(appended) {
    const body = byId("journal-rows");
    if (!appended) body.replaceChildren();
    for (const item of appended || this.items) body.append(...this.row(item));
    byId("journal-empty").hidden = this.items.length > 0;
    byId("journal-count").textContent = `Показано ${this.items.length} из ${this.total}`;
    byId("journal-more").hidden = this.items.length >= this.total;
  },

  row(item) {
    const tr = document.createElement("tr");
    tr.className = `journal-row ${tone(item)}`;
    const [date, time] = (item.local_time || item.ts).split(/[ T]/);
    tr.append(
      cellOf(`<span class="journal-time">${esc(time || "")}</span><span class="journal-date">${esc(date)}</span>`),
      cellOf(esc(item.airport_icao || "—")),
      cellOf(`<span class="journal-cat">${esc(item.category_title)}</span>`),
      cellOf(`<div class="journal-type">${esc(item.type_title)}</div><div class="journal-text">${esc(item.text)}</div>`),
      cellOf(item.call_id ? `№${item.call_id}${item.board_number ? `<span class="journal-board">${esc(item.board_number)}</span>` : ""}`
        : esc(item.board_number || "—")),
      cellOf(item.actor_name ? `${esc(item.actor_name)}<span class="journal-role">${esc(item.actor_role || "")}</span>` : '<span class="muted">система</span>'),
      cellOf(esc(item.employee_name || "—")),
      cellOf(item.duration_ms != null ? formatMs(item.duration_ms) : "", "num"),
    );

    const details = document.createElement("tr");
    details.className = "journal-details";
    details.hidden = true;
    const td = document.createElement("td");
    td.colSpan = 8;
    td.append(paramsView(item));
    details.append(td);
    tr.addEventListener("click", () => {
      details.hidden = !details.hidden;
      tr.classList.toggle("is-open", !details.hidden);
    });
    return [tr, details];
  },

  async download(format) {
    const button = byId(format === "csv" ? "journal-csv" : "journal-xlsx");
    button.disabled = true;
    try {
      const { blob, name } = await Api.exportEvents({ ...this.filters(), format });
      const link = document.createElement("a");
      link.href = URL.createObjectURL(blob);
      link.download = name;
      document.body.append(link);
      link.click();
      link.remove();
      setTimeout(() => URL.revokeObjectURL(link.href), 1000);
    } catch (error) {
      window.alert(error.detail);
    } finally {
      button.disabled = false;
    }
  },
};

/** Цвет строки: нарушения и ошибки — красным, риск — жёлтым, норма — зелёным. */
function tone(item) {
  if (["deadline_violation", "server_error", "data_error"].includes(item.type)) return "is-bad";
  if (["deadline_risk", "action_rejected", "login_failed"].includes(item.type)) return "is-warn";
  if (["deadline_met"].includes(item.type)) return "is-good";
  return "";
}

/** Параметры события: «ключ — значение», вложенные списки — строками. */
function paramsView(item) {
  const box = document.createElement("div");
  box.className = "journal-params";
  const head = document.createElement("div");
  head.className = "journal-params-head";
  head.innerHTML = `<span>UTC ${esc(item.ts.replace("T", " ").replace("+00:00", ""))}</span>` +
    `<span>Тип: ${esc(item.type)}</span><span>Запись №${item.id}</span>` +
    `<span class="journal-hash" title="SHA-256 записи">${esc((item.hash || "").slice(0, 16))}…</span>`;
  box.append(head);

  const list = document.createElement("dl");
  for (const [key, value] of Object.entries(item.params || {})) {
    if (value === null || value === "" || (Array.isArray(value) && !value.length)) continue;
    const dt = document.createElement("dt");
    dt.textContent = key.replaceAll("_", " ");
    const dd = document.createElement("dd");
    if (Array.isArray(value) && value.length && typeof value[0] === "object") {
      for (const entry of value) {
        const line = document.createElement("div");
        line.textContent = Object.values(entry).filter((v) => v !== null && v !== "").map(plain).join(" · ");
        dd.append(line);
      }
    } else {
      dd.textContent = plain(value);
    }
    list.append(dt, dd);
  }
  box.append(list);
  return box;
}

function plain(value) {
  if (value === true) return "да";
  if (value === false) return "нет";
  if (typeof value === "object") return JSON.stringify(value);
  return String(value);
}

function formatMs(value) {
  return value >= 100 ? Math.round(value).toString() : value.toFixed(1).replace(".", ",");
}

function cellOf(html, className) {
  const td = document.createElement("td");
  td.innerHTML = html;
  if (className) td.className = className;
  return td;
}

function byId(id) {
  return document.getElementById(id);
}

function esc(value) {
  return String(value ?? "").replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  })[c]);
}
