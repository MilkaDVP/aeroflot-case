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
 */

const EDITOR_SVG_NS = "http://www.w3.org/2000/svg";

// Режимы работы. Точка ставится кликом по карте, связь — двумя кликами
// по уже поставленным точкам.
const MODE_STAND = "stand";
const MODE_TECH = "tech_center";
const MODE_JUNCTION = "junction";
const MODE_LINK = "link";
const MODE_ERASE = "erase";

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
    // Присоединять ли новую стоянку и техцентр к ближайшей точке сети.
    this.autolink = true;
    // Первая точка начатой связи — ждём вторую.
    this.linkFrom = null;
    // Счётчик для идентификаторов: они должны быть уникальны в пределах
    // аэропорта и не зависеть от порядка удаления.
    this.counter = 1;

    this.map.svg.addEventListener("click", (event) => this.onMapClick(event));
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
    // Начатая связь при смене режима сбрасывается: иначе следующий клик
    // соединил бы точки, которые администратор соединять уже не собирался.
    this.linkFrom = null;
    this.render();
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
    // Клик мог прийтись по точке — тогда его обработал обработчик точки.
    if (event.target.closest(".editor-node")) {
      return;
    }
    if (this.mode === MODE_LINK || this.mode === MODE_ERASE) {
      return;
    }

    // Координаты графа, а не viewBox: иначе на приближённой карте точка
    // встанет в стороне от места, куда кликнул администратор.
    const point = this.map.toGraphCoords(event.clientX, event.clientY);
    const position = this.map.unprojectXY(point.x, point.y);
    const nearest = this.autolink ? this.nearestNode(point) : null;
    const node = this.addNode(this.mode, position.lat, position.lon);

    // Стоянку и техцентр сразу присоединяем к ближайшей точке сети.
    // В выгруженном из OSM аэропорту сотни точек, и искать вручную,
    // к какой рулёжке прицепить каждую новую стоянку, было бы мучением.
    // Перекрёсток не привязывается: его ставят, чтобы потом соединить
    // самому.
    if (nearest !== null && node.type !== MODE_JUNCTION) {
      this.addEdge(node.id, nearest.id);
    }
  }

  /** Ближайшая к точке карты вершина графа или null, если вершин нет. */
  nearestNode(point) {
    let best = null;
    let bestDistance = Infinity;
    for (const node of this.nodes) {
      const position = this.map.projectLatLon(node.lat, node.lon);
      const distance = Math.hypot(position.x - point.x, position.y - point.y);
      if (distance < bestDistance) {
        best = node;
        bestDistance = distance;
      }
    }
    return best;
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
      return;
    }
    if (this.linkFrom === node.id) {
      this.linkFrom = null;
      this.render();
      return;
    }

    this.addEdge(this.linkFrom, node.id);
    // Цепочку удобно тянуть дальше от только что соединённой точки:
    // рулёжки рисуются последовательностью, а не парами.
    this.linkFrom = node.id;
  }

  /**
   * Превращает существующий перекрёсток в стоянку или техцентр.
   *
   * В выгрузке из OSM стоянки без номера остаются узлами сети — это
   * плотные ряды точек у перронов. Клик по такой точке в режиме «Стоянка»
   * делает её стоянкой: она уже соединена с рулёжкой, и дорисовывать
   * ничего не нужно. Уже размеченную точку повторный клик не трогает,
   * чтобы случайно не перенумеровать стоянку.
   */
  retype(node, type) {
    if (node.type !== MODE_JUNCTION) {
      return;
    }
    node.type = type;
    node.ref = this.defaultRef(type);
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
    this.changed();
  }

  removeNode(nodeId) {
    this.nodes = this.nodes.filter((node) => node.id !== nodeId);
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

  removeEdge(fromId, toId) {
    this.edges = this.edges.filter(
      (edge) => !(edge.from_id === fromId && edge.to_id === toId)
    );
    this.changed();
  }

  clear() {
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
    this.drawEdges();
    this.drawNodes();
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
      const line = document.createElementNS(EDITOR_SVG_NS, "line");
      line.setAttribute("x1", start.x);
      line.setAttribute("y1", start.y);
      line.setAttribute("x2", end.x);
      line.setAttribute("y2", end.y);
      line.setAttribute("class", "editor-edge");
      if (this.mode === MODE_ERASE) {
        line.classList.add("is-erasable");
        line.addEventListener("click", () => this.removeEdge(edge.from_id, edge.to_id));
      }
      this.layer.append(line);
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

      const shape = document.createElementNS(EDITOR_SVG_NS, "circle");
      shape.setAttribute("r", node.type === MODE_JUNCTION ? 5 : 7);
      marker.append(shape);

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
      }`;
      marker.append(title);

      marker.addEventListener("click", (event) => {
        event.stopPropagation();
        this.onNodeClick(node);
      });
      this.layer.append(marker);
    }
  }
}
