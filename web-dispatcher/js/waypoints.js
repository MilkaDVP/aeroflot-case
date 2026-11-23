/**
 * Выбор промежуточных точек маршрута — по порядку.
 *
 * Точку берут из списка мест аэропорта (склады, стоянка спецтехники,
 * ангар, стоянки ВС) или указывают на карте. Что делать в точке, решает
 * её вид: склад — получить инструмент (30 с), стоянка спецтехники —
 * пересадка на машину, остальное — маршрут проходит через точку.
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
   * @param {Function} [options.onPickStart] вызывается перед выбором на карте
   */
  constructor(options) {
    this.graph = options.graph;
    this.nodes = new Map((this.graph?.nodes || []).map((node) => [node.id, node]));
    this.value = [...(options.value || [])];
    this.onChange = options.onChange || (() => {});
    this.map = options.map || null;
    this.onPickStart = options.onPickStart || null;
    this.onPickEnd = options.onPickEnd || null;
    this.root = document.createElement("div");
    this.root.className = "waypoints";
    this.render();
  }

  /** Места аэропорта, которые имеет смысл выбирать из списка. */
  places() {
    const order = { warehouse: 0, vehicle_parking: 1, tech_center: 2, terminal: 3, stand: 4 };
    return [...this.nodes.values()]
      .filter((node) => node.type in order)
      .sort((a, b) => order[a.type] - order[b.type] ||
        String(a.ref).localeCompare(String(b.ref), "ru", { numeric: true }))
      .map((node) => ({ id: node.id, label: placeLabel(node) }));
  }

  set(nodeIds) {
    this.value = [...nodeIds];
    this.render();
  }

  change() {
    this.render();
    this.onChange([...this.value]);
  }

  render() {
    const list = this.value.map((id, index) => {
      const node = this.nodes.get(id);
      return `
        <li class="waypoint" data-index="${index}">
          <span class="waypoint-num">${index + 1}</span>
          <span class="waypoint-name">${escapeWaypoint(node ? placeLabel(node) : id)}</span>
          <button type="button" class="waypoint-btn" data-act="up" title="Раньше"${index === 0 ? " disabled" : ""}>↑</button>
          <button type="button" class="waypoint-btn" data-act="remove" title="Убрать">✕</button>
        </li>`;
    }).join("");
    const options = this.places()
      .map((place) => `<option value="${place.id}">${escapeWaypoint(place.label)}</option>`)
      .join("");
    this.root.innerHTML = `
      ${this.value.length ? `<ol class="waypoint-list">${list}</ol>`
        : '<div class="waypoint-empty dim">Маршрут прямо к борту</div>'}
      <div class="waypoint-add">
        <select class="waypoint-select" aria-label="Промежуточная точка">
          <option value="">Добавить точку…</option>${options}
        </select>
        ${this.map ? '<button type="button" class="waypoint-btn waypoint-pick" title="Указать точку на карте">⌖ на карте</button>' : ""}
      </div>`;

    this.root.querySelector(".waypoint-select").addEventListener("change", (event) => {
      if (!event.target.value) return;
      this.value.push(event.target.value);
      this.change();
    });
    for (const button of this.root.querySelectorAll(".waypoint-btn[data-act]")) {
      button.addEventListener("click", () => {
        const index = Number(button.closest(".waypoint").dataset.index);
        if (button.dataset.act === "remove") {
          this.value.splice(index, 1);
        } else if (index > 0) {
          [this.value[index - 1], this.value[index]] = [this.value[index], this.value[index - 1]];
        }
        this.change();
      });
    }
    const pick = this.root.querySelector(".waypoint-pick");
    if (pick) {
      pick.addEventListener("click", () => {
        if (this.onPickStart) this.onPickStart();
        pick.textContent = "щёлкните по карте…";
        pick.disabled = true;
        this.map.pickPoint((node) => {
          if (this.onPickEnd) this.onPickEnd();
          if (node) {
            this.value.push(node.id);
            this.change();
          } else {
            this.render();
          }
        });
      });
    }
  }
}

/** Подпись места: что это и что в нём делать. */
function placeLabel(node) {
  switch (node.type) {
    case "warehouse":
      return `${node.ref || "Склад"} · инструмент 30 с`;
    case "vehicle_parking":
      return `${node.ref || "Стоянка спецтехники"} · пересадка на машину`;
    case "tech_center":
      return node.ref || "Техцентр";
    case "terminal":
      return node.ref || "Терминал";
    case "stand":
      return `Стоянка ${node.ref}`;
    default:
      return node.ref || "Точка на карте";
  }
}

function escapeWaypoint(value) {
  return String(value ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" })[c]);
}
