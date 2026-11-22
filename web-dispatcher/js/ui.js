/*
 * Окна, всплывающие списки и уведомления — свои, а не браузерные.
 *
 * Раньше причина снятия, постановки в очередь и переключения спрашивалась
 * через window.prompt, а подтверждения — через window.confirm. Встроенные
 * браузеры (панель предпросмотра в редакторе, киоск на стенде) такие окна
 * молча блокируют: prompt сразу возвращает null, и кнопка «Снять
 * исполнителя» просто ничего не делала. Свои окна работают везде и
 * выглядят так же, как остальной экран.
 *
 * Одновременно открыто не больше одного окна и одного всплывающего
 * списка: их состояние хранится здесь, в одном месте.
 */

const dialogState = {
  root: null,
  resolve: null,
  required: false,
};

const popoverState = {
  root: null,
  anchor: null,
};

let toastTimer = null;

const TOAST_MS = 4000;
const POPOVER_GAP_PX = 8;

/* --- Окно с вопросом --- */

/**
 * Окно с текстом и, если нужно, полем для ответа.
 *
 * @param {Object} options
 * @param {string} options.title заголовок — сам вопрос
 * @param {string} [options.text] пояснение под заголовком
 * @param {string} [options.placeholder] есть поле ввода, если задано
 * @param {string} [options.confirm] надпись на кнопке подтверждения
 * @param {boolean} [options.danger] действие необратимое — красная кнопка
 * @returns {Promise<string|boolean|null>} введённый текст (или true без
 *   поля), null — если окно закрыли без подтверждения
 */
function askDialog(options) {
  closeDialog(null);

  const backdrop = document.createElement("div");
  backdrop.className = "dialog-backdrop";
  backdrop.innerHTML = `
    <form class="dialog" novalidate>
      <h3 class="dialog-title">${escapeHtml(options.title)}</h3>
      ${options.text ? `<p class="dialog-text">${escapeHtml(options.text)}</p>` : ""}
      ${
        options.placeholder
          ? `<textarea class="dialog-input" rows="2" maxlength="300"
               placeholder="${escapeHtml(options.placeholder)}"></textarea>`
          : ""
      }
      <div class="dialog-actions">
        <button type="button" class="dialog-cancel">Отмена</button>
        <button type="submit" class="${options.danger ? "danger-fill" : "primary"}">${escapeHtml(
          options.confirm || "Готово"
        )}</button>
      </div>
    </form>
  `;
  document.body.append(backdrop);

  dialogState.root = backdrop;
  dialogState.required = Boolean(options.placeholder);
  backdrop.querySelector("form").addEventListener("submit", submitDialog);
  backdrop.querySelector(".dialog-cancel").addEventListener("click", cancelDialog);
  backdrop.addEventListener("mousedown", onDialogBackdrop);
  document.addEventListener("keydown", onDialogKey);

  const input = backdrop.querySelector(".dialog-input");
  (input || backdrop.querySelector("[type=submit]")).focus();
  return new Promise(rememberResolve);
}

function rememberResolve(resolve) {
  dialogState.resolve = resolve;
}

function submitDialog(event) {
  event.preventDefault();
  const input = dialogState.root.querySelector(".dialog-input");
  if (!input) {
    closeDialog(true);
    return;
  }
  const text = input.value.trim();
  // Причина обязательна: пустой ответ не закрывает окно, а подсвечивает поле.
  if (dialogState.required && !text) {
    input.classList.add("is-invalid");
    input.focus();
    return;
  }
  closeDialog(text);
}

function cancelDialog() {
  closeDialog(null);
}

function onDialogBackdrop(event) {
  if (event.target === dialogState.root) {
    closeDialog(null);
  }
}

function onDialogKey(event) {
  if (event.key === "Escape") {
    closeDialog(null);
  } else if (event.key === "Enter" && !event.shiftKey && event.target.classList.contains("dialog-input")) {
    submitDialog(event);
  }
}

function closeDialog(value) {
  document.removeEventListener("keydown", onDialogKey);
  if (dialogState.root) {
    dialogState.root.remove();
    dialogState.root = null;
  }
  const resolve = dialogState.resolve;
  dialogState.resolve = null;
  if (resolve) {
    resolve(value);
  }
}

/* --- Всплывающий список --- */

/**
 * Небольшое окно рядом с кнопкой: длинный список (занятые, отсеянные)
 * не растягивает панель, а открывается по щелчку поверх края карты.
 *
 * @param {HTMLElement} anchor кнопка, рядом с которой открыть
 * @param {string} title заголовок
 * @param {HTMLElement[]} nodes содержимое
 */
function openPopover(anchor, title, nodes) {
  // Повторный щелчок по той же кнопке закрывает список.
  if (popoverState.anchor === anchor) {
    closePopover();
    return;
  }
  const placement = popoverState.root?.contains(anchor) ? popoverState.anchor : anchor;
  closePopover();

  const root = document.createElement("div");
  root.className = "popover";
  root.innerHTML = `
    <div class="popover-head">
      <span>${escapeHtml(title)}</span>
      <button type="button" class="popover-close" aria-label="Закрыть">×</button>
    </div>
    <div class="popover-body"></div>
  `;
  root.querySelector(".popover-body").append(...nodes);
  root.querySelector(".popover-close").addEventListener("click", closePopover);
  document.body.append(root);

  placePopover(root, placement);
  popoverState.root = root;
  popoverState.anchor = anchor;
  anchor.classList.add("is-open");
  document.addEventListener("mousedown", onOutsidePopover);
  document.addEventListener("keydown", onPopoverKey);
}

/**
 * Окно открывается слева от правой колонки, на уровне кнопки: так оно
 * не перекрывает саму колонку и не уходит за край экрана.
 */
function placePopover(root, anchor) {
  const rect = anchor.getBoundingClientRect();
  const column = anchor.closest(".panel") || anchor;
  const columnRect = column.getBoundingClientRect();
  const left = columnRect.left >= root.offsetWidth + POPOVER_GAP_PX
    ? columnRect.left - root.offsetWidth - POPOVER_GAP_PX
    : Math.min(columnRect.right + POPOVER_GAP_PX, window.innerWidth - root.offsetWidth);
  root.style.left = `${Math.max(0, left)}px`;
  const top = Math.min(rect.top, window.innerHeight - root.offsetHeight - POPOVER_GAP_PX);
  root.style.top = `${Math.max(POPOVER_GAP_PX, top)}px`;
}

function onOutsidePopover(event) {
  if (!popoverState.root) {
    return;
  }
  const inside = popoverState.root.contains(event.target);
  const onAnchor = popoverState.anchor && popoverState.anchor.contains(event.target);
  if (!inside && !onAnchor) {
    closePopover();
  }
}

function onPopoverKey(event) {
  if (event.key === "Escape") {
    closePopover();
  }
}

function closePopover() {
  document.removeEventListener("mousedown", onOutsidePopover);
  document.removeEventListener("keydown", onPopoverKey);
  if (popoverState.anchor) {
    popoverState.anchor.classList.remove("is-open");
  }
  if (popoverState.root) {
    popoverState.root.remove();
  }
  popoverState.root = null;
  popoverState.anchor = null;
}

/* --- Уведомление --- */

/** Короткое сообщение внизу экрана: ошибка сервера или итог действия. */
function showToast(message, isError) {
  let toast = document.getElementById("toast-bar");
  if (!toast) {
    toast = document.createElement("div");
    toast.id = "toast-bar";
    toast.className = "toast-bar";
    document.body.append(toast);
  }
  toast.textContent = message;
  toast.classList.toggle("is-error", Boolean(isError));
  toast.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(hideToast, TOAST_MS);
}

function hideToast() {
  const toast = document.getElementById("toast-bar");
  if (toast) {
    toast.hidden = true;
  }
}
