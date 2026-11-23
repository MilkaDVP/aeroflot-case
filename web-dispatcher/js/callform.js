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
 * Слова экипажа вводятся один раз. Раньше их просили дважды: в поле поиска
 * типа и ниже, в поле «Сообщение экипажа». Теперь поле одно: по нему
 * подбирается тип, и этот же текст уходит инженеру как описание. При
 * разборе сообщения с планшета слова экипажа уже есть — тип предлагается
 * по ним сразу, перепечатывать ничего не нужно.
 *
 * Поиск выполняет сервер: правила ранжирования должны быть в одном месте,
 * а не повторяться в каждом клиенте.
 */

const CALL_FORM_SEARCH_DELAY_MS = 200;
const CALL_FORM_RESULTS = 7;
// Фраза экипажа разбирается по словам; короче трёх букв — предлоги и союзы.
const CALL_FORM_WORD_MIN = 3;
const CALL_FORM_WORDS_MAX = 8;
// Длинную фразу экипажа нельзя подставлять как название нового типа.
const CALL_FORM_NAME_FROM_TEXT_MAX = 40;

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
    // Сообщение экипажа при разборе входящего; иначе null.
    report: options.report || null,
  };

  document.body.append(form.root);
  // Точки маршрута: список мест аэропорта и выбор на карте. На время
  // выбора на карте форма уходит в сторону, чтобы карта была видна.
  form.waypoints = options.graph ? new WaypointPicker({
    graph: options.graph,
    map: options.map,
    onPickStart: () => form.root.classList.add("is-picking"),
    onPickEnd: () => form.root.classList.remove("is-picking"),
  }) : null;
  if (form.waypoints) field(form, "#form-waypoints").append(form.waypoints.root);
  bindAircraftSearch(form, options);
  bindCallForm(form, options);
  if (form.report) {
    // Слова экипажа уже есть — типы предлагаются по ним сразу.
    runDefectSearch(form);
  }
  field(form, form.aircraftId ? "#form-defect-query" : "#form-aircraft").focus();
  return form;
}

function buildCallFormMarkup(options) {
  const backdrop = document.createElement("div");
  backdrop.className = "modal-backdrop";

  backdrop.innerHTML = `
    <div class="modal call-form">
      <h2>${options.report ? "Разбор сообщения экипажа" : "Регистрация вызова"}</h2>
      ${reportBanner(options.report)}

      <div class="modal-field">
        <label for="form-aircraft">Воздушное судно</label>
        <input id="form-aircraft" role="combobox" aria-autocomplete="list" aria-expanded="false"
               aria-controls="aircraft-results" autocomplete="off" placeholder="Бортовой номер">
        <div id="aircraft-results" class="aircraft-results" role="listbox" hidden></div>
      </div>

      ${options.report ? defectTypeField(options) + wordsField(options) : wordsField(options) + defectTypeField(options)}

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

      <label class="form-check">
        <input type="checkbox" id="form-urgent">
        <span>Срочный вызов <span class="dim">— встанет первым в списке</span></span>
      </label>
      <div class="form-waypoints" id="form-waypoints-box"${options.graph ? "" : " hidden"}>
        <div class="form-label">Промежуточные точки маршрута
          <span class="dim">— склад (инструмент 30 с), стоянка спецтехники (пересадка), любая точка</span></div>
        <div id="form-waypoints"></div>
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
 * Поле слов экипажа. При разборе сообщения слова уже есть, и поле
 * становится уточняющим поиском — поэтому стоит ниже предложенных типов.
 */
function wordsField(options) {
  return `
      <div class="modal-field">
        <label for="form-defect-query">${
          options.report
            ? "Тип не подошёл? Найдите по слову или главе ATA"
            : "Что сообщил экипаж"
        }</label>
        <textarea id="form-defect-query" rows="${options.report ? 1 : 2}" maxlength="500"
                  autocomplete="off" placeholder="${
                    options.report
                      ? "«шасси», «ATA 32»"
                      : "Например: «течь у левой стойки шасси». Инженер прочтёт это в вызове"
                  }"></textarea>
      </div>`;
}

function defectTypeField(options) {
  return `
      <div class="modal-field">
        <label>Тип неисправности</label>
        <div class="defect-picked" id="form-defect-picked" hidden></div>
        <div class="defect-results" id="form-defect-results" hidden></div>
        <div class="defect-hint" id="form-defect-hint">${
          options.report
            ? "Подбираем по словам экипажа…"
            : "Появится здесь по словам экипажа — останется выбрать"
        }</div>
      </div>`;
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

function closeAircraftResults(form) {
  field(form, "#aircraft-results").hidden = true;
  const input = field(form, "#form-aircraft");
  input.setAttribute("aria-expanded", "false");
  input.removeAttribute("aria-activedescendant");
}

function chooseAircraft(form, item) {
  field(form, "#form-aircraft").value = item.board_number;
  form.aircraftId = item.id;
  closeAircraftResults(form);
  updateSubmitState(form);
}

function searchAircraft(form, aircraft) {
  const input = field(form, "#form-aircraft");
  const list = field(form, "#aircraft-results");
  const query = input.value.trim().toUpperCase();
  form.aircraftMatches = query ? aircraft.filter((item) => item.board_number.toUpperCase().includes(query)) : [];
  form.aircraftIndex = -1;
  form.aircraftId = form.aircraftMatches.find((item) => item.board_number.toUpperCase() === query)?.id || null;
  updateSubmitState(form);
  list.replaceChildren();
  closeAircraftResults(form);
  if (!query) return;

  for (const item of form.aircraftMatches) {
    const option = document.createElement("div");
    option.id = `aircraft-option-${item.id}`;
    option.setAttribute("role", "option");
    option.textContent = `${item.board_number} · ${item.aircraft_type} · стоянка ${item.stand_ref || "—"}`;
    option.addEventListener("mousedown", (event) => event.preventDefault());
    option.addEventListener("click", () => chooseAircraft(form, item));
    list.append(option);
  }
  if (!form.aircraftMatches.length) list.textContent = "Борт не найден";
  list.hidden = false;
  input.setAttribute("aria-expanded", "true");
}

function onAircraftKey(form, event) {
  const list = field(form, "#aircraft-results");
  if (event.key === "Escape") closeAircraftResults(form);
  if (list.hidden || !form.aircraftMatches.length) return;
  if (event.key === "Enter") {
    event.preventDefault();
    chooseAircraft(form, form.aircraftMatches[Math.max(0, form.aircraftIndex)]);
    return;
  }
  if (event.key !== "ArrowDown" && event.key !== "ArrowUp") return;
  event.preventDefault();
  const count = form.aircraftMatches.length;
  const step = event.key === "ArrowDown" ? 1 : -1;
  const index = form.aircraftIndex < 0 && step < 0 ? count - 1 : (form.aircraftIndex + step + count) % count;
  form.aircraftIndex = index;
  const active = list.children[index];
  for (const option of list.children) option.setAttribute("aria-selected", String(option === active));
  event.target.setAttribute("aria-activedescendant", active.id);
  active.scrollIntoView({ block: "nearest" });
}

function bindAircraftSearch(form, options) {
  const input = field(form, "#form-aircraft");
  input.addEventListener("input", () => searchAircraft(form, options.aircraft));
  input.addEventListener("blur", () => closeAircraftResults(form));
  input.addEventListener("keydown", (event) => onAircraftKey(form, event));
  const initial = options.aircraft.find((item) => item.id === options.preselectedAircraftId);
  if (initial) chooseAircraft(form, initial);
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
  const text = searchText(form);
  if (!text) {
    form.results = [];
    form.active = -1;
    renderDefectResults(form, "");
    return;
  }

  try {
    const results = await findDefects(text);
    // Пока ждали ответ, диспетчер мог дописать запрос — старый ответ
    // не должен перерисовать подсказки под новый текст.
    if (searchText(form) !== text) {
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
 * По каким словам искать тип: по набранным, а пока поле пусто при разборе
 * сообщения — по словам самого экипажа.
 */
function searchText(form) {
  const typed = field(form, "#form-defect-query").value.trim();
  if (typed || !form.report) {
    return typed;
  }
  return (form.report.description || "").trim();
}

/**
 * Типы неисправностей под фразу экипажа.
 *
 * Сервер ищет типы, в которых есть все слова запроса: для «течь» или
 * «ATA 32» это точно, но целая фраза «течь у левой основной стойки» так
 * не найдёт ничего. Поэтому, если фраза целиком не подошла, она
 * разбирается по словам: каждое ищется на сервере отдельно, и выше
 * встаёт тип, под который подошло больше слов. Ранжирование внутри
 * каждого ответа остаётся серверным.
 */
async function findDefects(text) {
  const whole = await Api.searchDefects(text, CALL_FORM_RESULTS);
  const words = phraseWords(text);
  if (whole.length || words.length < 2) {
    return whole;
  }
  const lists = await Promise.all(words.map(searchByWord));
  return mergeByHits(lists).slice(0, CALL_FORM_RESULTS);
}

function searchByWord(word) {
  return Api.searchDefects(word, CALL_FORM_RESULTS);
}

/** Значимые слова фразы без повторов. */
function phraseWords(text) {
  const words = text.toLowerCase().match(/[a-zа-яё0-9]+/gi) || [];
  const long = words.filter((word) => word.length >= CALL_FORM_WORD_MIN);
  return [...new Set(long)].slice(0, CALL_FORM_WORDS_MAX);
}

/**
 * Сводит ответы по отдельным словам в один список.
 * Больше совпавших слов — выше; при равенстве — кто стоял выше у сервера.
 */
function mergeByHits(lists) {
  const found = new Map();
  for (const list of lists) {
    list.forEach((defect, place) => {
      const entry = found.get(defect.code) || { defect, hits: 0, place };
      entry.hits += 1;
      entry.place = Math.min(entry.place, place);
      found.set(defect.code, entry);
    });
  }
  return [...found.values()].sort(compareHits).map((entry) => entry.defect);
}

function compareHits(left, right) {
  return right.hits - left.hits || left.place - right.place;
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
  const hint = field(form, "#form-defect-hint");
  box.replaceChildren();

  if (!text) {
    box.hidden = true;
    hint.hidden = false;
    return;
  }
  hint.hidden = true;

  form.results.forEach((defect, index) => {
    box.append(buildDefectOption(form, defect, index));
  });

  const create = document.createElement("button");
  create.type = "button";
  create.className = "defect-option defect-option-new";
  create.textContent = "＋ Подходящего нет — завести новый тип";
  create.addEventListener("click", () => startNewDefect(form, text));
  box.append(create);

  if (!form.results.length) {
    const empty = document.createElement("div");
    empty.className = "defect-empty";
    empty.textContent = "По этим словам в справочнике ничего нет";
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
    renderDefectResults(form, searchText(form));
  } else if (event.key === "ArrowUp" && total) {
    event.preventDefault();
    form.active = (form.active - 1 + total) % total;
    renderDefectResults(form, searchText(form));
  } else if (event.key === "Enter") {
    event.preventDefault();
    if (form.active >= 0 && form.results[form.active]) {
      pickDefect(form, form.results[form.active]);
    }
  } else if (event.key === "Escape") {
    closeCallForm(form);
  }
}

/** Выбран существующий тип: подсказки сворачиваются в плашку с кнопкой «изменить». */
function pickDefect(form, defect) {
  form.selected = defect;
  form.mode = "search";
  field(form, "#form-defect-new").hidden = true;
  field(form, "#form-defect-results").hidden = true;
  field(form, "#form-defect-hint").hidden = true;

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
  // Тип выбран — следующим действием остаётся только отправка.
  field(form, "#form-submit").focus();
}

function resetDefect(form) {
  form.selected = null;
  form.mode = "search";
  field(form, "#form-defect-picked").hidden = true;
  field(form, "#form-defect-new").hidden = true;
  field(form, "#form-defect-query").focus();
  runDefectSearch(form);
  updateSubmitState(form);
}

/* --- Новый тип --- */

async function startNewDefect(form, text) {
  form.selected = null;
  form.mode = "new";
  field(form, "#form-defect-results").hidden = true;
  field(form, "#form-defect-new").hidden = false;
  // Короткий запрос («обледенение датчика») годится в название как есть,
  // а целая фраза экипажа — нет: название диспетчер сформулирует сам.
  field(form, "#form-new-name").value = text.length <= CALL_FORM_NAME_FROM_TEXT_MAX ? text : "";

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
  field(form, "#form-submit").disabled = !ready || !form.aircraftId || form.busy;
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
    const aircraftId = form.aircraftId;
    const urgent = field(form, "#form-urgent").checked;
    const waypoints = form.waypoints ? form.waypoints.value : [];
    const call = options.report
      ? await Api.acceptReport(options.report.id, code, aircraftId, urgent, waypoints)
      : await Api.createCall(
          aircraftId,
          code,
          // Слова экипажа из единственного поля — они же описание для инженера.
          field(form, "#form-defect-query").value.trim() || null,
          urgent,
          waypoints
        );
    closeCallForm(form);
    options.onRegistered(call);
  } catch (error) {
    if (error.status === 409 && form.mode === "new") {
      showFormError(form, `${error.detail}. Выберите его в списке.`);
      const name = field(form, "#form-new-name").value.trim();
      field(form, "#form-defect-new").hidden = true;
      form.mode = "search";
      // Слова экипажа в поле не трогаем: показываем найденный тип по названию.
      form.results = await Api.searchDefects(name, CALL_FORM_RESULTS);
      form.active = form.results.length ? 0 : -1;
      renderDefectResults(form, name);
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
