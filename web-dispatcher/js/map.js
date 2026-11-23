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

// Половина стороны участка, который показывается для аэропорта без точек:
// полтора километра в каждую сторону от опорной точки — обычный размер
// перрона, с него удобно начинать разметку.
const EMPTY_VIEW_HALF_M = 1500;

const MIN_ZOOM = 1;
const MAX_ZOOM = 128;

// След хранит точки за это время и не короче этого шага между точками.
const TRAIL_WINDOW_MS = 3 * 60 * 1000;
const TRAIL_MIN_STEP_M = 3;

/** Точка следа ещё свежая; момент «сейчас» приходит через this. */
function isRecentTrailPoint(point) {
  return this - point.at <= TRAIL_WINDOW_MS;
}

function trailPoint(point) {
  return `${point.x},${point.y}`;
}

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
      trails: createGroup("layer-trails"),
      aircraft: createGroup("layer-aircraft"),
      vehicles: createGroup("layer-vehicles"),
      employees: createGroup("layer-employees"),
      // Отметки начала и конца маршрута — поверх людей и бортов:
      // иначе буква «Б» пряталась под силуэтом самолёта на стоянке.
      routeEnds: createGroup("layer-route-ends"),
      // Слой редактора: точки и связи, которые администратор наносит
      // на карту. На рабочем экране диспетчера остаётся пустым.
      editor: createGroup("layer-editor"),
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
    this.onAircraftClick = null;

    // Перенос подвижных объектов (машин, людей, бортов) мышью.
    // Колбэк получает вид объекта, сам объект и новое место в градусах.
    this.onObjectDrop = null;
    this.onObjectRefused = null;
    this.drag = null;
    this.swallowClick = false;
    // Последние и отложенные на время переноса данные каждого слоя.
    this.lastDraw = {};
    this.pendingDraw = {};
    // Перенесённые, но ещё не подтверждённые сервером объекты: до ответа
    // они рисуются на новом месте, а не прыгают назад по старым данным.
    this.overrides = new Map();
    this.cancelPan = null;
    this.setupObjectDrag();

    this.tiles = null;
    // Следы перемещения: идентификатор сотрудника -> последние точки.
    this.trails = new Map();
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
   * Только система координат и вид, без рисования графа.
   *
   * Нужна редактору: граф там рисует сам редактор, из своей рабочей копии.
   * Если бы карта рисовала ещё и сохранённую версию, удалённая точка
   * оставалась бы на экране до сохранения, и было бы непонятно,
   * что уже убрано, а что нет.
   */
  frame(graph) {
    this.graph = graph;
    this.nodesById = new Map(graph.nodes.map((node) => [node.id, node]));
    this.layers.taxiways.replaceChildren();
    this.layers.stands.replaceChildren();
    this.layers.labels.replaceChildren();

    this.fitView(graph.nodes);
    this.measureBaseScale();
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
    // Пустой граф — это новый аэропорт, который ещё только размечают.
    // Охват считать не по чему, поэтому показываем участок вокруг опорной
    // точки: без этого viewBox получил бы Infinity, и клик по карте давал
    // бы нечисловые координаты.
    if (!allNodes.length) {
      this.baseView = {
        minX: -EMPTY_VIEW_HALF_M,
        minY: -EMPTY_VIEW_HALF_M,
        width: EMPTY_VIEW_HALF_M * 2,
        height: EMPTY_VIEW_HALF_M * 2,
      };
      this.svg.setAttribute(
        "viewBox",
        `${-EMPTY_VIEW_HALF_M} ${-EMPTY_VIEW_HALF_M} ` +
          `${EMPTY_VIEW_HALF_M * 2} ${EMPTY_VIEW_HALF_M * 2}`
      );
      return;
    }

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
    if (this.deferWhileDragging("aircraft", [aircraftList])) return;
    const layer = this.layers.aircraft;
    layer.replaceChildren();

    for (const aircraft of aircraftList) {
      const place = this.aircraftPlace(aircraft);
      if (!place) {
        continue;
      }

      const marker = this.createMarker(place.x, place.y);
      const shape = document.createElementNS(SVG_NS, "path");
      // Простой силуэт носом вверх: на любом масштабе читается лучше,
      // чем иконка с мелкими деталями.
      shape.setAttribute("d", "M 0 -7 L 6 5 L -6 5 Z");
      shape.setAttribute("class", "aircraft");
      marker.append(
        selectRing(0, 0, 12),
        shape,
        hitArea(0, 0),
        title(
          `${aircraft.board_number} · ${aircraft.aircraft_type} · стоянка ${aircraft.stand_ref}`
        )
      );
      marker.dataset.aircraftId = aircraft.id;
      // Щелчок по борту — карточка с кнопкой «Зарегистрировать вызов»,
      // а не сразу форма: иначе, пытаясь перетащить борт, диспетчер
      // каждый раз открывал бы окно поверх карты.
      this.makeDraggable(marker, "aircraft", aircraft, () => {
        if (this.onAircraftClick) this.onAircraftClick(aircraft, marker);
      });
      layer.append(marker);
    }

    this.updateMarkerScale();
  }

  /**
   * Машины парка.
   *
   * Рисуются свободные и ждущие инженера. Машина, которая едет
   * с инженером, отдельной меткой не рисуется: её место — место
   * инженера, и она уже видна пунктирным кольцом вокруг его метки.
   * Две метки в одной точке только мешали бы.
   */
  drawVehicles(vehicles) {
    if (this.deferWhileDragging("vehicle", [vehicles])) return;
    const layer = this.layers.vehicles;
    layer.replaceChildren();

    for (const vehicle of vehicles) {
      if (vehicle.status === "in_use") {
        continue;
      }
      const override = this.overrides.get(`vehicle:${vehicle.id}`) || vehicle;
      const position = this.projectLatLon(override.lat, override.lon);
      const marker = this.createMarker(position.x, position.y);

      const shape = document.createElementNS(SVG_NS, "rect");
      shape.setAttribute("x", -5);
      shape.setAttribute("y", -5);
      shape.setAttribute("width", 10);
      shape.setAttribute("height", 10);
      shape.setAttribute("rx", 2);
      shape.setAttribute(
        "class",
        vehicle.status === "reserved" ? "vehicle is-reserved" : "vehicle"
      );

      // Позывной подписан всегда: машин мало, и диспетчеру нужно
      // видеть, какую из них система предлагает забрать.
      const label = document.createElementNS(SVG_NS, "text");
      label.setAttribute("y", -9);
      label.setAttribute("class", "label label-vehicle");
      label.textContent = vehicle.call_sign;

      marker.dataset.vehicleId = vehicle.id;
      marker.classList.add("vehicle-marker");
      marker.append(selectRing(0, 0, 12), shape, hitArea(0, 0), label, title(vehicleTooltip(vehicle)));
      this.makeDraggable(marker, "vehicle", vehicle);
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
    if (this.deferWhileDragging("employee", [employees, classifier])) return;
    const layer = this.layers.employees;
    layer.replaceChildren();

    const placed = employees
      .filter((employee) => employee.lat !== null && employee.lon !== null)
      .map((employee) => {
        const override = this.overrides.get(`employee:${employee.id}`);
        return override ? { ...employee, lat: override.lat, lon: override.lon } : employee;
      });
    this.drawTrails(placed);
    const offsets = spreadOverlapping(placed);

    for (const employee of placed) {
      const position = this.projectLatLon(employee.lat, employee.lon);
      const marker = this.createMarker(position.x, position.y);
      const offset = offsets.get(employee.id);

      marker.append(selectRing(offset.dx, offset.dy, 11));

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
      marker.append(shape, hitArea(offset.dx, offset.dy), title(employeeTooltip(employee)));
      marker.classList.add("employee-marker");
      // По этому признаку метку находят обработчики и автопроверки:
      // искать человека по подписи ненадёжно.
      marker.dataset.employeeId = employee.id;
      this.makeDraggable(marker, "employee", employee);

      layer.append(marker);
    }

    this.updateMarkerScale();
  }

  /* --- Перетаскивание подвижных объектов: машин, людей, бортов --- */

  /**
   * Пока объект тянут, опрос сервера не перерисовывает его слой — иначе
   * метка выскочила бы из-под курсора. Свежие данные дорисуются сразу
   * после отпускания.
   */
  deferWhileDragging(kind, args) {
    this.lastDraw[kind] = args;
    if (this.drag && this.drag.kind === kind) {
      this.pendingDraw[kind] = args;
      return true;
    }
    this.pendingDraw[kind] = null;
    return false;
  }

  redraw(kind) {
    const args = this.pendingDraw[kind] || this.lastDraw[kind];
    if (!args) return;
    if (kind === "aircraft") this.drawAircraft(...args);
    if (kind === "vehicle") this.drawVehicles(...args);
    if (kind === "employee") this.drawEmployees(...args);
  }

  /** Можно ли переносить объект. Машину — только свободную. */
  canMove(kind, item) {
    if (kind === "employee") return item.location_source !== "gps";
    return kind !== "vehicle" || item.status === "free";
  }

  /**
   * Перенос объекта мышью.
   *
   * Нажали на метку и повели — она «поднимается» (становится крупнее)
   * и идёт за курсором, пока кнопка не отпущена. Подержали без движения —
   * метка тоже поднимается: видно, что объект взят. Карта двигается,
   * если зажать пустое место.
   *
   * onPlainClick — что делает обычный щелчок (у борта — регистрация вызова).
   */
  makeDraggable(marker, kind, item, onPlainClick) {
    marker.classList.add("movable-marker");
    if (this.canMove(kind, item)) marker.classList.add("is-movable");

    marker.addEventListener("mousedown", (event) => this.pressObject(event, kind, item, marker));
    marker.addEventListener("click", (event) => {
      event.stopPropagation();
      // Щелчок, которым закончился перенос, — не щелчок по объекту.
      if (this.swallowClick) {
        this.swallowClick = false;
        return;
      }
      if (onPlainClick) onPlainClick();
    });
  }

  pressObject(event, kind, item, marker) {
    if (event.button !== 0) {
      return;
    }
    const press = {
      kind,
      item,
      marker,
      startX: event.clientX,
      startY: event.clientY,
      lastX: event.clientX,
      lastY: event.clientY,
      origin: { x: Number(marker.dataset.x), y: Number(marker.dataset.y) },
      moved: false,
      lifted: false,
    };

    // Занятую машину не берём: нажатие на ней двигает карту, как на пустом
    // месте, а удержание объясняет, почему её не перенести.
    if (!this.canMove(kind, item)) {
      this.drag = press;
      press.timer = setTimeout(() => {
        if (this.drag !== press) return;
        this.drag = null;
        if (this.onObjectRefused) this.onObjectRefused(kind, item);
      }, HOLD_TO_DRAG_MS);
      return;
    }

    // Нажатие на объект берёт его: карта под ним не двигается, а объект
    // поднимается, как только мышь сдвинулась или кнопку подержали.
    event.stopPropagation();
    event.preventDefault();
    press.grabbed = true;
    this.drag = press;
    press.timer = setTimeout(() => {
      if (this.drag === press) this.lift(press);
    }, HOLD_TO_DRAG_MS);
  }

  /** «Поднять» метку: крупнее, с рамкой, дальше идёт за курсором. */
  lift(press) {
    if (press.lifted) return;
    clearTimeout(press.timer);
    press.lifted = true;
    press.marker.classList.add("is-lifted");
    this.liftMarker(press.marker, true);
    this.svg.classList.add("dragging-object");
    this.startEdgePan(press);
  }

  /** Ставит поднятую метку под курсор — после движения мыши, зума или автопрокрутки. */
  followCursor(drag) {
    const point = this.toGraphCoords(drag.lastX, drag.lastY);
    if (drag.kind === "aircraft") {
      // Борт рядом со стоянкой «прилипает» к ней; дальше от стоянок —
      // встаёт ровно под курсором (ВПП, рулёжка).
      drag.snapStand = this.standNear(point);
      drag.marker.classList.toggle("is-snapped", Boolean(drag.snapStand));
      if (drag.snapStand) {
        this.placeMarker(drag.marker, drag.snapStand.x, drag.snapStand.y);
        return;
      }
    }
    this.placeMarker(drag.marker, point.x, point.y);
  }

  /** Стоянка в пределах AIRCRAFT_SNAP_PX экранных пикселей от точки, или null. */
  standNear(point) {
    const stand = this.nearestStand(point);
    if (!stand) return null;
    const pxPerUnit = this.basePxPerMetre * this.transform.scale;
    const distancePx = Math.hypot(stand.x - point.x, stand.y - point.y) * pxPerUnit;
    return distancePx <= AIRCRAFT_SNAP_PX ? stand : null;
  }

  /**
   * Где рисовать борт: на стоянке, в свободной точке или там, куда его
   * только что перенесли (до ответа сервера). node — вершина графа, к
   * которой он привязан (для щелчка по стоянке).
   */
  aircraftPlace(aircraft) {
    const override = this.overrides.get(`aircraft:${aircraft.id}`);
    if (override && override.standNodeId) {
      const node = this.nodesById.get(override.standNodeId);
      return node ? { x: node.x, y: node.y, node } : null;
    }
    const node = this.nodesById.get(aircraft.stand_node_id) || null;
    const free = override || (aircraft.on_stand === false ? aircraft : null);
    if (free && free.lat != null && free.lon != null) {
      const point = this.projectLatLon(free.lat, free.lon);
      return { x: point.x, y: point.y, node };
    }
    return node ? { x: node.x, y: node.y, node } : null;
  }

  /**
   * Автопрокрутка у края карты.
   *
   * Пока поднятую метку держат у края (или за ним), карта плавно едет
   * в ту сторону, и метка остаётся под курсором — так объект можно унести
   * в любую часть аэропорта, не отпуская кнопку. Чем ближе к краю,
   * тем быстрее.
   */
  startEdgePan(drag) {
    const step = () => {
      if (this.drag !== drag) return;
      const box = this.svg.getBoundingClientRect();
      // inner — расстояние от курсора до края внутри карты;
      // отрицательное — курсор уже за краем, скорость наибольшая.
      const speed = (inner) =>
        inner >= EDGE_PAN_ZONE_PX
          ? 0
          : EDGE_PAN_MAX_PX * Math.min(1, (EDGE_PAN_ZONE_PX - inner) / EDGE_PAN_ZONE_PX);
      const dx = speed(drag.lastX - box.left) - speed(box.right - drag.lastX);
      const dy = speed(drag.lastY - box.top) - speed(box.bottom - drag.lastY);
      if (dx || dy) {
        this.transform.x += dx / this.basePxPerMetre;
        this.transform.y += dy / this.basePxPerMetre;
        this.applyTransform();
        this.followCursor(drag);
        drag.moved = true;
      }
      drag.frame = requestAnimationFrame(step);
    };
    drag.frame = requestAnimationFrame(step);
  }

  setupObjectDrag() {
    window.addEventListener("mousemove", (event) => {
      const drag = this.drag;
      if (!drag) {
        return;
      }
      drag.lastX = event.clientX;
      drag.lastY = event.clientY;
      if (!drag.lifted) {
        const distance = Math.hypot(event.clientX - drag.startX, event.clientY - drag.startY);
        // Дрожание руки при щелчке — ещё не перенос.
        if (distance < DRAG_THRESHOLD_PX) return;
        if (!drag.grabbed) {
          // Занятая машина: это было панорамирование.
          clearTimeout(drag.timer);
          this.drag = null;
          this.flushPending(drag.kind);
          return;
        }
        this.lift(drag);
      }
      drag.moved = true;
      drag.marker.classList.add("is-dragging");
      this.followCursor(drag);
    });

    // Колесо во время переноса: можно отдалить карту, унести объект
    // далеко и приблизить обратно — метка остаётся под курсором.
    this.svg.addEventListener("wheel", () => {
      const drag = this.drag;
      if (drag && drag.lifted) {
        requestAnimationFrame(() => this.followCursor(drag));
      }
    });

    window.addEventListener("mouseup", (event) => {
      const drag = this.drag;
      if (!drag) {
        return;
      }
      this.finishDrag(drag);
      if (drag.lifted) {
        this.swallowClick = true;
        // Щелчок после отпускания может и не прийти (курсор ушёл
        // с метки) — флаг не должен пережить этот жест.
        setTimeout(() => { this.swallowClick = false; }, 0);
      }
      if (drag.moved) {
        drag.lastX = event.clientX;
        drag.lastY = event.clientY;
        this.drop(drag, this.toGraphCoords(drag.lastX, drag.lastY));
      }
      this.flushPending(drag.kind);
    });

    window.addEventListener("keydown", (event) => {
      const drag = this.drag;
      if (event.key !== "Escape" || !drag) {
        return;
      }
      // Esc во время переноса отменяет его: метка возвращается на место.
      this.finishDrag(drag);
      this.placeMarker(drag.marker, drag.origin.x, drag.origin.y);
      this.flushPending(drag.kind);
    });
  }

  finishDrag(drag) {
    clearTimeout(drag.timer);
    cancelAnimationFrame(drag.frame);
    this.drag = null;
    this.endDragVisuals(drag);
  }

  endDragVisuals(drag) {
    this.svg.classList.remove("dragging-object");
    drag.marker.classList.remove("is-dragging", "is-lifted", "is-snapped");
    this.liftMarker(drag.marker, false);
  }

  flushPending(kind) {
    if (this.pendingDraw[kind]) this.redraw(kind);
  }

  /**
   * Объект отпущен в точке графа. До ответа сервера он рисуется на новом
   * месте; при отказе возвращается туда, где стоял.
   */
  drop(drag, point) {
    const { kind, item } = drag;
    const key = `${kind}:${item.id}`;
    let position = this.unprojectXY(point.x, point.y);

    if (kind === "aircraft") {
      // Отпустили у стоянки — борт встаёт на неё; иначе — ровно туда,
      // куда отпустили (ВПП, рулёжка). Сервер получает явное указание.
      const stand = this.standNear(point);
      drag.marker.classList.remove("is-snapped");
      if (stand) {
        this.placeMarker(drag.marker, stand.x, stand.y);
        this.overrides.set(key, { standNodeId: stand.id });
        position = { lat: stand.lat, lon: stand.lon, stand_node_id: stand.id };
      } else {
        this.overrides.set(key, position);
        position = { ...position, free: true };
      }
    } else {
      this.overrides.set(key, position);
    }
    if (kind === "employee") {
      this.trails.delete(item.id);
    }

    const done = () => {
      this.overrides.delete(key);
      this.redraw(kind);
    };
    if (this.onObjectDrop) {
      this.onObjectDrop(kind, item, position, done);
    } else {
      done();
    }
  }

  nearestStand(point) {
    let best = null;
    let bestDistance = Infinity;
    for (const node of this.nodesById.values()) {
      if (node.type !== "stand") continue;
      const distance = Math.hypot(node.x - point.x, node.y - point.y);
      if (distance < bestDistance) {
        best = node;
        bestDistance = distance;
      }
    }
    return best;
  }

  /** Переставляет значок в точку графа, сохраняя экранный размер. */
  placeMarker(marker, x, y) {
    marker.dataset.x = x;
    marker.dataset.y = y;
    const lift = Number(marker.dataset.lift) || 1;
    marker.setAttribute("transform", `translate(${x} ${y}) scale(${this.markerScale * lift})`);
  }

  /** «Поднимает» метку при переносе: она крупнее, пока её держат. */
  liftMarker(marker, lifted) {
    if (lifted) marker.dataset.lift = LIFT_SCALE;
    else delete marker.dataset.lift;
    this.placeMarker(marker, Number(marker.dataset.x), Number(marker.dataset.y));
  }

  /**
   * Короткий след перемещения сотрудников за последние минуты.
   *
   * Точка на карте отвечает на вопрос «где он», а след — «куда и как
   * быстро он движется»: по нему видно, что инженер едет к борту,
   * стоит или свернул не туда, ещё до того, как изменится прогноз.
   * След собирается из опросов в памяти рабочего места; машина, на
   * которой едет инженер, движется вместе с ним, и отдельный след
   * ей не нужен.
   */
  drawTrails(employees) {
    const now = Date.now();
    const seen = new Set();

    for (const employee of employees) {
      seen.add(employee.id);
      // Точка, поставленная вручную (перенос на карте), — не движение:
      // след от старого места к новому выглядел бы как пройденный путь.
      // Такой след сбрасывается и начинается заново с настоящих координат.
      if (employee.location_source === "manual") {
        this.trails.delete(employee.id);
        continue;
      }
      const position = this.projectLatLon(employee.lat, employee.lon);
      const trail = this.trails.get(employee.id) || [];
      const last = trail[trail.length - 1];
      // Новая точка добавляется, только если сотрудник сдвинулся
      // заметнее погрешности: дрожание GPS следом не считается.
      if (!last || Math.hypot(last.x - position.x, last.y - position.y) > TRAIL_MIN_STEP_M) {
        trail.push({ x: position.x, y: position.y, at: now });
      }
      this.trails.set(employee.id, trail.filter(isRecentTrailPoint, now));
    }
    for (const id of [...this.trails.keys()]) {
      if (!seen.has(id)) {
        this.trails.delete(id);
      }
    }

    const layer = this.layers.trails;
    layer.replaceChildren();
    for (const trail of this.trails.values()) {
      if (trail.length < 2) {
        continue;
      }
      const line = document.createElementNS(SVG_NS, "polyline");
      line.setAttribute("points", trail.map(trailPoint).join(" "));
      line.setAttribute("class", "trail");
      layer.append(line);
    }
  }

  /**
   * Маршрут кандидата или назначенного исполнителя.
   *
   * Если по пути надо забрать машину, маршрут рисуется двумя участками:
   * пеший — пунктиром, на машине — сплошной линией. Иначе диспетчер
   * не отличил бы «идёт пешком» от «едет».
   */
  drawRoute(nodeIds, pickupNodeId, employee) {
    this.clearRoute();

    const known = (nodeIds || []).filter((id) => this.nodesById.has(id));
    if (known.length === 0) {
      return false;
    }
    // Маршрут из одной вершины. Если известно, где исполнитель, линия
    // идёт от его текущего места; иначе он уже у борта — отмечен конец.
    if (known.length === 1) {
      const target = this.nodesById.get(known[0]);
      if (employee && employee.lat != null && employee.lon != null) {
        const start = this.projectLatLon(employee.lat, employee.lon);
        this.appendRouteLine(known, "route", start);
        this.appendRouteEnd(start, "start", "А", "Исполнитель");
        this.appendRouteEnd(target, "finish", "Б", "Конец маршрута — стоянка борта");
      } else {
        this.appendRouteEnd(target, "finish", "Б", "Сотрудник уже у борта");
      }
      this.updateMarkerScale();
      return true;
    }

    const split = pickupNodeId ? known.indexOf(pickupNodeId) : -1;
    if (split > 0 && split < known.length) {
      this.appendRouteLine(known.slice(0, split + 1), "route route-walk");
      this.appendRouteLine(known.slice(split), "route");
    } else {
      this.appendRouteLine(known, "route");
    }

    // Концы маршрута отмечены отдельно и крупно: точка сотрудника мелкая,
    // а силуэт борта на стоянке легко принять за обычный значок карты —
    // без отметок было непонятно, откуда и куда идёт линия.
    const first = this.nodesById.get(known[0]);
    const last = this.nodesById.get(known[known.length - 1]);
    if (split > 0 && split < known.length - 1) {
      this.appendRouteEnd(this.nodesById.get(known[split]), "pickup", "М", "Здесь забрать машину");
    }
    this.appendRouteEnd(first, "start", "А", "Начало маршрута");
    this.appendRouteEnd(last, "finish", "Б", "Конец маршрута — стоянка борта");
    this.updateMarkerScale();
    return true;
  }

  /**
   * Маршрут, а если его нет — явная отметка «маршрут не построен»
   * на стоянке вызова.
   */
  showRoute(nodeIds, pickupNodeId, standNodeId, missingText, employee) {
    if (!this.drawRoute(nodeIds, pickupNodeId, employee) && standNodeId) {
      this.drawRouteMissing(standNodeId, missingText);
    }
  }

  /**
   * Маршрут не построен: подсветка стоянки и подпись.
   *
   * Раньше в этом случае карта просто оставалась без линии, и на месте
   * вызова виднелся только мелкий силуэт борта — диспетчер не понимал,
   * маршрут ещё считается, не нужен или не нашёлся.
   */
  drawRouteMissing(standNodeId, text) {
    this.clearRoute();

    const node = this.nodesById.get(standNodeId);
    if (!node) {
      return;
    }

    const marker = this.createMarker(node.x, node.y);
    marker.classList.add("route-missing");

    const ring = document.createElementNS(SVG_NS, "circle");
    ring.setAttribute("r", 20);
    ring.setAttribute("class", "route-missing-ring");

    const label = document.createElementNS(SVG_NS, "text");
    label.setAttribute("y", 38);
    label.setAttribute("class", "route-missing-label");
    label.textContent = text || "Маршрут не построен";

    marker.append(ring, label, title(text || "Маршрут не построен"));
    this.layers.routeEnds.append(marker);
    this.updateMarkerScale();
  }

  /** Одна ломаная маршрута по вершинам графа. */
  appendRouteLine(nodeIds, className, start) {
    const points = nodeIds
      .map((id) => this.nodesById.get(id))
      .filter(Boolean)
      .map((node) => `${node.x},${node.y}`);

    if (start) points.unshift(`${start.x},${start.y}`);
    if (points.length < 2) {
      return;
    }

    // Тёмная подложка под линией: на спутниковом снимке синяя линия
    // без окантовки сливалась с крышами и рулёжками.
    const casing = document.createElementNS(SVG_NS, "polyline");
    casing.setAttribute("points", points.join(" "));
    casing.setAttribute("class", "route-casing");

    const line = document.createElementNS(SVG_NS, "polyline");
    line.setAttribute("points", points.join(" "));
    line.setAttribute("class", className);
    this.layers.route.append(casing, line);
  }

  /** Отметка конца маршрута постоянного экранного размера с буквой внутри. */
  appendRouteEnd(node, kind, letter, hint) {
    if (!node) {
      return;
    }
    const marker = this.createMarker(node.x, node.y);
    marker.classList.add("route-end", `route-end-${kind}`);

    if (kind === "finish") {
      const halo = document.createElementNS(SVG_NS, "circle");
      halo.setAttribute("r", 24);
      halo.setAttribute("class", "route-end-halo");
      marker.append(halo);
    }

    const disc = document.createElementNS(SVG_NS, "circle");
    disc.setAttribute("r", kind === "pickup" ? 11 : 14);
    disc.setAttribute("class", "route-end-disc");

    const text = document.createElementNS(SVG_NS, "text");
    text.setAttribute("y", 0.5);
    text.setAttribute("class", "route-end-letter");
    text.textContent = letter;

    marker.append(disc, text, title(hint));
    this.layers.routeEnds.append(marker);
  }

  clearRoute() {
    this.layers.route.replaceChildren();
    this.layers.routeEnds.replaceChildren();
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

  /**
   * Обратное преобразование: метры карты в широту и долготу.
   *
   * Нужно редактору аэропорта: администратор указывает точку пальцем
   * по снимку, а храним мы географические координаты — только они
   * не зависят от того, какой участок и в каком масштабе был на экране.
   */
  unprojectXY(x, y) {
    const EARTH_RADIUS_M = 6371008.8;
    const reference = this.graph.ref_point;
    const toDeg = 180 / Math.PI;

    return {
      lat: reference.lat - (y / EARTH_RADIUS_M) * toDeg,
      lon:
        reference.lon +
        (x / (EARTH_RADIUS_M * Math.cos((reference.lat * Math.PI) / 180))) * toDeg,
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
        `translate(${marker.dataset.x} ${marker.dataset.y}) scale(${scale * (Number(marker.dataset.lift) || 1)})`
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

  /**
   * Точка экрана в координатах графа — с учётом зума и сдвига карты.
   *
   * toMapCoords даёт координаты в системе viewBox, то есть ДО применения
   * transform к слоям. Содержимое же нарисовано внутри viewport, и на
   * экране точка графа g оказывается в g * scale + translate. Значит
   * обратный пересчёт обязан делить на масштаб и вычитать сдвиг — иначе
   * на приближённой или сдвинутой карте клик попадает не туда, куда
   * показывает курсор.
   */
  toGraphCoords(clientX, clientY) {
    const point = this.toMapCoords(clientX, clientY);
    const { x, y, scale } = this.transform;
    return { x: (point.x - x) / scale, y: (point.y - y) / scale };
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

    // Удержание объекта превращает начатое панорамирование в его перенос:
    // карта возвращается туда, где была в момент нажатия.
    this.cancelPan = () => {
      if (dragging && origin) {
        this.transform.x = origin.x;
        this.transform.y = origin.y;
        this.applyTransform();
      }
      dragging = false;
      this.svg.classList.remove("dragging");
    };

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

  /** Наводит карту на произвольную точку графа — для поиска по карте. */
  focusOnPoint(x, y, scale = 5) {
    if (!this.baseView) {
      return;
    }
    const centre = this.screenCentre();
    this.transform.scale = clamp(Math.max(this.transform.scale, scale), MIN_ZOOM, MAX_ZOOM);
    this.transform.x = centre.x - this.transform.scale * x;
    this.transform.y = centre.y - this.transform.scale * y;
    this.applyTransform();
  }

  /**
   * Короткая подсветка найденного объекта: расходящиеся кольца
   * и подпись. Через несколько секунд исчезает сама — это указатель
   * «вот он», а не постоянная отметка.
   */
  flashAt(x, y, text) {
    if (!this.layers.search) {
      this.layers.search = createGroup("layer-search");
      this.viewport.append(this.layers.search);
    }
    const layer = this.layers.search;
    layer.replaceChildren();
    clearTimeout(this.flashTimer);

    const marker = this.createMarker(x, y);
    marker.classList.add("search-flash");
    for (const delay of [0, 0.5]) {
      const ring = document.createElementNS(SVG_NS, "circle");
      ring.setAttribute("r", 22);
      ring.setAttribute("class", "search-flash-ring");
      ring.style.animationDelay = `${delay}s`;
      marker.append(ring);
    }
    const core = document.createElementNS(SVG_NS, "circle");
    core.setAttribute("r", 16);
    core.setAttribute("class", "search-flash-core");
    marker.append(core);

    if (text) {
      const label = document.createElementNS(SVG_NS, "text");
      label.setAttribute("y", -24);
      label.setAttribute("class", "search-flash-label");
      label.textContent = text;
      marker.append(label);
    }
    layer.append(marker);
    this.updateMarkerScale();
    this.flashTimer = setTimeout(() => layer.replaceChildren(), 4500);
  }
}

/* --- Вспомогательные функции --- */

function createGroup(className) {
  const group = document.createElementNS(SVG_NS, "g");
  group.setAttribute("class", className);
  return group;
}

// Сколько держать кнопку на объекте, чтобы он «поднялся» для переноса.
const HOLD_TO_DRAG_MS = 300;
// Смещение мыши, после которого нажатие считается движением.
const DRAG_THRESHOLD_PX = 4;
// Автопрокрутка при переносе: ширина полосы у края карты, в которой
// карта начинает ехать, и наибольшая скорость, пикселей за кадр.
const EDGE_PAN_ZONE_PX = 48;
const EDGE_PAN_MAX_PX = 14;
// Насколько близко к стоянке (в экранных пикселях) нужно отпустить борт,
// чтобы он встал на неё, а не в свободную точку.
const AIRCRAFT_SNAP_PX = 24;
// Во сколько раз крупнее метка, пока её держат и переносят.
const LIFT_SCALE = 1.5;

/** Кольцо выделения подвижного объекта; видно только у выбранного. */
function selectRing(cx, cy, r) {
  const ring = document.createElementNS(SVG_NS, "circle");
  ring.setAttribute("cx", cx);
  ring.setAttribute("cy", cy);
  ring.setAttribute("r", r);
  ring.setAttribute("class", "select-ring");
  return ring;
}

/** Невидимая увеличенная зона захвата: мелкую метку мышью ловить трудно. */
function hitArea(cx, cy) {
  const hit = document.createElementNS(SVG_NS, "circle");
  hit.setAttribute("cx", cx);
  hit.setAttribute("cy", cy);
  hit.setAttribute("r", 12);
  hit.setAttribute("class", "hit-area");
  return hit;
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

/** Подсказка к машине: позывной, тип и у кого она сейчас. */
function vehicleTooltip(vehicle) {
  if (vehicle.status === "reserved") {
    return `${vehicle.call_sign} · ${vehicle.kind}\nждёт: ${vehicle.employee_name}`;
  }
  return `${vehicle.call_sign} · ${vehicle.kind}\nсвободна`;
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
