/* Ручная онлайн-синхронизация бортов из Flightradar24. */

el("fr24-sync-form").addEventListener("submit", onFlightradarSubmit);

/** Разовый запрос: фонового опроса и расхода кредитов без команды нет. */
async function onFlightradarSubmit(event) {
  event.preventDefault();

  if (!state.user.airport_icao) {
    setNote("fr24-note", "Сначала выберите аэропорт", "is-error");
    return;
  }

  const button = el("fr24-sync-submit");
  const results = el("fr24-results");
  button.disabled = true;
  button.textContent = "Загрузка…";
  results.replaceChildren();
  results.hidden = true;
  setNote(
    "fr24-note",
    "Запрашиваем текущие позиции Flightradar24. Это может занять несколько секунд.",
    "is-pending"
  );

  try {
    const result = await request("POST", "/api/aircraft/flightradar/sync");
    setNote(
      "fr24-note",
      `${result.airport_icao}: получено ${result.received}; добавлено ${result.created}; ` +
        `уже есть ${result.existing}; пропущено ${result.skipped}.`,
      "is-ok"
    );
    renderFlightradarResults(result.items);
    await loadAircraft();
  } catch (error) {
    setNote("fr24-note", error.detail, "is-error");
  } finally {
    button.disabled = false;
    button.textContent = "Загрузить текущие борта";
  }
}

function renderFlightradarResults(items) {
  const container = el("fr24-results");
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
