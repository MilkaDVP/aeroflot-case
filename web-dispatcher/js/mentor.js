/*
 * Наставник — лента замечаний и итог смены.
 *
 * Наставник — вымышленный опытный диспетчер: он смотрит за каждым решением
 * и говорит «хорошо», «не очень» или «плохо» — с причиной в цифрах.
 * Оценки выносит сервер по детерминированным правилам; здесь они только
 * показываются. Клиент не может ни смягчить оценку, ни придумать свою.
 */

const VERDICT_TITLES = {
  good: "хорошо",
  fair: "не очень",
  bad: "плохо",
};

const MENTOR_FEED_VISIBLE = 12;
const MENTOR_FULL_LIMIT = 200;

/**
 * Перерисовывает блок Наставника в правой панели.
 *
 * @param {Object} payload ответ /api/shift/feed: events и summary
 * @param {Function} onOpenCall открыть вызов, к которому относится замечание
 */
function renderMentor(payload, onOpenCall) {
  renderMentorScore(payload.summary);

  const list = document.getElementById("mentor-feed");
  list.replaceChildren();

  if (!payload.events.length) {
    list.innerHTML =
      '<div class="empty">Замечаний пока нет. Наставник оценит первое же решение.</div>';
    return;
  }

  for (const event of payload.events.slice(0, MENTOR_FEED_VISIBLE)) {
    list.append(buildMentorItem(event, onOpenCall));
  }
}

/** Счётчики оценок в заголовке блока: смена одним взглядом. */
function renderMentorScore(summary) {
  const score = document.getElementById("mentor-score");
  score.innerHTML = ["good", "fair", "bad"]
    .map(
      (verdict) =>
        `<span class="verdict-count is-${verdict}" title="${VERDICT_TITLES[verdict]}">` +
        `${summary.verdicts[verdict] || 0}</span>`
    )
    .join("");
}

function buildMentorItem(event, onOpenCall) {
  const item = document.createElement("div");
  item.className = `mentor-item is-${event.verdict}`;
  item.innerHTML = `
    <div class="mentor-item-top">
      <span class="verdict-badge is-${escapeHtml(event.verdict)}">${escapeHtml(
        VERDICT_TITLES[event.verdict] || event.verdict
      )}</span>
      <span class="call-board">${escapeHtml(event.board_number || "")}</span>
      <span class="mentor-time num">${formatClock(event.created_at)}</span>
    </div>
    <div class="mentor-text">${escapeHtml(event.text)}</div>
  `;
  if (onOpenCall && event.call_id) {
    item.classList.add("is-clickable");
    item.addEventListener("click", () => onOpenCall(event.call_id));
  }
  return item;
}

/* --- Итог смены --- */

/**
 * Окно разбора смены: общая оценка, цифры и полная лента.
 *
 * Открывается кнопкой, а не висит постоянно: во время работы диспетчеру
 * нужна карта, а разбор — это разговор после.
 */
async function openShiftSummary(onOpenCall) {
  let payload;
  try {
    payload = await Api.shiftFeed(MENTOR_FULL_LIMIT);
  } catch (error) {
    window.alert(error.detail);
    return;
  }

  const backdrop = document.createElement("div");
  backdrop.className = "modal-backdrop";
  backdrop.innerHTML = `
    <div class="modal shift-summary">
      <h2>Итог смены <span class="dim">— за последние ${payload.summary.window_hours} ч</span></h2>
      <div class="summary-grade ${gradeClass(payload.summary)}">${escapeHtml(
        payload.summary.grade
      )}</div>
      <div class="summary-tiles">${summaryTiles(payload.summary)}</div>
      <div class="section-title">Все замечания (${payload.events.length})</div>
      <div class="summary-feed" id="summary-feed"></div>
      <div class="modal-actions">
        <button id="summary-close">Закрыть</button>
      </div>
    </div>
  `;
  document.body.append(backdrop);

  const list = backdrop.querySelector("#summary-feed");
  if (!payload.events.length) {
    list.innerHTML = '<div class="empty">Замечаний нет</div>';
  }
  for (const event of payload.events) {
    list.append(buildMentorItem(event, null));
  }

  backdrop.querySelector("#summary-close").addEventListener("click", () => backdrop.remove());
  backdrop.addEventListener("click", (event) => {
    if (event.target === backdrop) {
      backdrop.remove();
    }
  });
}

function gradeClass(summary) {
  if (summary.verdicts.bad) {
    return "is-bad";
  }
  if (summary.verdicts.fair) {
    return "is-fair";
  }
  return summary.verdicts.good ? "is-good" : "";
}

/** Плитки с цифрами: каждая отвечает на один вопрос о смене. */
function summaryTiles(summary) {
  const inTime =
    summary.in_time_percent === null
      ? tile("—", "прибытий пока нет", "В регламенте")
      : tile(
          `${summary.in_time_percent} %`,
          `${summary.arrivals_in_time} из ${summary.arrivals} прибытий`,
          "В регламенте"
        );
  const toAssign =
    summary.avg_minutes_to_assign === null
      ? tile("—", "назначений пока нет", "До назначения")
      : tile(
          `${formatMinutes(summary.avg_minutes_to_assign)} мин`,
          `в среднем, назначений: ${summary.assignments}`,
          "До назначения"
        );
  const overrides = tile(
    String(summary.overrides),
    summary.overrides
      ? `стоили ${formatMinutes(summary.override_cost_min)} мин`
      : "решение системы не меняли",
    "Переопределений"
  );
  return inTime + toAssign + overrides + verdictTile(summary.verdicts);
}

function tile(value, note, title) {
  return `
    <div class="summary-tile">
      <div class="summary-tile-title">${escapeHtml(title)}</div>
      <div class="summary-tile-value num">${escapeHtml(value)}</div>
      <div class="summary-tile-note">${escapeHtml(note)}</div>
    </div>
  `;
}

/** Плитка оценок: три числа цветом оценки — читаются без подписи. */
function verdictTile(verdicts) {
  const numbers = ["good", "fair", "bad"]
    .map((verdict) => `<span class="verdict-badge is-${verdict}">${verdicts[verdict] || 0}</span>`)
    .join('<span class="dim"> · </span>');
  return `
    <div class="summary-tile">
      <div class="summary-tile-title">Оценки</div>
      <div class="summary-tile-value num">${numbers}</div>
      <div class="summary-tile-note">хорошо · не очень · плохо</div>
    </div>
  `;
}
