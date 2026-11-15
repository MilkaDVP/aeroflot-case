/*
 * Форма регистрации вызова с открытым справочником неисправностей.
 *
 * Раньше неисправность выбиралась из выпадающего списка в дюжину пунктов.
 * Эксперты Аэрофлота указали, что такой список не покрывает реальных
 * дефектов, поэтому теперь диспетчер вводит слова экипажа и видит
 * подходящие типы, а если подходящего нет — заводит новый прямо здесь:
 * название, глава ATA и категория допуска. По категории сервер посчитает
 * требуемую отметку, и новый тип сразу участвует в подборе.
 *
 * Поиск выполняет сервер: правила ранжирования должны быть в одном месте,
 * а не повторяться в каждом клиенте.
 */

const CALL_FORM_SEARCH_DELAY_MS = 200;
const CALL_FORM_RESULTS = 7;

// Главы ATA грузятся один раз за сеанс: справочник глав неизменен.
let ataChaptersCache = null;

/**
 * Открывает форму.
 *
 * @param {Object} options
 * @param {Array} options.aircraft борта аэропорта
 * @param {number|null} options.preselectedAircraftId борт, выбранный на карте
 * @param {Function} options.onRegistered вызывается с созданным вызовом
 */
function showCallForm(options) {
  const form = {
    root: buildCallFormMarkup(options),
    // Выбранный тип из справочника, либо null.
    selected: null,
    // «search» — ищем существующий тип, «new» — заводим новый.
    mode: "search",
    results: [],
    active: -1,
    timer: null,
    busy: false,
  };

  document.body.append(form.root);
  bindCallForm(form, options);
  if (options.report) {
    // Слова экипажа уже записаны в сообщении и показаны в шапке формы —
    // второе поле описания только путало бы.
    field(form, "#form-description").closest(".modal-field").hidden = true;
  }
  field(form, "#form-defect-query").focus();
  return form;
}

function buildCallFormMarkup(options) {
  const backdrop = document.createElement("div");
  backdrop.className = "modal-backdrop";

  const aircraftOptions = options.aircraft
    .map(
      (item) =>
        `<option value="${item.id}" ${
          item.id === options.preselectedAircraftId ? "selected" : ""
        }>${escapeHtml(item.board_number)} · ${escapeHtml(item.aircraft_type)} · стоянка ${escapeHtml(
          item.stand_ref || "—"
        )}</option>`
    )
    .join("");

  backdrop.innerHTML = `
    <div class="modal call-form">
      <h2>${options.report ? "Разбор сообщения экипажа" : "Регистрация вызова"}</h2>
      ${reportBanner(options.report)}

      <div class="modal-field">
        <label for="form-aircraft">Воздушное судно</label>
        <select id="form-aircraft">${aircraftOptions}</select>
      </div>

      <div class="modal-field">
        <label for="form-defect-query">Неисправность</label>
        <div class="defect-picked" id="form-defect-picked" hidden></div>
        <div class="defect-search" id="form-defect-search">
          <input id="form-defect-query" autocomplete="off"
                 placeholder="Слова экипажа или глава: «течь», «шасси», «ATA 32»">
          <div class="defect-results" id="form-defect-results" hidden></div>
        </div>
      </div>

      <div class="defect-new" id="form-defect-new" hidden>
        <div class="defect-new-title">Новый тип неисправности</div>
        <div class="modal-field">
          <label for="form-new-name">Название</label>
          <input id="form-new-name" maxlength="120">
        </div>
        <div class="defect-new-row">
          <div class="modal-field">
            <label for="form-new-ata">Глава ATA</label>
            <select id="form-new-ata"></select>
          </div>
          <div class="modal-field">
            <label for="form-new-category">Допуск</label>
            <select id="form-new-category">
              <option value="A">A — простые</option>
              <option value="B1">B1 — механика</option>
              <option value="B2">B2 — авионика</option>
            </select>
          </div>
        </div>
        <div class="defect-new-hint" id="form-new-hint"></div>
      </div>

      <div class="modal-field">
        <label for="form-description">Сообщение экипажа <span class="dim">— необязательно</span></label>
        <textarea id="form-description" rows="2" maxlength="500"
                  placeholder="Подробности для инженера: какая стойка, где течь, что на индикации"></textarea>
      </div>

      <div id="form-error" class="error" hidden></div>
      <div class="modal-actions">
        <button id="form-cancel">Отмена</button>
        <button class="primary" id="form-submit" disabled>${
          options.report ? "Создать вызов" : "Зарегистрировать"
        }</button>
      </div>
    </div>
  `;
  return backdrop;
}

/**
 * Шапка формы при разборе сообщения: что сообщил экипаж и сколько осталось.
 * Диспетчер выбирает тип неисправности, глядя на исходные слова экипажа.
 */
function reportBanner(report) {
  if (!report) {
    return "";
  }
  return `
    <div class="report-banner">
      <div class="report-banner-top">
        <span class="call-board">${escapeHtml(report.board_number)}</span>
        <span class="dim">${escapeHtml(
          report.reporter || SOURCE_TITLES[report.source] || report.source
        )} ·
          сообщено в ${formatClock(report.reported_at)} ·
          срок ${formatClock(report.deadline_at)}</span>
      </div>
      <div class="report-banner-text">«${escapeHtml(report.description)}»</div>
    </div>
  `;
}

function bindCallForm(form, options) {
  const query = field(form, "#form-defect-query");

  query.addEventListener("input", () => scheduleDefectSearch(form));
  query.addEventListener("keydown", (event) => onDefectKeydown(form, event));
  field(form, "#form-new-ata").addEventListener("change", () => onChapterChange(form));
  field(form, "#form-new-category").addEventListener("change", () => updateSubmitState(form));
  field(form, "#form-new-name").addEventListener("input", () => updateSubmitState(form));

  field(form, "#form-cancel").addEventListener("click", () => closeCallForm(form));
  form.root.addEventListener("click", (event) => {
    if (event.target === form.root) {
      closeCallForm(form);
    }
  });
  field(form, "#form-submit").addEventListener("click", () => submitCallForm(form, options));
}

function field(form, selector) {
  return form.root.querySelector(selector);
}

function closeCallForm(form) {
  clearTimeout(form.timer);
  form.root.remove();
}

/* --- Поиск по справочнику --- */

/**
 * Поиск запускается после короткой паузы в наборе: запрос на каждую
 * букву нагружал бы сервер без пользы для диспетчера.
 */
function scheduleDefectSearch(form) {
  clearTimeout(form.timer);
  form.timer = setTimeout(() => runDefectSearch(form), CALL_FORM_SEARCH_DELAY_MS);
}

async function runDefectSearch(form) {
  const text = field(form, "#form-defect-query").value.trim();
  if (!text) {
    form.results = [];
    form.active = -1;
    renderDefectResults(form, "");
    return;
  }

  try {
    const results = await Api.searchDefects(text, CALL_FORM_RESULTS);
    // Пока ждали ответ, диспетчер мог дописать запрос — старый ответ
    // не должен перерисовать подсказки под новый текст.
    if (field(form, "#form-defect-query").value.trim() !== text) {
      return;
    }
    form.results = results;
    form.active = results.length ? 0 : -1;
    renderDefectResults(form, text);
  } catch (error) {
    showFormError(form, error.detail);
  }
}

/**
 * Подсказки под полем поиска.
 *
 * Последняя строка всегда «завести новый тип»: даже если что-то нашлось,
 * найденное может не подходить, и диспетчер не должен подгонять дефект
 * под ближайший пункт справочника.
 */
function renderDefectResults(form, text) {
  const box = field(form, "#form-defect-results");
  box.replaceChildren();

  if (!text) {
    box.hidden = true;
    return;
  }

  form.results.forEach((defect, index) => {
    box.append(buildDefectOption(form, defect, index));
  });

  const create = document.createElement("button");
  create.type = "button";
  create.className = "defect-option defect-option-new";
  create.innerHTML = `＋ Завести новый тип: <b>«${escapeHtml(text)}»</b>`;
  create.addEventListener("click", () => startNewDefect(form, text));
  box.append(create);

  if (!form.results.length) {
    const empty = document.createElement("div");
    empty.className = "defect-empty";
    empty.textContent = "В справочнике такого нет";
    box.prepend(empty);
  }

  box.hidden = false;
}

function buildDefectOption(form, defect, index) {
  const option = document.createElement("button");
  option.type = "button";
  option.className = "defect-option";
  if (index === form.active) {
    option.classList.add("active");
  }
  option.innerHTML = `
    <span class="defect-option-main">
      <span class="defect-option-name">${escapeHtml(defect.name)}</span>
      <span class="defect-option-ata">ATA ${escapeHtml(defect.ata)}${
        defect.ata_title ? ` · ${escapeHtml(defect.ata_title)}` : ""
      }${defect.is_builtin ? "" : " · заведён вручную"}</span>
    </span>
    <span class="mark">${escapeHtml(defect.category)}</span>
  `;
  option.addEventListener("click", () => pickDefect(form, defect));
  return option;
}

/** Стрелки и Enter: диспетчер с телефонной гарнитурой работает с клавиатуры. */
function onDefectKeydown(form, event) {
  const total = form.results.length;
  if (event.key === "ArrowDown" && total) {
    event.preventDefault();
    form.active = (form.active + 1) % total;
    renderDefectResults(form, field(form, "#form-defect-query").value.trim());
  } else if (event.key === "ArrowUp" && total) {
    event.preventDefault();
    form.active = (form.active - 1 + total) % total;
    renderDefectResults(form, field(form, "#form-defect-query").value.trim());
  } else if (event.key === "Enter") {
    event.preventDefault();
    if (form.active >= 0 && form.results[form.active]) {
      pickDefect(form, form.results[form.active]);
    }
  } else if (event.key === "Escape") {
    closeCallForm(form);
  }
}

/** Выбран существующий тип: поиск сворачивается в плашку с кнопкой «изменить». */
function pickDefect(form, defect) {
  form.selected = defect;
  form.mode = "search";
  field(form, "#form-defect-new").hidden = true;
  field(form, "#form-defect-results").hidden = true;
  field(form, "#form-defect-search").hidden = true;

  const picked = field(form, "#form-defect-picked");
  picked.hidden = false;
  picked.innerHTML = `
    <span class="defect-option-main">
      <span class="defect-option-name">${escapeHtml(defect.name)}</span>
      <span class="defect-option-ata">ATA ${escapeHtml(defect.ata)}${
        defect.ata_title ? ` · ${escapeHtml(defect.ata_title)}` : ""
      }</span>
    </span>
    <span class="mark">${escapeHtml(defect.category)}</span>
    <button type="button" class="defect-change">изменить</button>
  `;
  picked.querySelector(".defect-change").addEventListener("click", () => resetDefect(form));

  hideFormError(form);
  updateSubmitState(form);
  field(form, "#form-description").focus();
}

function resetDefect(form) {
  form.selected = null;
  form.mode = "search";
  field(form, "#form-defect-picked").hidden = true;
  field(form, "#form-defect-new").hidden = true;
  field(form, "#form-defect-search").hidden = false;
  const query = field(form, "#form-defect-query");
  query.focus();
  runDefectSearch(form);
  updateSubmitState(form);
}

/* --- Новый тип --- */

async function startNewDefect(form, text) {
  form.selected = null;
  form.mode = "new";
  field(form, "#form-defect-results").hidden = true;
  field(form, "#form-defect-new").hidden = false;
  field(form, "#form-new-name").value = text;

  try {
    await fillChapters(form);
  } catch (error) {
    showFormError(form, error.detail);
  }
  updateSubmitState(form);
}

async function fillChapters(form) {
  if (!ataChaptersCache) {
    ataChaptersCache = await Api.ataChapters();
  }
  const select = field(form, "#form-new-ata");
  if (select.options.length) {
    return;
  }
  select.innerHTML =
    `<option value="">— выберите главу —</option>` +
    ataChaptersCache
      .map(
        (chapter) =>
          `<option value="${escapeHtml(chapter.ata)}">${escapeHtml(chapter.ata)} — ${escapeHtml(
            chapter.title
          )}</option>`
      )
      .join("");
}

/**
 * Смена главы подставляет категорию, которая по ней требуется обычно.
 *
 * Это подсказка, а не правило: диспетчер видит, какую категорию система
 * предлагает и почему, и может её сменить — например, для посадочных фар,
 * которые по главе «Освещение», но работа уровня B1.
 */
function onChapterChange(form) {
  const ata = field(form, "#form-new-ata").value;
  const chapter = (ataChaptersCache || []).find((item) => item.ata === ata);
  const hint = field(form, "#form-new-hint");

  if (chapter) {
    field(form, "#form-new-category").value = chapter.default_category;
    hint.textContent =
      `По главе «${chapter.title}» обычно требуется ${chapter.default_category}. ` +
      "Смените, если работа другого уровня.";
  } else {
    hint.textContent = "";
  }
  updateSubmitState(form);
}

/* --- Отправка --- */

function updateSubmitState(form) {
  const ready =
    form.mode === "new"
      ? field(form, "#form-new-name").value.trim().length >= 3 &&
        Boolean(field(form, "#form-new-ata").value)
      : Boolean(form.selected);
  field(form, "#form-submit").disabled = !ready || form.busy;
}

/**
 * Регистрация вызова.
 *
 * Для нового типа — два шага: сначала тип заводится в справочнике, затем
 * регистрируется вызов. Если такой тип уже есть (сервер отвечает 409),
 * диспетчеру показывается найденное, а не вторая запись того же дефекта.
 */
async function submitCallForm(form, options) {
  hideFormError(form);
  form.busy = true;
  updateSubmitState(form);

  try {
    let code = form.selected ? form.selected.code : null;

    if (form.mode === "new") {
      const created = await Api.createDefectType({
        name: field(form, "#form-new-name").value.trim(),
        ata: field(form, "#form-new-ata").value,
        category: field(form, "#form-new-category").value,
      });
      code = created.code;
    }

    // Разбор входящего сообщения: вызов создаётся из него и наследует
    // время сообщения экипажа — от него идёт отсчёт регламента.
    const aircraftId = Number(field(form, "#form-aircraft").value);
    const call = options.report
      ? await Api.acceptReport(options.report.id, code, aircraftId)
      : await Api.createCall(
          aircraftId,
          code,
          field(form, "#form-description").value.trim() || null
        );
    closeCallForm(form);
    options.onRegistered(call);
  } catch (error) {
    if (error.status === 409 && form.mode === "new") {
      showFormError(form, `${error.detail}. Выберите его в списке.`);
      field(form, "#form-defect-query").value = field(form, "#form-new-name").value;
      field(form, "#form-defect-new").hidden = true;
      form.mode = "search";
      await runDefectSearch(form);
    } else {
      showFormError(form, error.detail);
    }
  } finally {
    form.busy = false;
    updateSubmitState(form);
  }
}

function showFormError(form, message) {
  const box = field(form, "#form-error");
  box.hidden = false;
  box.textContent = message;
}

function hideFormError(form) {
  field(form, "#form-error").hidden = true;
}
