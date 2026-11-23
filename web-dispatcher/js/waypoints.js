/**
 * Выбор промежуточных точек маршрута — по порядку.
 *
 * Выбранное показано цепочкой «Инженер → 1 Склад №1 · 30 с → … → Борт».
 * Добавить точку — быстрыми кнопками основных мест (склады, стоянка
 * спецтехники, ангар), списком стоянок ВС или щелчком по карте.
 * Что делать в точке, решает её вид: склад — получить инструмент (30 с),
 * стоянка спецтехники — пересадка на машину, остальное — проезд.
 *
 * Используется в форме регистрации вызова и в карточке выбранного вызова.
 */
class WaypointPicker {
  /**
   * @param {object} options
   * @param {object} options.graph граф аэропорта (узлы и рёбра)
   * @param {Array<string>} [options.value] выбранные узлы
   * @param {Function} [options.onChange] (nodeIds) => void
   * @param {object} [options.map] карта — для выбора точки щелчком
   * @param {Function} [options.onPickStart] перед выбором на карте
   * @param {Function} [options.onPickEnd] после выбора на карте
   */
  constructor(options) {
    this.graph = options.graph;
    this.nodes = new Map((this.graph?.nodes || []).map((node) => [node.id, node]));
    this.value = [...(options.value || [])];
    this.onChange = options.onChange || (() => {});
    this.map = options.map || null;
    this.onPickStart = options.onPickStart || null;
    this.onPickEnd = options.onPickEnd || null;
    this.picking = false;
    this.root = document.createElement("div");
    this.root.className = "wp";
    this.render();
  }

  /** Основные места — быстрыми кнопками. */
  quickPlaces() {
    const order = { warehouse: 0, vehicle_parking: 1, tech_center: 2 };
    return [...this.nodes.values()]
      .filter((node) => node.type in order)
      .sort((a, b) => order[a.type] - order[b.type] ||
        String(a.ref).localeCompare(String(b.ref), "ru", { numeric: true }));
  }

  /** Стоянки ВС — списком: их много. */
  stands() {
    return [...this.nodes.values()]
      .filter((node) => node.type === "stand")
      .sort((a, b) => String(a.ref).localeCompare(String(b.ref), "ru", { numeric: true }));
  }

  set(nodeIds) {
    this.value = [...nodeIds];
    this.render();
  }

  change() {
    this.render();
    this.onChange([...this.value]);
  }

  add(nodeId) {
    if (!nodeId) return;
    this.value.push(nodeId);
    this.change();
  }

  render() {
    const stops = this.value.map((id, index) => {
      const node = this.nodes.get(id);
      const kind = placeKind(node);
      return `
        <span class="wp-arrow">→</span>
        <span class="wp-stop wp-${kind}" data-index="${index}">
          <span class="wp-num">${index + 1}</span>
          <span class="wp-name">${escapeWaypoint(placeShort(node, id))}</span>
          ${placeAction(node) ? `<span class="wp-act">${placeAction(node)}</span>` : ""}
          ${index > 0 ? '<button type="button" class="wp-x" data-act="up" title="Раньше">‹</button>' : ""}
          <button type="button" class="wp-x" data-act="remove" title="Убрать">✕</button>
        </span>`;
    }).join("");

    const quick = this.quickPlaces().map((node) => `
      <button type="button" class="wp-add wp-${placeKind(node)}" data-node="${node.id}"
              title="${escapeWaypoint(placeHint(node))}">
        <span class="wp-dot"></span>${escapeWaypoint(placeShort(node, node.id))}
      </button>`).join("");
    const stands = this.stands();

    this.root.innerHTML = `
      <div class="wp-chain${this.value.length ? "" : " is-empty"}">
        <span class="wp-end">Инженер</span>
        ${stops}
        <span class="wp-arrow">→</span>
        <span class="wp-end">Борт</span>
      </div>
      <div class="wp-tools">
        ${quick}
        ${stands.length ? `<select class="wp-stand" aria-label="Стоянка ВС">
          <option value="">+ Стоянка…</option>
          ${stands.map((node) => `<option value="${node.id}">Стоянка ${escapeWaypoint(node.ref)}</option>`).join("")}
        </select>` : ""}
        ${this.map ? `<button type="button" class="wp-add wp-map${this.picking ? " is-on" : ""}">
          ⌖ ${this.picking ? "Щёлкните по карте…" : "На карте"}</button>` : ""}
      </div>`;

    for (const button of this.root.querySelectorAll(".wp-add[data-node]")) {
      button.addEventListener("click", () => this.add(button.dataset.node));
    }
    this.root.querySelector(".wp-stand")?.addEventListener("change", (event) => this.add(event.target.value));
    for (const button of this.root.querySelectorAll(".wp-x")) {
      button.addEventListener("click", () => {
        const index = Number(button.closest(".wp-stop").dataset.index);
        if (button.dataset.act === "remove") {
          this.value.splice(index, 1);
        } else if (index > 0) {
          [this.value[index - 1], this.value[index]] = [this.value[index], this.value[index - 1]];
        }
        this.change();
      });
    }
    const pick = this.root.querySelector(".wp-map");
    if (pick) {
      pick.addEventListener("click", () => {
        if (this.picking) return;
        this.picking = true;
        if (this.onPickStart) this.onPickStart();
        this.render();
        this.map.pickPoint((node) => {
          this.picking = false;
          if (this.onPickEnd) this.onPickEnd();
          if (node) this.add(node.id);
          else this.render();
        });
      });
    }
  }
}

function placeKind(node) {
  if (!node) return "point";
  return { warehouse: "warehouse", vehicle_parking: "parking", tech_center: "base", stand: "stand" }[node.type]
    || "point";
}

/** Короткое имя точки для цепочки и кнопок. */
function placeShort(node, id) {
  if (!node) return id;
  switch (node.type) {
    case "warehouse":
      return node.ref || "Склад";
    case "vehicle_parking":
      return node.ref || "Стоянка спецтехники";
    case "stand":
      return `Стоянка ${node.ref}`;
    case "tech_center":
    case "terminal":
      return node.ref || "Объект";
    default:
      return "Точка на карте";
  }
}

/** Что произойдёт в точке — мелкой подписью. */
function placeAction(node) {
  if (node?.type === "warehouse") return "инструмент 30 с";
  if (node?.type === "vehicle_parking") return "пересадка";
  return "";
}

function placeHint(node) {
  if (node.type === "warehouse") return "Получить инструмент — 30 с";
  if (node.type === "vehicle_parking") return "Пересесть на машину — посадка 10 с";
  return "Маршрут пройдёт через эту точку";
}

/** Подпись места в полном виде — для списков вне цепочки. */
function placeLabel(node) {
  const action = placeAction(node);
  return action ? `${placeShort(node, node.id)} · ${action}` : placeShort(node, node.id);
}

function escapeWaypoint(value) {
  return String(value ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" })[c]);
}
