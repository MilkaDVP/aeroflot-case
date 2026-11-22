/**
 * Поиск объектов на карте по названию.
 *
 * Диспетчеру звонят и говорят «борт RA-89001», «Волков», «ТМ-04»,
 * «стоянка 35» — и ему нужно за секунду увидеть это место на карте,
 * а не водить по перрону глазами. Поиск идёт по тому, что уже есть
 * на рабочем месте: граф аэропорта (стоянки и техцентры), сотрудники
 * смены, машины парка и борта. Отдельного номера рейса в системе нет —
 * воздушное судно ищется по бортовому номеру и типу.
 *
 * Строка «ra89001» находит «RA-89001»: дефисы, пробелы и регистр при
 * сравнении не учитываются, «ё» равна «е».
 */

const SEARCH_LIMIT = 12;

const SEARCH_KINDS = {
  aircraft: { label: "Борт", icon: "✈" },
  stand: { label: "Стоянка", icon: "◯" },
  tech_center: { label: "Техцентр", icon: "▢" },
  employee: { label: "Сотрудник", icon: "●" },
  vehicle: { label: "Машина", icon: "■" },
};

// При равной точности совпадения сначала то, что диспетчер ищет чаще.
const KIND_ORDER = ["aircraft", "employee", "vehicle", "stand", "tech_center"];

function normalizeSearch(text) {
  return String(text || "")
    .toLowerCase()
    .replace(/ё/g, "е")
    .replace(/[\s\-–—_.·,]/g, "");
}

class MapSearch {
  /**
   * @param {HTMLElement} root контейнер, куда встраивается поле поиска
   * @param {AirportMap} map карта, на которой показываются находки
   * @param {Function} getData возвращает { graph, aircraft, employees, vehicles }
   */
  constructor(root, map, getData) {
    this.map = map;
    this.getData = getData;
    this.results = [];
    this.active = -1;

    this.box = document.createElement("div");
    this.box.className = "map-search";
    this.box.innerHTML = `
      <input type="search" class="map-search-input" placeholder="Поиск: борт, стоянка, сотрудник, машина"
             autocomplete="off" spellcheck="false" aria-label="Поиск на карте"
             role="combobox" aria-expanded="false" aria-controls="map-search-list">
      <ul class="map-search-list" id="map-search-list" role="listbox" hidden></ul>`;
    root.prepend(this.box);

    this.input = this.box.querySelector("input");
    this.list = this.box.querySelector("ul");

    this.input.addEventListener("input", () => this.update());
    this.input.addEventListener("focus", () => this.update());
    this.input.addEventListener("keydown", (event) => this.onKey(event));
    // Клик по карте не должен уходить в карту из поля и списка.
    this.box.addEventListener("mousedown", (event) => event.stopPropagation());
    this.box.addEventListener("click", (event) => event.stopPropagation());
    document.addEventListener("mousedown", (event) => {
      if (!this.box.contains(event.target)) this.close();
    });
    // «/» — быстрый переход к поиску, как в большинстве веб-приложений.
    document.addEventListener("keydown", (event) => {
      const typing = event.target.closest("input, textarea, select, [contenteditable]");
      if (event.key === "/" && !typing) {
        event.preventDefault();
        this.input.focus();
        this.input.select();
      }
    });
  }

  /** Все объекты, по которым можно искать, с точкой на карте. */
  collect() {
    const { graph, aircraft, employees, vehicles } = this.getData();
    const items = [];
    if (!graph) {
      return items;
    }
    const nodes = new Map(graph.nodes.map((node) => [node.id, node]));
    const planeAt = new Map(
      (aircraft || [])
        .filter((plane) => plane.on_stand !== false)
        .map((plane) => [plane.stand_node_id, plane])
    );

    for (const node of graph.nodes) {
      if (!node.ref || (node.type !== "stand" && node.type !== "tech_center")) continue;
      const isStand = node.type === "stand";
      items.push({
        kind: node.type,
        title: isStand ? `Стоянка ${node.ref}` : node.ref,
        sub: isStand && planeAt.has(node.id) ? `борт ${planeAt.get(node.id).board_number}` : "",
        keys: [node.ref, isStand ? `стоянка ${node.ref}` : node.ref],
        x: node.x,
        y: node.y,
      });
    }

    for (const plane of aircraft || []) {
      const offStand = plane.on_stand === false && plane.lat != null;
      const node = offStand ? this.map.projectLatLon(plane.lat, plane.lon) : nodes.get(plane.stand_node_id);
      if (!node) continue;
      items.push({
        kind: "aircraft",
        title: plane.board_number,
        sub: `${plane.aircraft_type} · ${offStand ? "вне стоянки" : `стоянка ${plane.stand_ref}`}`,
        keys: [plane.board_number, plane.aircraft_type],
        // По номеру стоянки борт тоже находится, но ниже самой стоянки.
        weakKeys: [plane.stand_ref],
        x: node.x,
        y: node.y,
      });
    }

    for (const person of employees || []) {
      const placed = person.lat != null && person.lon != null;
      const point = placed ? this.map.projectLatLon(person.lat, person.lon) : null;
      items.push({
        kind: "employee",
        title: person.full_name,
        sub: placed ? employeeStatusText(person) : "нет координат",
        keys: [person.full_name, ...(person.full_name || "").split(/\s+/)],
        x: point && point.x,
        y: point && point.y,
        disabled: !placed,
      });
    }

    for (const vehicle of vehicles || []) {
      const placed = vehicle.lat != null && vehicle.lon != null;
      const point = placed ? this.map.projectLatLon(vehicle.lat, vehicle.lon) : null;
      items.push({
        kind: "vehicle",
        title: vehicle.call_sign,
        sub: [vehicle.kind, vehicle.status === "free" ? "свободна" : vehicle.employee_name || "занята"]
          .filter(Boolean)
          .join(" · "),
        keys: [vehicle.call_sign, vehicle.kind],
        x: point && point.x,
        y: point && point.y,
        disabled: !placed,
      });
    }
    return items;
  }

  /**
   * Подбор совпадений. Точное совпадение выше начала слова, начало
   * выше вхождения в середину: на «35» первой идёт стоянка 35, а не 135.
   */
  search(query) {
    const needle = normalizeSearch(query);
    if (!needle) return [];

    const scored = [];
    for (const item of this.collect()) {
      let best = Infinity;
      for (const key of item.keys) {
        const hay = normalizeSearch(key);
        if (!hay) continue;
        if (hay === needle) best = Math.min(best, 0);
        else if (hay.startsWith(needle)) best = Math.min(best, 1);
        else if (hay.includes(needle)) best = Math.min(best, 2);
      }
      for (const key of item.weakKeys || []) {
        const hay = normalizeSearch(key);
        if (hay && hay.startsWith(needle)) best = Math.min(best, hay === needle ? 1 : 2);
      }
      if (best !== Infinity) {
        scored.push({ item, score: best });
      }
    }
    scored.sort(
      (a, b) =>
        a.score - b.score ||
        KIND_ORDER.indexOf(a.item.kind) - KIND_ORDER.indexOf(b.item.kind) ||
        a.item.title.localeCompare(b.item.title, "ru", { numeric: true })
    );
    return scored.slice(0, SEARCH_LIMIT).map((entry) => entry.item);
  }

  update() {
    const query = this.input.value.trim();
    this.results = this.search(query);
    this.active = this.results.findIndex((item) => !item.disabled);
    this.render(query);
  }

  render(query) {
    this.list.replaceChildren();
    if (!query) {
      this.close();
      return;
    }
    if (this.results.length === 0) {
      const empty = document.createElement("li");
      empty.className = "map-search-empty";
      empty.textContent = "Ничего не найдено";
      this.list.append(empty);
    }
    this.results.forEach((item, index) => {
      const row = document.createElement("li");
      row.className = "map-search-item";
      row.setAttribute("role", "option");
      row.classList.toggle("is-active", index === this.active);
      row.classList.toggle("is-disabled", Boolean(item.disabled));
      row.setAttribute("aria-selected", String(index === this.active));

      const kind = SEARCH_KINDS[item.kind];
      const icon = document.createElement("span");
      icon.className = `map-search-icon is-${item.kind}`;
      icon.textContent = kind.icon;

      const text = document.createElement("span");
      text.className = "map-search-text";
      const title = document.createElement("b");
      title.textContent = item.title;
      const sub = document.createElement("small");
      sub.textContent = [kind.label, item.sub].filter(Boolean).join(" · ");
      text.append(title, sub);

      row.append(icon, text);
      row.addEventListener("mousemove", () => this.setActive(index));
      row.addEventListener("click", () => this.choose(item));
      this.list.append(row);
    });
    this.list.hidden = false;
    this.input.setAttribute("aria-expanded", "true");
  }

  setActive(index) {
    if (index === this.active || !this.results[index] || this.results[index].disabled) return;
    this.active = index;
    [...this.list.children].forEach((row, i) => {
      row.classList.toggle("is-active", i === index);
      row.setAttribute("aria-selected", String(i === index));
    });
  }

  onKey(event) {
    if (event.key === "ArrowDown" || event.key === "ArrowUp") {
      event.preventDefault();
      const step = event.key === "ArrowDown" ? 1 : -1;
      const count = this.results.length;
      for (let i = 1; i <= count; i += 1) {
        const index = (this.active + step * i + count) % count;
        if (!this.results[index].disabled) {
          this.setActive(index);
          this.list.children[index].scrollIntoView({ block: "nearest" });
          break;
        }
      }
    } else if (event.key === "Enter") {
      event.preventDefault();
      const item = this.results[this.active];
      if (item) this.choose(item);
    } else if (event.key === "Escape") {
      if (this.input.value) {
        this.input.value = "";
        this.close();
      } else {
        this.input.blur();
      }
    }
  }

  choose(item) {
    if (item.disabled) return;
    this.map.focusOnPoint(item.x, item.y, 5);
    this.map.flashAt(item.x, item.y, item.title);
    this.close();
    this.input.blur();
  }

  close() {
    this.list.hidden = true;
    this.input.setAttribute("aria-expanded", "false");
  }
}

function employeeStatusText(person) {
  const statuses = {
    free: "свободен",
    assigned: "назначен на вызов",
    en_route: "в пути",
    busy: "на работах",
    offline: "не на связи",
  };
  return statuses[person.status] || person.status || "";
}
