/* Ручная онлайн-синхронизация бортов из бесплатного ADSB.lol и FR24. */

const LIVE_AIRCRAFT_SOURCES = [
  {
    name: "ADSB.lol",
    formId: "adsblol-sync-form",
    buttonId: "adsblol-sync-submit",
    noteId: "adsblol-note",
    resultsId: "adsblol-results",
    endpoint: "/api/aircraft/adsblol/sync",
    buttonLabel: "Загрузить из ADSB.lol",
  },
  {
    name: "Flightradar24",
    formId: "fr24-sync-form",
    buttonId: "fr24-sync-submit",
    noteId: "fr24-note",
    resultsId: "fr24-results",
    endpoint: "/api/aircraft/flightradar/sync",
    buttonLabel: "Загрузить из Flightradar24",
  },
];

for (const source of LIVE_AIRCRAFT_SOURCES) {
  el(source.formId).addEventListener("submit", (event) =>
    onLiveAircraftSubmit(event, source)
  );
}

/** Разовый запрос: фонового опроса и расхода кредитов без команды нет. */
async function onLiveAircraftSubmit(event, source) {
  event.preventDefault();

  if (!state.user || !state.user.airport_icao) {
    setNote(source.noteId, "Сначала выберите аэропорт", "is-error");
    return;
  }

  const button = el(source.buttonId);
  const results = el(source.resultsId);
  button.disabled = true;
  button.textContent = "Загрузка…";
  results.replaceChildren();
  results.hidden = true;
  setNote(
    source.noteId,
    `Запрашиваем текущие позиции ${source.name}. Это может занять несколько секунд.`,
    "is-pending"
  );

  try {
    const result = await request("POST", source.endpoint);
    setNote(
      source.noteId,
      `${result.airport_icao} · ${source.name}: получено ${result.received}; ` +
        `добавлено ${result.created}; уже есть ${result.existing}; ` +
        `пропущено ${result.skipped}.`,
      "is-ok"
    );
    renderLiveAircraftResults(source.resultsId, result.items);
    await loadAircraft();
  } catch (error) {
    setNote(source.noteId, error.detail, "is-error");
  } finally {
    button.disabled = false;
    button.textContent = source.buttonLabel;
  }
}

function renderLiveAircraftResults(containerId, items) {
  const container = el(containerId);
  container.replaceChildren();
  container.hidden = !items.length;

  const statuses = {
    created: ["загружен", "is-ok"],
    existing: ["уже есть", "is-busy"],
    skipped: ["пропущен", "is-error"],
  };

  for (const item of items) {
    const [title, className] = statuses[item.status] || [item.status, ""];
    const type = item.aircraft_type || item.source_type || "тип неизвестен";
    const row = document.createElement("div");
    row.className = "admin-row-item";
    row.innerHTML = `
      <div class="item-main">
        <span class="item-title">${escapeHtml(item.board_number)}</span>
        <span class="item-sub">${escapeHtml(type)} · ${escapeHtml(item.detail)}</span>
      </div>
      <div class="item-meta">
        <span class="badge ${className}">${escapeHtml(title)}</span>
      </div>
    `;
    container.append(row);
  }
}
