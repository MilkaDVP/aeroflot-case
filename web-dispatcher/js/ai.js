/*
 * ИИ-диспетчер — кнопка в шапке.
 *
 * Панель справа, сверху вниз:
 *   пояснение обстановки (GigaChat, без него — встроенные правила);
 *   одна главная кнопка «Назначить всех, кто успевает»;
 *   карточка на каждый вызов: кто поедет и через сколько будет у борта,
 *   «Назначить», значки «на карте» и «открыть»;
 *   внизу — агент: просьба словами, он выполняет её на экране (agent.js).
 *
 * Пока панель открыта, план обновляется сам (без нового пояснения).
 * Значок на кнопке показывает, сколько вызовов можно назначить сразу.
 * Агент действует от имени диспетчера; права проверяет сервер, важные
 * действия подтверждает человек.
 */

const AI_REFRESH_MS = 15000;
const AI_BADGE_MS = 20000;

const AiPanel = {
  root: null,
  button: null,
  plan: null,
  busy: false,
  onChanged: null,
  onOpenCall: null,
  onPreview: null,
  refreshTimer: null,
  answers: [],

  init(button, root, { onChanged, onOpenCall, onPreview }) {
    const user = Session.user;
    this.historyKey = JSON.stringify(["oto.ai", user.login, user.airport_icao, user.shift]);
    try { this.answers = JSON.parse(sessionStorage.getItem(this.historyKey) || "[]"); } catch { this.answers = []; }
    this.button = button;
    this.root = root;
    this.onChanged = onChanged;
    this.onOpenCall = onOpenCall;
    this.onPreview = onPreview;
    button.hidden = false;
    button.innerHTML = 'ИИ-диспетчер<span class="ai-badge" hidden></span>';
    button.addEventListener("click", () => this.toggle());
    document.addEventListener("keydown", (event) => {
      if (event.key === "Escape" && !this.root.hidden) this.close();
    });
    this.updateBadge();
    setInterval(() => this.updateBadge(), AI_BADGE_MS);
  },

  /** Сколько вызовов можно назначить прямо сейчас — значок на кнопке. */
  async updateBadge() {
    if (!this.root.hidden) return;
    try {
      const plan = await Api.aiPlan(false);
      this.setBadge(assignable(plan.items).length);
    } catch (error) {
      // значок — подсказка, без него можно
    }
  },

  setBadge(count) {
    const badge = this.button.querySelector(".ai-badge");
    badge.hidden = !count;
    badge.textContent = count ? String(count) : "";
  },

  toggle() {
    if (this.root.hidden) this.open();
    else this.close();
  },

  async open() {
    this.root.hidden = false;
    this.button.classList.add("active");
    this.button.setAttribute("aria-expanded", "true");
    this.renderShell();
    await this.load(true);
    clearInterval(this.refreshTimer);
    this.refreshTimer = setInterval(() => this.load(false), AI_REFRESH_MS);
  },

  close() {
    this.root.hidden = true;
    this.button.classList.remove("active");
    this.button.setAttribute("aria-expanded", "false");
    clearInterval(this.refreshTimer);
    if (this.onPreview && !this.working) this.onPreview(null);
    this.updateBadge();
  },

  renderShell() {
    this.root.innerHTML = `
      <div class="ai-head">
        <span class="ai-title">ИИ-диспетчер</span>
        <span class="ai-engine" id="ai-engine"></span>
        <button type="button" class="ai-icon" id="ai-refresh" title="Обновить и заново объяснить" aria-label="Обновить">⟳</button>
        <button type="button" class="ai-icon" id="ai-close" aria-label="Закрыть">×</button>
      </div>
      <div class="ai-body">
        <div class="ai-summary is-loading" id="ai-summary">
          <div class="ai-thinking"><span></span><span></span><span></span> разбираю обстановку</div>
        </div>
        <button type="button" class="primary ai-assign-all" id="ai-assign-all" disabled>Назначить всех</button>
        <div class="ai-list" id="ai-list"></div>
      </div>
      <form class="ai-ask" id="ai-ask">
        <div class="ai-answers" id="ai-answers"></div>
        <div class="ai-ask-row">
          <input id="ai-question" maxlength="1000" autocomplete="off" placeholder="Что сделать?">
          <button type="submit" class="primary" id="ai-send" aria-label="Выполнить">➤</button>
        </div>
      </form>`;
    this.root.querySelector("#ai-close").addEventListener("click", () => this.close());
    this.root.querySelector("#ai-refresh").addEventListener("click", () => this.load(true));
    this.root.querySelector("#ai-assign-all").addEventListener("click", () => this.assignAll());
    this.root.querySelector("#ai-ask").addEventListener("submit", (event) => this.ask(event));
    this.renderAnswers();
    if (this.working) this.setWorking(true);
  },

  async load(explain) {
    if (explain) {
      const summary = this.root.querySelector("#ai-summary");
      summary.classList.add("is-loading");
      summary.innerHTML = '<div class="ai-thinking"><span></span><span></span><span></span> разбираю обстановку</div>';
    }
    try {
      const plan = await Api.aiPlan(explain);
      // Без пояснения план приходит с шаблонным текстом — оставляем
      // прежнее пояснение GigaChat, пока его не попросят обновить.
      if (!explain && this.plan) {
        plan.summary = this.plan.summary;
        plan.summary_source = this.plan.summary_source;
      }
      this.plan = plan;
      this.render(explain);
    } catch (error) {
      this.root.querySelector("#ai-summary").innerHTML =
        `<div class="error">${escapeHtml(error.detail || "Не удалось получить план")}</div>`;
    }
  },

  render(withSummary) {
    const plan = this.plan;
    // Назначить можно всех, у кого план нашёл инженера: и тех, кто
    // успевает, и «нужно решение» — по порядку срочности.
    const ready = assignable(plan.items).length;
    this.setBadge(ready);

    const engine = this.root.querySelector("#ai-engine");
    const live = plan.summary_source === "gigachat";
    // Подпись — подключён ли GigaChat. Без него план всё равно считается
    // (это встроенный подбор: допуск, маршрут, 15 минут), а короткое
    // пояснение пишется по шаблону.
    const failed = plan.gigachat_configured && !live && plan.items.length > 0;
    engine.textContent = !plan.gigachat_configured ? "без GigaChat" : failed ? "GigaChat не ответил" : "GigaChat";
    engine.className = `ai-engine ${plan.gigachat_configured && !failed ? "is-live" : ""}`;
    engine.title = !plan.gigachat_configured
      ? "GigaChat не подключён: план строит встроенный подбор, пояснение — по шаблону"
      : failed ? "GigaChat не ответил — пояснение по шаблону" : "Пояснения и ответы пишет GigaChat";

    if (withSummary) {
      const summary = this.root.querySelector("#ai-summary");
      summary.classList.remove("is-loading");
      summary.innerHTML = `<p class="ai-typed"></p>`;
      typeText(summary.querySelector(".ai-typed"), plan.summary);
    }

    const all = this.root.querySelector("#ai-assign-all");
    all.disabled = !ready || this.busy;
    all.textContent = ready ? `Назначить всех · ${ready}` : "Назначать некого";

    const list = this.root.querySelector("#ai-list");
    list.replaceChildren();
    if (!plan.items.length) {
      list.innerHTML = '<div class="ai-empty">✓ Ждущих вызовов нет</div>';
    }
    plan.items.forEach((item, index) => {
      const card = this.card(item);
      card.style.animationDelay = `${Math.min(index, 8) * 40}ms`;
      list.append(card);
    });
  },

  card(item) {
    const row = document.createElement("article");
    // Горит или срочный — подсветка как у карточки в «Активных вызовах».
    row.className = `ai-item is-${item.verdict}${item.alert ? " is-alert" : ""}${
      item.aog || item.urgent ? " is-urgent" : ""}`;
    row.dataset.callId = item.call_id;
    // Отметки — те же, что в «Активных вызовах».
    const flags = (item.aog ? '<span class="flag flag-aog">AOG</span>' : "") +
      (item.urgent ? '<span class="flag flag-urgent">срочный</span>' : "");
    const left = item.minutes_left == null ? "" : item.minutes_left > 0
      ? `<span class="ai-left num">${Math.ceil(item.minutes_left)} мин</span>`
      : '<span class="ai-left num is-over">срок истёк</span>';

    row.innerHTML = `
      <div class="ai-item-top">
        <span class="call-board">${escapeHtml(item.board_number)}</span>${flags}
        ${left}
      </div>
      <div class="ai-item-what" title="${escapeHtml(item.aircraft_type)} · ${escapeHtml(item.required_mark)}">${
        item.stand_ref ? `ст. ${escapeHtml(item.stand_ref)}` : "вне стоянки"} · ${escapeHtml(item.defect_name)}</div>
      ${item.employee_name ? this.engineerBlock(item) : ""}
      ${item.verdict !== "assign" ? `<div class="ai-reason">${escapeHtml(item.reason)}</div>` : ""}
      <div class="ai-item-actions"></div>`;

    const actions = row.querySelector(".ai-item-actions");
    if (item.employee_id) {
      const assign = document.createElement("button");
      assign.type = "button";
      assign.className = "primary ai-assign";
      assign.textContent = "Назначить";
      if (item.needs_order) {
        // Лучший для этого вызова нужен на более срочном. Когда тот
        // назначен, этот кандидат станет лучшим и здесь — поэтому
        // по порядку: «Назначить всех» делает именно так.
        assign.disabled = true;
        assign.title = "Сначала назначьте более срочный вызов — «Назначить всех» сделает это по порядку";
      }
      assign.addEventListener("click", () => this.assign([item]));
      actions.append(assign);
    }
    if (item.route_node_ids.length || item.stand_node_id) {
      actions.append(iconButton("⌖", "Показать на карте", () => this.preview(item, row)));
    }
    actions.append(iconButton("↗", "Открыть вызов", () => {
      if (this.onOpenCall) this.onOpenCall(item.call_id);
    }));
    return row;
  },

  /** Кто поедет: инициалы, имя, через сколько будет у борта. */
  engineerBlock(item) {
    const eta = item.eta_minutes || 0;
    const initials = item.employee_name.split(/\s+/).slice(0, 2).map((part) => part[0]).join("");
    const vehicle = item.vehicle_call_sign ? ` · ${escapeHtml(item.vehicle_call_sign)}` : "";
    return `
      <div class="ai-engineer">
        <span class="ai-avatar">${escapeHtml(initials)}</span>
        <div class="ai-engineer-main">
          <div><b>${escapeHtml(item.employee_name)}</b><span class="muted"> · ${Math.round(eta)} мин${vehicle}</span></div>
        </div>
      </div>`;
  },

  preview(item, row) {
    for (const card of this.root.querySelectorAll(".ai-item.is-previewed")) {
      card.classList.remove("is-previewed");
    }
    row.classList.add("is-previewed");
    if (this.onPreview) this.onPreview(item);
  },

  async assignAll() {
    await this.assign(assignable(this.plan.items));
  },

  /** Назначает по плану; каждый вызов отдельно — отказ одного не мешает другим. */
  async assign(items) {
    if (this.busy || !items.length) return;
    this.busy = true;
    this.root.querySelector("#ai-assign-all").disabled = true;
    const failed = [];
    for (const item of items) {
      const card = this.root.querySelector(`.ai-item[data-call-id="${item.call_id}"]`);
      card?.classList.add("is-working");
      try {
        await Api.assign(item.call_id, item.employee_id, null, false, "auto");
        card?.classList.replace("is-working", "is-done");
      } catch (error) {
        card?.classList.remove("is-working");
        failed.push(`${item.board_number}: ${error.detail}`);
      }
    }
    this.busy = false;
    const done = items.length - failed.length;
    if (done) showToast(`Назначено по плану: ${done}`);
    if (failed.length) showToast(failed.join("; "), true);
    if (this.onPreview) this.onPreview(null);
    if (this.onChanged) await this.onChanged();
    // Дать увидеть «✓», затем план без назначенных.
    await new Promise((resolve) => setTimeout(resolve, 450));
    await this.load(false);
  },

  /* --- Агент --- */

  async ask(event) {
    event.preventDefault();
    // Пока агент работает, та же кнопка его останавливает.
    if (this.working) {
      Agent.stop();
      this.setWorking(true, true);
      return;
    }
    const input = this.root.querySelector("#ai-question");
    const question = input.value.trim();
    if (!question) return;
    input.value = "";
    const entry = { question, answer: null, steps: [] };
    const previous = this.answers.filter(turn => turn.answer && turn.source !== "error");
    this.answers.push(entry);
    this.answers = this.answers.slice(-10);
    this.setWorking(true);
    this.renderAnswers();
    try {
      entry.answer = await Agent.run(question, (title) => {
        const step = { title, note: "", kind: "working" };
        entry.steps.push(step);
        this.renderAnswers();
        return (note, kind) => {
          step.note = note;
          step.kind = kind;
          this.renderAnswers();
        };
      }, previous);
      entry.readExternal = Agent.readExternal;
      entry.source = "gigachat";
      entry.idle = !entry.steps.some((step) => step.kind === "done");
    } catch (error) {
      entry.answer = error.detail || "Не удалось выполнить";
      entry.source = "error";
      // Без GigaChat агент не работает, но план по правилам остаётся.
      if (error.status === 503 && !entry.steps.length) {
        try {
          entry.answer = (await Api.aiAsk(question.slice(0, 500), previous.map(({question, answer}) => ({question:question.slice(0,1000), answer:answer.slice(0,1200)})))).answer;
        } catch (again) { /* остаётся исходная ошибка */ }
      }
    }
    try { sessionStorage.setItem(this.historyKey, JSON.stringify(this.answers)); } catch { /* История остаётся в памяти. */ }
    this.setWorking(false);
    this.renderAnswers(true);
    if (!this.root.hidden) this.load(false);
  },

  setWorking(working, stopping) {
    this.working = working;
    const button = this.root.querySelector("#ai-send");
    if (!button) return;
    button.textContent = working ? "■" : "➤";
    button.disabled = Boolean(stopping);
    button.setAttribute("aria-label", working ? "Остановить" : "Выполнить");
  },

  renderAnswers(typeLast) {
    const box = this.root.querySelector("#ai-answers");
    if (!box) return;
    box.replaceChildren();
    this.answers.forEach((entry, index) => {
      const item = document.createElement("div");
      item.className = "ai-qa";
      item.innerHTML = `<div class="ai-q">${escapeHtml(entry.question)}</div>`;
      for (const step of entry.steps || []) {
        const row = document.createElement("div");
        row.className = `ai-step is-${step.kind}`;
        row.textContent = step.note && step.kind !== "done" ? `${step.title} — ${step.note}` : step.title;
        item.append(row);
      }
      const answer = document.createElement("div");
      answer.className = `ai-a ${entry.source === "gigachat" ? "is-live" : ""}`;
      if (entry.answer === null) {
        answer.innerHTML = '<div class="ai-thinking"><span></span><span></span><span></span></div>';
      } else if (typeLast && index === this.answers.length - 1) {
        typeText(answer, entry.answer);
      } else {
        answer.textContent = entry.answer;
      }
      item.append(answer);
      if (entry.idle) {
        // Что бы ни написала модель, факт говорит экран.
        const note = document.createElement("div");
        note.className = "ai-idle";
        note.textContent = "Действий не выполнено";
        item.append(note);
      }
      box.append(item);
    });
    box.scrollTop = box.scrollHeight;
  },
};

/** Текст появляется «печатью» — видно, что это ответ ИИ, а не справка. */
function typeText(node, text) {
  const reduce = window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  if (reduce || !text) {
    node.textContent = text || "";
    return;
  }
  node.textContent = "";
  let index = 0;
  const step = () => {
    index = Math.min(text.length, index + 3);
    node.textContent = text.slice(0, index);
    if (index < text.length) setTimeout(step, 12);
  };
  step();
}

/** Небольшая кнопка-значок с подсказкой. */
function iconButton(symbol, title, onClick) {
  const button = document.createElement("button");
  button.type = "button";
  button.className = "ai-icon-action";
  button.textContent = symbol;
  button.title = title;
  button.setAttribute("aria-label", title);
  button.addEventListener("click", onClick);
  return button;
}

/** Кого можно назначить: план нашёл инженера. Порядок — срочность плана. */
function assignable(items) {
  return items.filter((item) => item.employee_id);
}
