/*
 * SVG-карта аэропорта.
 *
 * Граф Шереметьево — около 1800 узлов и 2600 рёбер. Отрисовать их все
 * браузер способен без труда; проблема не в скорости, а в шуме. Поэтому
 * рулёжки уходят в фон одной тусклой линией, а смысл несут только
 * стоянки, воздушные суда, люди и маршрут.
 *
 * Координаты приходят с сервера уже в метрах (линейная проекция от
 * опорной точки аэропорта), ось Y направлена на юг — как в экранных
 * координатах. Поэтому viewBox задаётся прямо в метрах.
 *
 * Важная деталь. Всё содержимое лежит в масштабируемой группе, поэтому
 * значок стоянки, нарисованный «радиусом 9», — это 9 метров на местности.
 * На общем плане аэропорта шириной 7 километров он превратился бы
 * в точку в один пиксель, а при десятикратном приближении — в круг
 * во весь экран. Поэтому все точечные значки рисуются в пикселях
 * и получают обратный масштаб: их экранный размер не зависит от зума.
 */

const SVG_NS = "http://www.w3.org/2000/svg";

// Порог масштаба, за которым появляются номера стоянок. Показывать 179
// подписей на общем плане бессмысленно — они сольются в кашу.
const LABEL_VISIBLE_SCALE = 2.6;

// Запас вокруг данных, чтобы крайние стоянки не липли к краю экрана.
const VIEW_PADDING_M = 120;

const MIN_ZOOM = 1;
const MAX_ZOOM = 30;

class AirportMap {
  constructor(svgElement) {
    this.svg = svgElement;
    this.graph = null;
    this.nodesById = new Map();

    // Слои: порядок добавления определяет порядок отрисовки. Снимок
    // в самом низу, маршрут и люди — всегда поверх рулёжек.
    this.layers = {
      basemap: createGroup("layer-basemap"),
      scrim: createGroup("layer-scrim"),
      taxiways: createGroup("layer-taxiways"),
      stands: createGroup("layer-stands"),
      route: createGroup("layer-route"),
      aircraft: createGroup("layer-aircraft"),
      employees: createGroup("layer-employees"),
      labels: createGroup("layer-labels"),
    };

    this.viewport = createGroup("viewport");
    for (const layer of Object.values(this.layers)) {
      this.viewport.append(layer);
    }
    this.svg.append(this.viewport);

    this.transform = { x: 0, y: 0, scale: 1 };
    this.baseView = null;
    // Сколько экранных пикселей приходится на метр местности при зуме 1.
    this.basePxPerMetre = 1;
    this.onStandClick = null;

    this.tiles = null;
    // Догрузка тайлов откладывается до конца жеста: во время
    // перетаскивания пересчитывать их на каждое движение мыши незачем.
    this.tileRefreshTimer = null;

    this.setupPanZoom();
    window.addEventListener("resize", () => this.measureBaseScale());
  }

  /** Подключает растровую подложку. Без вызова карта остаётся схемой. */
  attachTiles(onStatus) {
    this.tiles = new TileLayer(this, this.layers.basemap, onStatus);
    return this.tiles;
  }

  /**
   * Переключение подложки.
   *
   * Затемнение поверх снимка обязательно: тонкие линии рулёжек и серые
   * значки, рассчитанные на тёмный фон, поверх светлого снимка теряются.
   */
  setBasemap(key) {
    if (!this.tiles) {
      return;
    }
    this.tiles.setBasemap(key);

    const withImagery = key !== "scheme";
    this.svg.classList.toggle("has-basemap", withImagery);
    this.drawScrim(withImagery);
  }

  /** Полупрозрачная подложка между снимком и графом. */
  drawScrim(visible) {
    this.layers.scrim.replaceChildren();
    if (!visible || !this.baseView) {
      return;
    }

    // Прямоугольник заведомо шире данных: карту можно отвести далеко
    // в сторону, и затемнение не должно обрываться.
    const margin = Math.max(this.baseView.width, this.baseView.height) * 2;
    const rect = document.createElementNS(SVG_NS, "rect");
    rect.setAttribute("x", this.baseView.minX - margin);
    rect.setAttribute("y", this.baseView.minY - margin);
    rect.setAttribute("width", this.baseView.width + margin * 2);
    rect.setAttribute("height", this.baseView.height + margin * 2);
    rect.setAttribute("class", "basemap-scrim");
    this.layers.scrim.append(rect);
  }

  /** Откладывает догрузку тайлов до паузы в жесте. */
  scheduleTileRefresh() {
    if (!this.tiles) {
      return;
    }
    clearTimeout(this.tileRefreshTimer);
    this.tileRefreshTimer = setTimeout(() => this.tiles.refresh(), 120);
  }

  /** Загружает граф и рисует неизменяемую основу: рулёжки и стоянки. */
  render(graph) {
    this.graph = graph;
    this.nodesById = new Map(graph.nodes.map((node) => [node.id, node]));

    this.fitView(graph.nodes);
    this.measureBaseScale();
    this.drawTaxiways(graph);
    this.drawStands(graph);
  }

  /**
   * Подбирает viewBox под рабочую зону аэропорта.
   *
   * Считаем охват по стоянкам и техцентрам, а не по всем узлам графа.
   * В Шереметьево рулёжка к третьей полосе уходит на километры западнее
   * перронов: если вписывать её, рабочая зона сожмётся в угол экрана
   * и половина карты будет пустой. Сами рулёжки при этом никуда
   * не деваются — до них доезжают панорамированием.
   */
  fitView(allNodes) {
    const operational = allNodes.filter((node) => node.type !== "junction");
    const nodes = operational.length >= 2 ? operational : allNodes;

    const xs = nodes.map((node) => node.x);
    const ys = nodes.map((node) => node.y);

    const minX = Math.min(...xs) - VIEW_PADDING_M;
    const minY = Math.min(...ys) - VIEW_PADDING_M;
    const width = Math.max(...xs) - minX + VIEW_PADDING_M;
    const height = Math.max(...ys) - minY + VIEW_PADDING_M;

    this.baseView = { minX, minY, width, height };
    this.svg.setAttribute("viewBox", `${minX} ${minY} ${width} ${height}`);
  }

  /**
   * Измеряет, сколько пикселей приходится на метр при зуме 1.
   *
   * Нужно для значков постоянного экранного размера. preserveAspectRatio
   * вписывает viewBox целиком, поэтому масштаб определяется меньшим
   * из двух отношений.
   */
  measureBaseScale() {
    if (!this.baseView) {
      return;
    }
    const box = this.svg.getBoundingClientRect();
    if (!box.width || !box.height) {
      return;
    }
    this.basePxPerMetre = Math.min(
      box.width / this.baseView.width,
      box.height / this.baseView.height
    );
    this.updateMarkerScale();
  }

  /** Коэффициент, переводящий пиксели значка в метры карты. */
  get markerScale() {
    return 1 / (this.basePxPerMetre * this.transform.scale);
  }

  /**
   * Рулёжная сеть.
   *
   * Каждое ребро рисуется ломаной по промежуточным точкам, а не прямой
   * между вершинами: без этого изогнутые рулёжки превратились бы в хорды,
   * и карта перестала бы походить на настоящий аэродром.
   */
  drawTaxiways(graph) {
    const layer = this.layers.taxiways;
    layer.replaceChildren();

    for (const edge of graph.edges) {
      const from = this.nodesById.get(edge.from_id);
      const to = this.nodesById.get(edge.to_id);
      if (!from || !to) {
        continue;
      }

      const path = document.createElementNS(SVG_NS, "path");
      path.setAttribute("d", edgePath(from, to, edge.points));
      path.setAttribute("class", edge.vehicle_allowed ? "taxiway" : "taxiway walk-only");
      layer.append(path);
    }
  }

  /** Стоянки и техцентры. Подписи в отдельном слое: их показ зависит от зума. */
  drawStands(graph) {
    const stands = this.layers.stands;
    const labels = this.layers.labels;
    stands.replaceChildren();
    labels.replaceChildren();

    for (const node of graph.nodes) {
      if (node.type === "junction") {
        continue;
      }

      const isTech = node.type === "tech_center";
      const marker = this.createMarker(node.x, node.y);

      if (isTech) {
        const shape = document.createElementNS(SVG_NS, "rect");
        shape.setAttribute("x", -7);
        shape.setAttribute("y", -7);
        shape.setAttribute("width", 14);
        shape.setAttribute("height", 14);
        shape.setAttribute("class", "tech-center");
        marker.append(shape, title(`Техцентр ${node.ref || ""}`));
      } else {
        const shape = document.createElementNS(SVG_NS, "circle");
        shape.setAttribute("r", 3.5);
        shape.setAttribute("class", "stand");
        marker.append(shape, title(`Стоянка ${node.ref}`));
        marker.addEventListener("click", () => {
          if (this.onStandClick) {
            this.onStandClick(node);
          }
        });
      }

      stands.append(marker);

      const label = this.createMarker(node.x, node.y);
      const text = document.createElementNS(SVG_NS, "text");
      text.setAttribute("y", isTech ? -12 : -8);
      text.setAttribute("class", isTech ? "label label-tech" : "label label-stand");
      text.textContent = node.ref || "";
      label.append(text);
      // Техцентры подписаны всегда: их три, и это опорные точки карты.
      label.dataset.always = isTech ? "1" : "0";
      labels.append(label);
    }

    this.updateMarkerScale();
  }

  /** Метки воздушных судов на стоянках. */
  drawAircraft(aircraftList) {
    const layer = this.layers.aircraft;
    layer.replaceChildren();

    for (const aircraft of aircraftList) {
      const node = this.nodesById.get(aircraft.stand_node_id);
      if (!node) {
        continue;
      }

      const marker = this.createMarker(node.x, node.y);
      const shape = document.createElementNS(SVG_NS, "path");
      // Простой силуэт носом вверх: на любом масштабе читается лучше,
      // чем иконка с мелкими деталями.
      shape.setAttribute("d", "M 0 -7 L 6 5 L -6 5 Z");
      shape.setAttribute("class", "aircraft");
      marker.append(
        shape,
        title(
          `${aircraft.board_number} · ${aircraft.aircraft_type} · стоянка ${aircraft.stand_ref}`
        )
      );
      layer.append(marker);
    }

    this.updateMarkerScale();
  }

  /**
   * Сотрудники.
   *
   * Цвет метки — это ответ на вопрос «кого можно послать прямо сейчас».
   * Класс приходит снаружи: карта не знает про допуски, она их рисует.
   */
  drawEmployees(employees, classifier) {
    const layer = this.layers.employees;
    layer.replaceChildren();

    const placed = employees.filter(
      (employee) => employee.lat !== null && employee.lon !== null
    );
    const offsets = spreadOverlapping(placed);

    for (const employee of placed) {
      const position = this.projectLatLon(employee.lat, employee.lon);
      const marker = this.createMarker(position.x, position.y);
      const offset = offsets.get(employee.id);

      // Спецтранспорт отмечается кольцом: наличие машины меняет время
      // в пути вчетверо, это должно считываться без наведения мыши.
      if (employee.has_vehicle) {
        const ring = document.createElementNS(SVG_NS, "circle");
        ring.setAttribute("cx", offset.dx);
        ring.setAttribute("cy", offset.dy);
        ring.setAttribute("r", 9);
        ring.setAttribute("class", "employee-vehicle");
        marker.append(ring);
      }

      const shape = document.createElementNS(SVG_NS, "circle");
      shape.setAttribute("cx", offset.dx);
      shape.setAttribute("cy", offset.dy);
      shape.setAttribute("r", 5);
      shape.setAttribute("class", `employee ${classifier(employee)}`);
      marker.append(shape, title(employeeTooltip(employee)));

      layer.append(marker);
    }

    this.updateMarkerScale();
  }

  /** Маршрут предложенного кандидата. */
  drawRoute(nodeIds) {
    const layer = this.layers.route;
    layer.replaceChildren();

    if (!nodeIds || nodeIds.length < 2) {
      return;
    }

    const points = nodeIds
      .map((id) => this.nodesById.get(id))
      .filter(Boolean)
      .map((node) => `${node.x},${node.y}`);

    if (points.length < 2) {
      return;
    }

    const line = document.createElementNS(SVG_NS, "polyline");
    line.setAttribute("points", points.join(" "));
    line.setAttribute("class", "route");
    // Нормируем длину: анимация прорисовки одинаково работает
    // и на коротком маршруте, и на трёхкилометровом.
    line.setAttribute("pathLength", "1");
    layer.append(line);
  }

  clearRoute() {
    this.layers.route.replaceChildren();
  }

  /**
   * Проекция координат сотрудника в систему карты.
   *
   * Повторяет ту же линейную проекцию, что и офлайн-скрипт выгрузки:
   * координаты приходят от телефона в градусах, а карта работает
   * в метрах от опорной точки аэропорта.
   */
  projectLatLon(lat, lon) {
    const EARTH_RADIUS_M = 6371008.8;
    const reference = this.graph.ref_point;
    const toRad = Math.PI / 180;

    return {
      x: (lon - reference.lon) * toRad * EARTH_RADIUS_M * Math.cos(reference.lat * toRad),
      y: (reference.lat - lat) * toRad * EARTH_RADIUS_M,
    };
  }

  /* --- Значки постоянного экранного размера --- */

  /** Группа значка: содержимое рисуется в пикселях вокруг начала координат. */
  createMarker(x, y) {
    const group = document.createElementNS(SVG_NS, "g");
    group.setAttribute("class", "marker");
    group.dataset.x = x;
    group.dataset.y = y;
    return group;
  }

  /** Пересчитывает обратный масштаб всех значков после изменения зума. */
  updateMarkerScale() {
    const scale = this.markerScale;
    for (const marker of this.viewport.querySelectorAll("g.marker")) {
      marker.setAttribute(
        "transform",
        `translate(${marker.dataset.x} ${marker.dataset.y}) scale(${scale})`
      );
    }
    this.updateLabelVisibility();
  }

  /* --- Панорамирование и масштаб --- */

  /**
   * Видимая область в координатах viewBox, то есть в метрах.
   *
   * preserveAspectRatio вписывает viewBox целиком и центрует его,
   * поэтому по «лишней» стороне видно больше, чем задано в baseView.
   */
  visibleRect() {
    const box = this.svg.getBoundingClientRect();
    const width = box.width / this.basePxPerMetre;
    const height = box.height / this.basePxPerMetre;
    return {
      box,
      width,
      height,
      minX: this.baseView.minX - (width - this.baseView.width) / 2,
      minY: this.baseView.minY - (height - this.baseView.height) / 2,
    };
  }

  /**
   * Экранная точка в координатах карты.
   *
   * Ключевой момент всей навигации по карте: transform применяется внутри
   * SVG с viewBox, поэтому его аргументы — метры, а не пиксели. Смещение
   * мыши приходит в пикселях, и без деления на масштаб карта уезжала бы
   * в разы дальше, чем двигалась рука.
   */
  toMapCoords(clientX, clientY) {
    const rect = this.visibleRect();
    return {
      x: rect.minX + (clientX - rect.box.left) / this.basePxPerMetre,
      y: rect.minY + (clientY - rect.box.top) / this.basePxPerMetre,
    };
  }

  /** Центр экрана в координатах карты. При вписанном viewBox он постоянен. */
  screenCentre() {
    return {
      x: this.baseView.minX + this.baseView.width / 2,
      y: this.baseView.minY + this.baseView.height / 2,
    };
  }

  setupPanZoom() {
    let dragging = false;
    let origin = null;

    this.svg.addEventListener("mousedown", (event) => {
      dragging = true;
      origin = {
        clientX: event.clientX,
        clientY: event.clientY,
        x: this.transform.x,
        y: this.transform.y,
      };
      this.svg.classList.add("dragging");
    });

    window.addEventListener("mouseup", () => {
      dragging = false;
      this.svg.classList.remove("dragging");
    });

    window.addEventListener("mousemove", (event) => {
      if (!dragging || !origin) {
        return;
      }
      this.transform.x =
        origin.x + (event.clientX - origin.clientX) / this.basePxPerMetre;
      this.transform.y =
        origin.y + (event.clientY - origin.clientY) / this.basePxPerMetre;
      this.applyTransform();
    });

    this.svg.addEventListener(
      "wheel",
      (event) => {
        event.preventDefault();
        const factor = event.deltaY < 0 ? 1.18 : 1 / 1.18;
        const point = this.toMapCoords(event.clientX, event.clientY);
        this.zoomAt(point.x, point.y, factor);
      },
      { passive: false }
    );
  }

  /**
   * Масштабирование с сохранением неподвижной точки.
   *
   * Точка задаётся в координатах карты. При колесе мыши это точка под
   * курсором — так карта ведёт себя предсказуемо, а не прыгает к центру.
   */
  zoomAt(mapX, mapY, factor) {
    const next = clamp(this.transform.scale * factor, MIN_ZOOM, MAX_ZOOM);
    const applied = next / this.transform.scale;

    this.transform.x = mapX - (mapX - this.transform.x) * applied;
    this.transform.y = mapY - (mapY - this.transform.y) * applied;
    this.transform.scale = next;

    this.applyTransform();
  }

  /** Масштабирование от центра экрана — для кнопок «+» и «−». */
  zoomFromCentre(factor) {
    const centre = this.screenCentre();
    this.zoomAt(centre.x, centre.y, factor);
  }

  applyTransform() {
    const { x, y, scale } = this.transform;
    this.viewport.setAttribute("transform", `translate(${x} ${y}) scale(${scale})`);
    this.updateMarkerScale();
    this.scheduleTileRefresh();
  }

  updateLabelVisibility() {
    const visible = this.transform.scale >= LABEL_VISIBLE_SCALE;
    for (const label of this.layers.labels.children) {
      label.style.display = visible || label.dataset.always === "1" ? "" : "none";
    }
  }

  resetView() {
    this.transform = { x: 0, y: 0, scale: 1 };
    this.applyTransform();
  }

  /**
   * Приближает карту к указанному узлу — используется при выборе вызова.
   *
   * Узел должен оказаться в центре экрана: transform переводит координаты
   * содержимого в координаты viewBox как t + s·c, отсюда t = центр − s·c.
   */
  focusOn(nodeId, scale = 5) {
    const node = this.nodesById.get(nodeId);
    if (!node || !this.baseView) {
      return;
    }

    const centre = this.screenCentre();
    this.transform.scale = clamp(scale, MIN_ZOOM, MAX_ZOOM);
    this.transform.x = centre.x - this.transform.scale * node.x;
    this.transform.y = centre.y - this.transform.scale * node.y;
    this.applyTransform();
  }
}

/* --- Вспомогательные функции --- */

function createGroup(className) {
  const group = document.createElementNS(SVG_NS, "g");
  group.setAttribute("class", className);
  return group;
}

function title(text) {
  const element = document.createElementNS(SVG_NS, "title");
  element.textContent = text;
  return element;
}

function edgePath(from, to, points) {
  const parts = [`M ${from.x} ${from.y}`];
  for (const point of points || []) {
    parts.push(`L ${point[0]} ${point[1]}`);
  }
  parts.push(`L ${to.x} ${to.y}`);
  return parts.join(" ");
}

/**
 * Разводит метки сотрудников, стоящих в одной точке.
 *
 * В техцентре смена собирается вся сразу, и без разведения три инженера
 * дают на карте один кружок — диспетчер видит одного человека вместо
 * троих и делает неверный вывод о наличии людей. Смещение задаётся
 * в экранных пикселях по кругу и не зависит от масштаба.
 */
function spreadOverlapping(employees) {
  const RING_RADIUS_PX = 11;
  const groups = new Map();

  for (const employee of employees) {
    // Пять знаков после запятой — примерно метр: ближе этого метки
    // всё равно неразличимы.
    const key = `${employee.lat.toFixed(5)},${employee.lon.toFixed(5)}`;
    if (!groups.has(key)) {
      groups.set(key, []);
    }
    groups.get(key).push(employee);
  }

  const offsets = new Map();
  for (const group of groups.values()) {
    if (group.length === 1) {
      offsets.set(group[0].id, { dx: 0, dy: 0 });
      continue;
    }
    group.forEach((employee, index) => {
      const angle = (2 * Math.PI * index) / group.length;
      offsets.set(employee.id, {
        dx: Math.round(RING_RADIUS_PX * Math.cos(angle) * 10) / 10,
        dy: Math.round(RING_RADIUS_PX * Math.sin(angle) * 10) / 10,
      });
    });
  }

  return offsets;
}

function employeeTooltip(employee) {
  const marks = employee.qualifications
    .map((item) => `${item.category} / ${item.aircraft_types.join(", ")}`)
    .join("\n");
  return `${employee.full_name}\n${marks}\nстатус: ${employee.status}`;
}

function clamp(value, min, max) {
  return Math.min(max, Math.max(min, value));
}
