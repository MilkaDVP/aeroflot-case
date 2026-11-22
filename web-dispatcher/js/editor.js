/**
 * Редактор графа аэропорта: точки и связи поверх спутникового снимка.
 *
 * Зачем он нужен. Импорт по коду ИКАО даёт настоящий аэропорт на сотни
 * стоянок — по нему хорошо показывать работу системы, но плохо её
 * проверять: в сети из тысячи рёбер не видно, почему маршрут пошёл именно
 * так. Схема из десяти точек, нарисованная руками, позволяет глазами
 * проследить каждый маршрут и убедиться, что расчёт верен.
 *
 * Координаты храним географические: администратор ставит точку по снимку,
 * а широта и долгота не зависят от того, какой участок и в каком масштабе
 * был на экране. Длины рёбер считает сервер — по тем же формулам, что и
 * при выгрузке из OpenStreetMap.
 *
 * Отклик на действия. Редактор показывает, что произойдёт, до щелчка:
 * под курсором — полупрозрачная точка, которую поставите; в режиме
 * «Соединить» от выбранной точки к курсору тянется линия; в режиме
 * «Удаление» краснеет то, что удалится. Новое появляется, удалённое
 * исчезает с короткой анимацией — по ней видно, что действие сработало.
 */

const EDITOR_SVG_NS = "http://www.w3.org/2000/svg";

// Режимы работы. Точка ставится щелчком по карте, связь — щелчками
// по уже поставленным точкам, удаление — щелчком по точке или связи.
const MODE_STAND = "stand";
const MODE_TECH = "tech_center";
const MODE_JUNCTION = "junction";
const MODE_LINK = "link";
const MODE_ERASE = "erase";

const PLACING_MODES = [MODE_STAND, MODE_TECH, MODE_JUNCTION];

// Щелчок — это быстрое нажатие почти без движения. Зажали кнопку дольше
// или повели мышью — это работа с картой (сдвиг), а не установка точки.
const EDITOR_CLICK_SLOP_PX = 4;
const EDITOR_CLICK_MAX_MS = 300;

// Сколько длятся анимации появления и исчезновения — столько же, сколько
// в CSS; по истечении служебные элементы удаляются.
const EDITOR_FX_MS = 450;

const NODE_TITLES = {
  stand: "стоянка",
  tech_center: "техцентр",
  junction: "перекрёсток",
};

class GraphEditor {
  /**
   * @param {AirportMap} map карта, в чьём слое рисуется граф
   * @param {Function} onChange вызывается после любого изменения
   */
  constructor(map, onChange) {
    this.map = map;
    this.onChange = onChange || null;
    this.layer = map.layers.editor;

    this.nodes = [];
    this.edges = [];
    this.mode = MODE_STAND;
    // Первая точка начатой связи — ждём вторую.
    this.linkFrom = null;
    // Счётчик для идентификаторов: они должны быть уникальны в пределах
    // аэропорта и не зависеть от порядка удаления.
    this.counter = 1;

    // Что только что появилось — нарисуется с анимацией один раз.
    this.freshNodes = new Set();
    this.freshEdges = new Set();

    // Слой эффектов поверх графа: «призрак» под курсором, линия начатой
    // связи, волны и исчезающие элементы. Его не трогает перерисовка.
    this.fx = document.createElementNS(EDITOR_SVG_NS, "g");
    this.fx.setAttribute("class", "editor-fx");
    this.map.viewport.append(this.fx);
    this.ghost = null;
    this.rubber = null;
    this.cursor = null;

    // Где и когда нажали кнопку мыши: по этому отличаем щелчок от сдвига
    // карты или долгого нажатия.
    this.down = null;
    const svg = this.map.svg;
    svg.addEventListener(
      "mousedown",
      (event) => {
        this.down = { x: event.clientX, y: event.clientY, at: Date.now() };
        svg.classList.add("editor-pressing");
      },
      true
    );
    window.addEventListener("mouseup", () => svg.classList.remove("editor-pressing"));
    svg.addEventListener("click", (event) => this.onMapClick(event));
    svg.addEventListener("mousemove", (event) => this.onPointerMove(event));
    svg.addEventListener("mouseleave", () => this.hidePointerFx());
    // После зума колесом пересчитываем «призрак» под неподвижным курсором.
    svg.addEventListener("wheel", () => requestAnimationFrame(() => this.updatePointerFx()));
  }

  /**
   * Было ли это настоящим щелчком: быстрое нажатие почти без движения.
   * Сдвиг карты и долгое нажатие щелчком не считаются.
   */
  isClick(event) {
    if (!this.down) return true;
    const moved = Math.hypot(event.clientX - this.down.x, event.clientY - this.down.y);
    const held = Date.now() - this.down.at;
    return moved <= EDITOR_CLICK_SLOP_PX && held <= EDITOR_CLICK_MAX_MS;
  }

  /** Загружает уже существующий граф в редактор. */
  load(graph) {
    this.nodes = (graph.nodes || []).map((node) => ({
      id: node.id,
      type: node.type,
      ref: node.ref,
      lat: node.lat,
      lon: node.lon,
    }));
    this.edges = (graph.edges || []).map((edge) => ({
      from_id: edge.from_id,
      to_id: edge.to_id,
      vehicle_allowed: edge.vehicle_allowed !== false,
    }));
    this.counter = this.nodes.length + 1;
    this.linkFrom = null;
    this.render();
  }

  setMode(mode) {
    this.mode = mode;
    // Начатая связь при смене режима сбрасывается: иначе следующий щелчок
    // соединил бы точки, которые администратор соединять уже не собирался.
    this.linkFrom = null;
    this.render();
    this.updatePointerFx();
  }

  /** Данные для отправки на сервер. */
  payload() {
    return { nodes: this.nodes, edges: this.edges };
  }

  counts() {
    return {
      nodes: this.nodes.length,
      stands: this.nodes.filter((node) => node.type === MODE_STAND).length,
      tech: this.nodes.filter((node) => node.type === MODE_TECH).length,
      edges: this.edges.length,
    };
  }

  nodeById(id) {
    return this.nodes.find((node) => node.id === id) || null;
  }

  /* --- Действия --- */

  onMapClick(event) {
    // Щелчок по точке или связи обработал их собственный обработчик.
    if (event.target.closest(".editor-node, .editor-edge-group")) {
      return;
    }
    if (!this.isClick(event) || !PLACING_MODES.includes(this.mode)) {
      return;
    }

    // Координаты графа, а не viewBox: иначе на приближённой карте точка
    // встанет в стороне от места, куда щёлкнул администратор.
    const point = this.map.toGraphCoords(event.clientX, event.clientY);
    const position = this.map.unprojectXY(point.x, point.y);
    this.ripple(point, this.mode);
    this.addNode(this.mode, position.lat, position.lon);
  }

  addNode(type, lat, lon) {
    const node = {
      id: this.nextId(),
      type,
      ref: this.defaultRef(type),
      lat: Number(lat.toFixed(7)),
      lon: Number(lon.toFixed(7)),
    };
    this.nodes.push(node);
    this.freshNodes.add(node.id);
    this.changed();
    return node;
  }

  /**
   * Идентификатор новой точки.
   *
   * Уникальность проверяется по уже существующим: у загруженного графа
   * идентификаторы могут идти не подряд, и простой счётчик выдал бы
   * занятое имя — сервер отклонил бы граф как «точка описана дважды».
   */
  nextId() {
    let id = `p${this.counter}`;
    while (this.nodeById(id)) {
      this.counter += 1;
      id = `p${this.counter}`;
    }
    this.counter += 1;
    return id;
  }

  /**
   * Подпись новой точки.
   *
   * Стоянки нумеруются по порядку стоянок, а не всех точек: иначе после
   * техцентра и перекрёстка первая стоянка получала бы номер «3», и по
   * номерам на карте нельзя было бы понять, сколько стоянок разметили.
   * Техцентры подписываются так же — диспетчер отличает их по названию.
   */
  defaultRef(type) {
    if (type === MODE_STAND) {
      return this.nextFreeRef(MODE_STAND, "");
    }
    if (type === MODE_TECH) {
      return this.nextFreeRef(MODE_TECH, "ТЦ-");
    }
    return null;
  }

  /** Наименьший номер вида «префикс + число», ещё не занятый. */
  nextFreeRef(type, prefix) {
    const taken = new Set(
      this.nodes.filter((node) => node.type === type).map((node) => node.ref)
    );
    let number = 1;
    while (taken.has(`${prefix}${number}`)) {
      number += 1;
    }
    return `${prefix}${number}`;
  }

  onNodeClick(node) {
    if (this.mode === MODE_ERASE) {
      this.removeNode(node.id);
      return;
    }
    if (this.mode === MODE_STAND || this.mode === MODE_TECH) {
      this.retype(node, this.mode);
      return;
    }
    if (this.mode !== MODE_LINK) {
      return;
    }

    if (this.linkFrom === null) {
      this.linkFrom = node.id;
      this.render();
      this.updatePointerFx();
      return;
    }
    if (this.linkFrom === node.id) {
      this.linkFrom = null;
      this.render();
      this.updatePointerFx();
      return;
    }

    // Цепочку удобно тянуть дальше от только что соединённой точки:
    // рулёжки рисуются последовательностью, а не парами. Новая точка
    // начала ставится до добавления связи — тогда перерисовка одна,
    // и прорисовка новой связи не теряется.
    const from = this.linkFrom;
    this.linkFrom = node.id;
    const before = this.edges.length;
    this.addEdge(from, node.id);
    if (this.edges.length === before) {
      this.render();
    }
    this.updatePointerFx();
  }

  /**
   * Превращает существующий перекрёсток в стоянку или техцентр.
   *
   * В выгрузке из OSM стоянки без номера остаются узлами сети — это
   * плотные ряды точек у перронов. Щелчок по такой точке в режиме
   * «Стоянка» делает её стоянкой: она уже соединена с рулёжкой, и
   * дорисовывать ничего не нужно. Уже размеченную точку повторный щелчок
   * не трогает, чтобы случайно не перенумеровать стоянку.
   */
  retype(node, type) {
    if (node.type !== MODE_JUNCTION) {
      return;
    }
    node.type = type;
    node.ref = this.defaultRef(type);
    this.freshNodes.add(node.id);
    this.changed();
  }

  addEdge(fromId, toId) {
    const exists = this.edges.some(
      (edge) =>
        (edge.from_id === fromId && edge.to_id === toId) ||
        (edge.from_id === toId && edge.to_id === fromId)
    );
    if (exists) {
      return;
    }
    this.edges.push({ from_id: fromId, to_id: toId, vehicle_allowed: true });
    this.freshEdges.add(edgeKey(fromId, toId));
    this.changed();
  }

  removeNode(nodeId) {
    const node = this.nodeById(nodeId);
    const gone = this.edges.filter((edge) => edge.from_id === nodeId || edge.to_id === nodeId);
    // Исчезают и точка, и её связи — видно, что ушло вместе с ней.
    gone.forEach((edge) => this.fadeEdge(edge));
    if (node) this.fadeNode(node);

    this.nodes = this.nodes.filter((item) => item.id !== nodeId);
    // Связи удаляются вместе с точкой: ребро в пустоту сервер всё равно
    // не примет, а на карте оно выглядело бы как настоящая рулёжка.
    this.edges = this.edges.filter(
      (edge) => edge.from_id !== nodeId && edge.to_id !== nodeId
    );
    if (this.linkFrom === nodeId) {
      this.linkFrom = null;
    }
    this.changed();
  }

  /** Удаляет одну связь — в какую бы сторону она ни была записана. */
  removeEdge(fromId, toId) {
    const key = edgeKey(fromId, toId);
    this.edges
      .filter((edge) => edgeKey(edge.from_id, edge.to_id) === key)
      .forEach((edge) => this.fadeEdge(edge));
    this.edges = this.edges.filter((edge) => edgeKey(edge.from_id, edge.to_id) !== key);
    this.changed();
  }

  clear() {
    this.edges.forEach((edge) => this.fadeEdge(edge));
    this.nodes.forEach((node) => this.fadeNode(node));
    this.nodes = [];
    this.edges = [];
    this.linkFrom = null;
    this.changed();
  }

  changed() {
    this.render();
    if (this.onChange) {
      this.onChange(this.counts());
    }
  }

  /* --- Отрисовка --- */

  render() {
    this.layer.replaceChildren();
    this.layer.classList.toggle("is-erase", this.mode === MODE_ERASE);
    this.layer.classList.toggle("is-link", this.mode === MODE_LINK);
    this.layer.classList.toggle("is-placing", PLACING_MODES.includes(this.mode));
    this.drawEdges();
    this.drawNodes();
    this.freshNodes.clear();
    this.freshEdges.clear();
    this.map.updateMarkerScale();
  }

  drawEdges() {
    for (const edge of this.edges) {
      const from = this.nodeById(edge.from_id);
      const to = this.nodeById(edge.to_id);
      if (!from || !to) {
        continue;
      }

      const start = this.map.projectLatLon(from.lat, from.lon);
      const end = this.map.projectLatLon(to.lat, to.lon);
      const line = edgeLine(start, end, "editor-edge");
      if (this.freshEdges.has(edgeKey(edge.from_id, edge.to_id))) {
        // Длина «1» — чтобы анимация прорисовки не зависела от длины связи.
        line.setAttribute("pathLength", "1");
        line.classList.add("is-new");
      }
      if (this.mode !== MODE_ERASE) {
        this.layer.append(line);
        continue;
      }

      // Удаление: поверх линии — широкая невидимая полоса, по которой
      // легко попасть мышью. Под курсором линия краснеет: удалится
      // именно она.
      const hit = edgeLine(start, end, "editor-edge-hit");
      const group = document.createElementNS(EDITOR_SVG_NS, "g");
      group.setAttribute("class", "editor-edge-group");
      const label = document.createElementNS(EDITOR_SVG_NS, "title");
      label.textContent = "Удалить связь";
      group.append(line, hit, label);
      group.addEventListener("click", (event) => {
        event.stopPropagation();
        if (!this.isClick(event)) return;
        this.removeEdge(edge.from_id, edge.to_id);
      });
      this.layer.append(group);
    }
  }

  drawNodes() {
    for (const node of this.nodes) {
      const point = this.map.projectLatLon(node.lat, node.lon);
      const marker = this.map.createMarker(point.x, point.y);
      marker.classList.add("editor-node", `is-${node.type}`);
      if (node.id === this.linkFrom) {
        marker.classList.add("is-linking");
      }
      if (this.freshNodes.has(node.id)) {
        marker.classList.add("is-new");
      }

      // Внутренняя группа — чтобы анимации масштаба не спорили
      // с transform самой метки, который держит её экранный размер.
      const body = document.createElementNS(EDITOR_SVG_NS, "g");
      body.setAttribute("class", "editor-node-body");
      const shape = document.createElementNS(EDITOR_SVG_NS, "circle");
      shape.setAttribute("r", node.type === MODE_JUNCTION ? 5 : 7);
      body.append(shape);
      marker.append(body);

      if (node.ref) {
        const label = document.createElementNS(EDITOR_SVG_NS, "text");
        label.setAttribute("x", 10);
        label.setAttribute("y", 4);
        label.setAttribute("class", "editor-label");
        label.textContent = node.ref;
        marker.append(label);
      }

      const title = document.createElementNS(EDITOR_SVG_NS, "title");
      title.textContent = `${NODE_TITLES[node.type] || node.type}${
        node.ref ? ` ${node.ref}` : ""
      }${this.mode === MODE_ERASE ? " — удалить" : ""}`;
      marker.append(title);

      marker.addEventListener("click", (event) => {
        event.stopPropagation();
        if (!this.isClick(event)) return;
        this.onNodeClick(node);
      });
      this.layer.append(marker);
    }
  }

  /* --- Отклик под курсором --- */

  onPointerMove(event) {
    this.cursor = { x: event.clientX, y: event.clientY };
    this.updatePointerFx();
  }

  /**
   * «Призрак» точки под курсором в режимах установки и линия от выбранной
   * точки к курсору в режиме «Соединить». Пока кнопка зажата (сдвиг карты)
   * или курсор над существующей точкой — призрака нет.
   */
  updatePointerFx() {
    if (!this.cursor) return;
    const point = this.map.toGraphCoords(this.cursor.x, this.cursor.y);
    const overNode = document
      .elementFromPoint(this.cursor.x, this.cursor.y)
      ?.closest?.(".editor-node");

    if (PLACING_MODES.includes(this.mode) && !overNode) {
      if (!this.ghost || this.ghost.dataset.type !== this.mode) {
        this.ghost?.remove();
        this.ghost = this.fxMarker(point, `editor-ghost is-${this.mode}`, this.mode);
        this.ghost.dataset.type = this.mode;
      }
      this.map.placeMarker(this.ghost, point.x, point.y);
    } else {
      this.ghost?.remove();
      this.ghost = null;
    }

    const from = this.mode === MODE_LINK && this.linkFrom ? this.nodeById(this.linkFrom) : null;
    if (from) {
      const start = this.map.projectLatLon(from.lat, from.lon);
      if (!this.rubber) {
        this.rubber = edgeLine(start, point, "editor-rubber");
        this.fx.append(this.rubber);
      }
      this.rubber.setAttribute("x1", start.x);
      this.rubber.setAttribute("y1", start.y);
      this.rubber.setAttribute("x2", point.x);
      this.rubber.setAttribute("y2", point.y);
    } else {
      this.rubber?.remove();
      this.rubber = null;
    }
  }

  hidePointerFx() {
    this.cursor = null;
    this.ghost?.remove();
    this.ghost = null;
    this.rubber?.remove();
    this.rubber = null;
  }

  /* --- Эффекты --- */

  /** Метка постоянного экранного размера в слое эффектов. */
  fxMarker(point, className, type) {
    const marker = this.map.createMarker(point.x, point.y);
    marker.classList.add(...className.split(" "));
    const shape = document.createElementNS(EDITOR_SVG_NS, "circle");
    shape.setAttribute("r", type === MODE_JUNCTION ? 5 : 7);
    marker.append(shape);
    this.fx.append(marker);
    this.map.placeMarker(marker, point.x, point.y);
    return marker;
  }

  /** Волна от места щелчка — точка поставлена. */
  ripple(point, type) {
    const marker = this.fxMarker(point, `editor-ripple is-${type}`, type);
    setTimeout(() => marker.remove(), EDITOR_FX_MS + 150);
  }

  /** Исчезающая копия удалённой точки. */
  fadeNode(node) {
    const point = this.map.projectLatLon(node.lat, node.lon);
    const marker = this.fxMarker(point, `editor-fade-node is-${node.type}`, node.type);
    setTimeout(() => marker.remove(), EDITOR_FX_MS);
  }

  /** Исчезающая копия удалённой связи. */
  fadeEdge(edge) {
    const from = this.nodeById(edge.from_id);
    const to = this.nodeById(edge.to_id);
    if (!from || !to) return;
    const line = edgeLine(
      this.map.projectLatLon(from.lat, from.lon),
      this.map.projectLatLon(to.lat, to.lon),
      "editor-fade-edge"
    );
    this.fx.append(line);
    setTimeout(() => line.remove(), EDITOR_FX_MS);
  }
}

/** Ключ связи без направления: из А в Б и из Б в А — одна связь. */
function edgeKey(a, b) {
  return a < b ? `${a}|${b}` : `${b}|${a}`;
}

function edgeLine(start, end, className) {
  const line = document.createElementNS(EDITOR_SVG_NS, "line");
  line.setAttribute("x1", start.x);
  line.setAttribute("y1", start.y);
  line.setAttribute("x2", end.x);
  line.setAttribute("y2", end.y);
  line.setAttribute("class", className);
  return line;
}
