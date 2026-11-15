/*
 * Ящик входящих сообщений о неисправностях.
 *
 * Сюда попадает то, что сообщил экипаж или передала система авиакомпании.
 * Это ещё не вызов: диспетчер разбирает сообщение — определяет борт и тип
 * неисправности — и только тогда оно становится вызовом. Срок при этом
 * уже идёт: пятнадцать минут отсчитываются от сообщения экипажа, поэтому
 * у каждого сообщения показано, сколько осталось.
 */

const SOURCE_TITLES = {
  "crew-tablet": "планшет экипажа",
  "airline-system": "система авиакомпании",
};

function renderInbox(reports, onProcess, onReject) {
  const box = document.getElementById("inbox");
  box.replaceChildren();
  box.hidden = reports.length === 0;
  if (!reports.length) {
    return;
  }

  const head = document.createElement("div");
  head.className = "inbox-head";
  head.innerHTML = `<span>Входящие сообщения</span><span class="num">${reports.length}</span>`;
  box.append(head);

  for (const report of reports) {
    box.append(buildInboxItem(report, onProcess, onReject));
  }
}

function buildInboxItem(report, onProcess, onReject) {
  const leftMin = (new Date(report.deadline_at).getTime() - Date.now()) / 60000;
  const item = document.createElement("div");
  item.className = "inbox-item";
  item.innerHTML = `
    <div class="inbox-item-top">
      <span class="call-board">${escapeHtml(report.board_number)}</span>
      <span class="call-stand">${escapeHtml(report.aircraft_type || "борт не найден")}</span>
      <span class="inbox-left num ${inboxLeftClass(leftMin)}">${inboxLeftText(leftMin)}</span>
    </div>
    <div class="inbox-text">«${escapeHtml(report.description)}»</div>
    <div class="inbox-meta">${escapeHtml(report.reporter || "")}${
      report.reporter ? " · " : ""
    }${escapeHtml(SOURCE_TITLES[report.source] || report.source)} ·
      <span class="num">${formatClock(report.reported_at)}</span></div>
    <div class="inbox-actions">
      <button type="button" class="primary inbox-process">Разобрать</button>
      <button type="button" class="inbox-reject">Отклонить</button>
    </div>
  `;
  item.querySelector(".inbox-process").addEventListener("click", () => onProcess(report));
  item.querySelector(".inbox-reject").addEventListener("click", () => onReject(report));
  return item;
}

/** Остаток до срока: время разбора отнимается у инженера, и это видно. */
function inboxLeftText(leftMin) {
  if (leftMin < 0) {
    return `просрочен ${Math.ceil(-leftMin)} мин`;
  }
  return `ост. ${Math.floor(leftMin)} мин`;
}

function inboxLeftClass(leftMin) {
  if (leftMin < 5) {
    return "is-danger";
  }
  return leftMin < 10 ? "is-warn" : "";
}
