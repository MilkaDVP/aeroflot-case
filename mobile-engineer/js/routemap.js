/*
 * Карта в приложении инженера.
 *
 * Показывается всегда, а не только при активном вызове. Изначально карта
 * появлялась вместе с вызовом, и это было ошибкой: подсказка «коснитесь
 * карты, чтобы задать координаты» относилась к тому, чего на экране нет,
 * а поставить себя на перрон до прихода вызова — ровно то, что нужно
 * для демонстрации, — было невозможно.
 *
 * Что рисуется: спутниковая подложка, ломаная маршрута (если есть вызов),
 * цель и своя точка. Граф аэропорта на телефон не тянется — он весит
 * около мегабайта, а сервер отдаёт готовые точки маршрута вместе
 * с вызовом. Подложка даёт то, ради чего нужна карта: понимание, где
 * человек находится относительно зданий и перрона.
 *
 * Проекция — та же линейная, что на сервере: метры от опорной точки.
 * Тайлы приходят в Web Mercator, но вблизи одной широты обе системы
 * отличаются равномерным множителем, поэтому снимок ложится в метровую
 * систему без перепроецирования.
 */

const SVG_NS = "http://www.w3.org/2000/svg";
const EARTH_RADIUS_M_MAP = 6371008.8;
const EARTH_CIRCUMFERENCE_M = 40075016.686;
const TILE_SIZE = 256;

// Дальняя GPS-фиксация может попасть в один кадр с аэропортом.
const MIN_TILE_ZOOM = 0;
const MAX_TILE_ZOOM = 19;

// Охват карты, когда вызова нет: примерно квартал перрона вокруг человека.
const IDLE_SPAN_M = 700;
// Охват, когда координат ещё нет и карта наведена на центр аэропорта:
// шире, чтобы человек мог найти себя и поставить точку касанием.
const FALLBACK_SPAN_M = 2400;
// Наименьший охват при активном вызове: у самого борта карта не должна
// растягиваться на несколько метров во весь экран.
const MIN_ROUTE_SPAN_M = 160;
const PADDING_RATIO = 0.14;
// Крупный план вокруг инженера: видны ближайшие повороты, стоянки
// и их номера.
const FOLLOW_SPAN_M = 150;

// Пределы масштаба, который инженер задаёт сам (щипок, колесо, кнопки):
// метры по меньшей стороне карты.
const USER_SPAN_MIN_M = 16;
const USER_SPAN_MAX_M = 8000;
// Смещение пальца, после которого касание — уже сдвиг карты, а не «я здесь».
const TAP_SLOP_PX = 8;

// Период волны вокруг инженера — тот же, что у self-pulse в app.css.
const SELF_PULSE_MS = 2400;

// Значки меток, сетка 24×24 (Material Design Icons, Apache 2.0).
const ICON_PLANE = "M21 16v-2l-8-5V3.5a1.5 1.5 0 0 0-3 0V9l-8 5v2l8-2.5V19l-2 1.5V22l3.5-1 3.5 1v-1.5L13 19v-5.5z";
const ICON_CAR = "M18.92 6.01C18.72 5.42 18.16 5 17.5 5h-11c-.66 0-1.21.42-1.42 1.01L3 12v8c0 .55.45 1 1 1h1c.55 0 1-.45 1-1v-1h12v1c0 .55.45 1 1 1h1c.55 0 1-.45 1-1v-8zM6.5 16a1.5 1.5 0 1 1 0-3 1.5 1.5 0 0 1 0 3zm11 0a1.5 1.5 0 1 1 0-3 1.5 1.5 0 0 1 0 3zM5 11l1.5-4.5h11L19 11z";
const ICON_FLAG = "M14.4 6L14 4H5v17h2v-7h5.6l.4 2h7V6z";
const ICON_WRENCH = "M22.7 19l-9.1-9.1c.9-2.3.4-5-1.5-6.9-2-2-5-2.4-7.4-1.3L9 6 6 9 1.6 4.7C.4 7.1.9 10.1 2.9 12.1c1.9 1.9 4.6 2.4 6.9 1.5l9.1 9.1c.4.4 1 .4 1.4 0l2.3-2.3c.5-.4.5-1.1.1-1.4z";

const ESRI_IMAGERY =
  "https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}";

class RouteMap {
  constructor(svgElement, onTap) {
    this.svg = svgElement;
    this.onTap = onTap;
    this.reference = null;
    this.view = null;
    this.showImagery = false;

    this.tileLayer = group("layer-tiles");
    this.contentLayer = group("layer-content");
    this.graphLayer = group("airport-network");
    this.labelLayer = group("airport-labels");
    this.svg.append(this.tileLayer, this.graphLayer, this.labelLayer, this.contentLayer);

    // Свой вид инженера переживает обновления данных: карта больше не
    // возвращается к автоматическому охвату на каждом опросе.
    // userCentre — точка на местности (lat/lon), а не сдвиг от инженера:
    // сдвиг складывался с перемещением точки, и экран улетал.
    // null — карта едет за инженером. userSpan — метры по меньшей стороне,
    // null — автоматический охват.
    this.userCentre = null;
    this.userSpan = null;
    this.base = null;
    this.lastArgs = null;
    this.markers = [];
    this.pointers = new Map();
    this.gesture = null;
    this.suppressTap = false;
    this.onViewChange = null;

    this.svg.addEventListener("click", (event) => this.handleTap(event));
    this.setupGestures();
  }

  /* --- Масштаб и сдвиг пальцами, колесом и кнопками --- */

  setupGestures() {
    const svg = this.svg;
    svg.addEventListener("pointerdown", (event) => {
      // Новое касание: следы прошлого жеста больше не действуют, иначе
      // потерянный палец превратил бы касание в щипок.
      if (event.isPrimary) {
        this.pointers.clear();
        this.gesture = null;
        this.suppressTap = false;
      }
      // Захват нужен, чтобы жест не обрывался, когда палец уходит с карты;
      // если браузер отказал — работаем без него.
      try {
        svg.setPointerCapture(event.pointerId);
      } catch (error) {
        // захват недоступен — не страшно
      }
      this.pointers.set(event.pointerId, { x: event.clientX, y: event.clientY });
      this.startGesture();
    });
    svg.addEventListener("pointermove", (event) => {
      if (!this.pointers.has(event.pointerId)) return;
      this.pointers.set(event.pointerId, { x: event.clientX, y: event.clientY });
      this.moveGesture();
    });
    const end = (event) => {
      if (!this.pointers.delete(event.pointerId)) return;
      if (this.pointers.size === 0) {
        if (this.gesture && this.gesture.moved) {
          // Касание, которым закончился сдвиг, — не «я здесь». На телефоне
          // click приходит с задержкой, поэтому запрет снимает следующее
          // касание, а не таймер.
          this.suppressTap = true;
          this.rerender();
        }
        this.gesture = null;
      } else {
        this.startGesture();
      }
    };
    svg.addEventListener("pointerup", end);
    svg.addEventListener("pointercancel", end);
    svg.addEventListener("lostpointercapture", end);
    svg.addEventListener("wheel", (event) => {
      event.preventDefault();
      this.zoomAt(event.deltaY < 0 ? 1.25 : 1 / 1.25, event.clientX, event.clientY);
      this.scheduleRerender();
    }, { passive: false });
  }

  /** Запоминает исходное положение пальцев и вида для текущего жеста. */
  startGesture() {
    const points = [...this.pointers.values()];
    const moved = this.gesture ? this.gesture.moved : false;
    this.gesture = {
      moved,
      centre: midpoint(points),
      spread: points.length > 1 ? distance(points[0], points[1]) : 0,
    };
  }

  moveGesture() {
    const gesture = this.gesture;
    if (!gesture || !this.view) return;
    const points = [...this.pointers.values()];
    const centre = midpoint(points);
    const dx = centre.x - gesture.centre.x;
    const dy = centre.y - gesture.centre.y;
    if (!gesture.moved && points.length < 2 && Math.hypot(dx, dy) < TAP_SLOP_PX) return;
    gesture.moved = true;

    if (points.length > 1 && gesture.spread > 0) {
      // Щипок: масштаб относительно точки между пальцами, плюс сдвиг
      // самой этой точки. Отсчёт каждый раз от предыдущего положения.
      const spread = distance(points[0], points[1]);
      this.zoomAt(spread / gesture.spread, gesture.centre.x, gesture.centre.y);
      this.panBy(dx, dy);
      gesture.centre = centre;
      gesture.spread = spread;
      return;
    }
    this.panBy(dx, dy);
    gesture.centre = centre;
  }

  /** Сдвиг вида на экранные пиксели (карта едет за пальцем). */
  panBy(dx, dy) {
    const box = this.svg.getBoundingClientRect();
    const mpp = this.view.width / (box.width || 1);
    this.moveCentre(-dx * mpp, -dy * mpp);
  }

  /** Масштаб с неподвижной точкой под пальцем или курсором. */
  zoomAt(factor, clientX, clientY) {
    if (!this.view || !this.base) return;
    const box = this.svg.getBoundingClientRect();
    const fx = (clientX - box.left) / (box.width || 1);
    const fy = (clientY - box.top) / (box.height || 1);
    // Точка под пальцем в метрах — после масштаба она должна остаться там же.
    const px = this.view.minX + fx * this.view.width;
    const py = this.view.minY + fy * this.view.height;
    this.setSpan(factor);
    this.moveCentre(
      px - (this.view.minX + fx * this.view.width),
      py - (this.view.minY + fy * this.view.height)
    );
  }

  /** Меняет масштаб, не трогая центр. */
  setSpan(factor) {
    const current = this.view.scaleRef;
    this.userSpan = Math.min(
      Math.max(USER_SPAN_MAX_M, current),
      Math.max(USER_SPAN_MIN_M, current / factor)
    );
    this.applyView();
  }

  /** Сдвигает центр вида на метры и отвязывает карту от инженера. */
  moveCentre(dx, dy) {
    const view = this.view;
    this.userCentre = this.unproject(
      view.minX + view.width / 2 + dx,
      view.minY + view.height / 2 + dy
    );
    this.applyView();
  }

  /** Кнопки «+» и «−»: масштаб от центра карты. */
  zoomBy(factor) {
    if (!this.view) return;
    this.setSpan(factor);
    this.rerender();
  }

  /** Оставляет на экране ровно то, что видно сейчас, что бы ни сдвинулось. */
  holdView() {
    if (!this.view) return;
    this.userSpan = this.view.scaleRef;
    this.moveCentre(0, 0);
  }

  /** «Ко мне»: центр — на инженера, масштаб остаётся прежним. */
  locate() {
    if (!this.view || !this.lastArgs) return;
    const [, , position, , , , centre] = this.lastArgs;
    this.userSpan = this.view.scaleRef;
    // Пока карта наведена на аэропорт (centre), следить не за кем.
    this.userCentre = position && centre ? { lat: position.lat, lon: position.lon } : null;
    this.rerender();
  }

  /** Полный сброс к автоматическому виду — новая смена, другой аэропорт. */
  resetView() {
    this.userSpan = null;
    this.userCentre = null;
    this.rerender();
  }

  isUserView() {
    return this.userCentre !== null;
  }

  /** Автоматический охват плюс масштаб и сдвиг инженера. */
  applyView() {
    if (!this.base) return;
    const span = this.userSpan ?? this.base.span;
    const aspect = this.base.aspect;
    const width = aspect >= 1 ? span * aspect : span;
    const height = aspect >= 1 ? span : span / aspect;
    const { x: cx, y: cy } = this.userCentre ? this.project(this.userCentre) : this.base;
    this.view = {
      minX: cx - width / 2,
      minY: cy - height / 2,
      width,
      height,
      scaleRef: Math.min(width, height),
    };
    this.svg.setAttribute("viewBox", `${this.view.minX} ${this.view.minY} ${width} ${height}`);
    this.placeMarkers();
    // Подписи стоянок — постоянного экранного размера; издалека их
    // не показываем, иначе сотня номеров сливается в кашу.
    const font = this.view.scaleRef * 0.032;
    this.labelLayer.setAttribute("font-size", font);
    this.labelLayer.setAttribute("stroke-width", font * 0.28);
    this.labelLayer.style.display = this.view.scaleRef > 1200 ? "none" : "";
    if (this.onViewChange) this.onViewChange(this.isUserView());
  }

  /** Перерисовать с теми же данными — после жеста значки и снимок под новый масштаб. */
  rerender() {
    if (this.lastArgs) this.render(...this.lastArgs);
  }

  scheduleRerender() {
    clearTimeout(this.rerenderTimer);
    this.rerenderTimer = setTimeout(() => this.rerender(), 120);
  }

  /**
   * Схема аэропорта — та же, что на карте диспетчера: контур территории,
   * здания и склады, зоны обслуживания ВС, дороги по видам (служебная,
   * пешеходная, рулёжка, ВПП), стоянки с номерами. Координаты x/y
   * в графе уже в метрах от опорной точки — в той же проекции, что здесь.
   */
  setGraph(graph) {
    this.graphOrigin = graph?.ref_point || graph?.nodes[0];
    this.graphLayer.replaceChildren();
    this.labelLayer.replaceChildren();
    if (this.graphOrigin) {
      this.reference = this.graphOrigin;
      const at = (node) => (Number.isFinite(node.x) ? { x: node.x, y: node.y } : this.project(node));
      const ring = (points) => "M" + points.map((p) => `${p[0]},${p[1]}`).join(" L") + " Z";
      const area = (points, className, hint) => {
        const path = svgElement("path", { d: ring(points), class: className });
        if (hint) path.append(svgTitle(hint));
        this.graphLayer.append(path);
      };
      for (const points of graph.boundary || []) area(points, "area area-boundary");
      for (const item of graph.objects || []) {
        for (const points of item.rings || []) area(points, `area area-${item.kind || "object"}`, item.name);
        if (item.label && item.kind !== "runway" && item.name) {
          this.labelLayer.append(svgElement("text", { x: item.label[0], y: item.label[1], class: "label-area" }, item.name));
        }
      }
      for (const zone of graph.zones || []) {
        for (const points of zone.rings || []) area(points, "area area-zone", `Зона обслуживания ВС ${zone.ref}`);
      }

      const nodes = new Map(graph.nodes.map((n) => [n.id, n]));
      for (const edge of graph.edges) {
        const a = nodes.get(edge.from_id), b = nodes.get(edge.to_id);
        if (!a || !b) continue;
        const pa = at(a), pb = at(b);
        const middle = (edge.points || []).map((p) => `L${p[0]} ${p[1]}`).join("");
        const kind = edge.kind ? ` road-${edge.kind}` : "";
        this.graphLayer.append(svgElement("path", {
          d: `M${pa.x} ${pa.y}${middle}L${pb.x} ${pb.y}`,
          class: (edge.vehicle_allowed ? "road" : "road walk-only") + kind,
        }));
      }

      for (const node of graph.nodes) {
        if (node.type === "junction") continue;
        const p = at(node);
        if (["warehouse", "vehicle_parking", "terminal", "tech_center"].includes(node.type)) {
          const size = node.type === "tech_center" ? 10 : 7;
          this.graphLayer.append(svgElement("rect", {
            x: p.x - size / 2, y: p.y - size / 2, width: size, height: size, class: `place place-${node.type}`,
          }));
          if (node.type === "tech_center" && node.ref) {
            this.labelLayer.append(svgElement("text", { x: p.x, y: p.y - 9, class: "label-tech" }, node.ref));
          }
          continue;
        }
        this.graphLayer.append(svgElement("circle", { cx: p.x, cy: p.y, r: 3.5, class: "stand" }));
        if (node.ref) {
          this.labelLayer.append(svgElement("text", { x: p.x, y: p.y - 6, class: "label-stand" }, node.ref));
        }
      }
    }
    this.rerender();
  }

  setImagery(enabled) {
    this.showImagery = enabled;
    this.svg.classList.toggle("has-imagery", enabled);
    this.tileLayer.replaceChildren();
  }

  /**
   * Перерисовывает карту.
   *
   * @param {Array} routePoints точки маршрута [{lat, lon}] или пустой список
   * @param {Object} target цель — стоянка воздушного судна, либо null
   * @param {Object} position текущее место инженера, либо null
   * @param {Object} fallback опорная точка аэропорта, если места ещё нет
   */
  render(routePoints, target, position, fallback, vehicle, pickupNodeId, centre, stops, remaining = routePoints) {
    this.lastArgs = [routePoints, target, position, fallback, vehicle, pickupNodeId, centre, stops, remaining];
    const points = routePoints || [];
    const all = [...points];
    if (target) {
      all.push(target);
    }
    if (position) {
      all.push(position);
    }
    // Машину, которую надо забрать, обязательно видно на карте.
    const pickupVehicle = vehicle && vehicle.pickup ? vehicle : null;
    if (pickupVehicle) {
      all.push(pickupVehicle);
    }
    fallback ||= this.graphOrigin;
    const usingFallback = all.length === 0 && Boolean(fallback);
    if (usingFallback) {
      all.push(fallback);
    }

    this.contentLayer.replaceChildren();
    this.markers = [];
    if (all.length === 0) {
      // Совсем не от чего оттолкнуться: ни вызова, ни координат,
      // ни аэропорта. Пустая карта честнее случайной точки на глобусе.
      this.tileLayer.replaceChildren();
      return;
    }

    this.reference = this.graphOrigin || centre || all[0];
    // Главный вид — крупный план вокруг инженера: карта едет за ним,
    // как в навигаторе. Пока места нет — маршрут или аэропорт целиком.
    if (position && !centre) {
      this.fit([this.project(position)], FOLLOW_SPAN_M);
    } else {
      const extent = centre ? [centre] : all;
      this.fit(extent.map((point) => this.project(point)),
        centre || usingFallback ? FALLBACK_SPAN_M : points.length ? MIN_ROUTE_SPAN_M : IDLE_SPAN_M);
    }
    this.drawTiles();

    if (points.length >= 2) {
      this.drawLegs(remaining || [], pickupNodeId);
    }
    if (pickupVehicle) {
      this.drawVehicle(this.project(pickupVehicle));
    }
    // Обязательные точки впереди: склад — гаечный ключ, прочие — флажок.
    for (const stop of stops || []) {
      const place = points.find((point) => point.id === stop.id);
      if (!place) continue;
      if (stop.warehouse) this.drawWarehouse(this.project(place));
      else this.addBadge(this.project(place), "route-pass", ICON_FLAG);
    }
    if (target) {
      this.drawTarget(this.project(target));
    }
    if (position) {
      this.drawSelf(this.project(position));
    }
  }

  /** Линейная проекция в метры от опорной точки. */
  project(point) {
    const toRad = Math.PI / 180;
    return {
      x:
        (point.lon - this.reference.lon) *
        toRad *
        EARTH_RADIUS_M_MAP *
        Math.cos(this.reference.lat * toRad),
      y: (this.reference.lat - point.lat) * toRad * EARTH_RADIUS_M_MAP,
    };
  }

  /** Обратное преобразование — для касания по карте. */
  unproject(x, y) {
    const toRad = Math.PI / 180;
    return {
      lat: this.reference.lat - y / (EARTH_RADIUS_M_MAP * toRad),
      lon:
        this.reference.lon +
        x / (EARTH_RADIUS_M_MAP * toRad * Math.cos(this.reference.lat * toRad)),
    };
  }

  /**
   * Подбирает viewBox под содержимое и под форму области на экране.
   *
   * Пропорции берутся у контейнера, а не задаются квадратом. Квадратный
   * viewBox в вытянутой по вертикали области оставлял сверху и снизу
   * пустые полосы, куда подложка не доходила: тайлы считались только
   * для квадрата, а места на экране было больше.
   */
  fit(points, minimum) {
    const box = this.svg.getBoundingClientRect();
    const boxWidth = box.width || 320;
    const boxHeight = box.height || 320;

    const xs = points.map((point) => point.x);
    const ys = points.map((point) => point.y);

    const extent = Math.max(
      Math.max(...xs) - Math.min(...xs),
      Math.max(...ys) - Math.min(...ys)
    );
    const span = Math.max(extent, minimum) * (1 + PADDING_RATIO * 2);

    // Заданный охват укладывается в меньшую сторону, большая получает
    // пропорционально больше местности (см. applyView).
    const aspect = boxWidth / boxHeight;
    const centreX = (Math.max(...xs) + Math.min(...xs)) / 2;
    const centreY = (Math.max(...ys) + Math.min(...ys)) / 2;

    // Автоматический охват; поверх него — масштаб и сдвиг инженера.
    // Опорный размер значков (scaleRef) берётся от итогового вида, чтобы
    // они не зависели ни от масштаба, ни от того, насколько вытянут экран.
    this.base = { x: centreX, y: centreY, span, aspect };
    this.applyView();
  }

  /* --- Подложка --- */

  /**
   * Выкладывает тайлы снимка под содержимым карты.
   *
   * Тайлы пересобираются целиком при каждой отрисовке: их единицы,
   * а карта перерисовывается редко — только при смене вызова или
   * заметном перемещении. Кэш здесь усложнил бы код без выигрыша.
   */
  drawTiles() {
    const existing = new Map(Array.from(this.tileLayer.querySelectorAll("[data-key]"), tile => [tile.dataset.key, tile]));
    if (!this.showImagery || !this.view) {
      return;
    }

    const box = this.svg.getBoundingClientRect();
    const sidePx = Math.min(box.width, box.height) || 320;
    const zoom = this.pickZoom(sidePx);

    const resolution = groundResolution(this.reference.lat, zoom);
    const worldX0 = lonToWorldX(this.reference.lon, zoom);
    const worldY0 = latToWorldY(this.reference.lat, zoom);
    const tileSpanM = TILE_SIZE * resolution;
    // Соседние тайлы перекрываются на доли процента: без этого между
    // ними видны светлые швы от округления при растеризации.
    const bleed = tileSpanM * 0.004;

    const columns = [
      tileIndex(this.view.minX, resolution, worldX0),
      tileIndex(this.view.minX + this.view.width, resolution, worldX0),
    ];
    const rows = [
      tileIndex(this.view.minY, resolution, worldY0),
      tileIndex(this.view.minY + this.view.height, resolution, worldY0),
    ];
    const limit = 2 ** zoom;

    for (let cx = Math.max(0, columns[0]); cx <= Math.min(columns[1], limit - 1); cx += 1) {
      for (let cy = Math.max(0, rows[0]); cy <= Math.min(rows[1], limit - 1); cy += 1) {
        const key = [zoom, cx, cy].join("/");
        const image = existing.get(key) || satelliteTile(ESRI_IMAGERY, zoom, cx, cy);
        existing.delete(key);
        image.dataset.key = key;
        image.setAttribute("x", (cx * TILE_SIZE - worldX0) * resolution);
        image.setAttribute("y", (cy * TILE_SIZE - worldY0) * resolution);
        image.setAttribute("width", tileSpanM + bleed);
        image.setAttribute("height", tileSpanM + bleed);
        image.setAttribute("preserveAspectRatio", "none");
        image.classList.add("map-tile");
        if (!image.parentNode) this.tileLayer.append(image);
      }
    }

    for (const tile of existing.values()) tile.remove();
    const scrim = this.tileLayer.querySelector(".map-scrim") || document.createElementNS(SVG_NS, "rect");
    scrim.setAttribute("x", this.view.minX);
    scrim.setAttribute("y", this.view.minY);
    scrim.setAttribute("width", this.view.width);
    scrim.setAttribute("height", this.view.height);
    scrim.setAttribute("class", "map-scrim");
    this.tileLayer.append(scrim);
  }

  /** Уровень детализации: пиксель тайла примерно равен пикселю экрана. */
  pickZoom(sidePx) {
    const metresPerPixel = Math.min(this.view.width, this.view.height) / sidePx;
    const exact = Math.log2(
      (EARTH_CIRCUMFERENCE_M * Math.cos((this.reference.lat * Math.PI) / 180)) /
        (TILE_SIZE * metresPerPixel)
    );
    return Math.max(MIN_TILE_ZOOM, Math.min(MAX_TILE_ZOOM, Math.round(exact)));
  }

  /* --- Содержимое --- */

  /**
   * Маршрут одним или двумя участками.
   *
   * Если по пути надо забрать машину, пеший участок до неё — пунктиром,
   * дальше на машине — сплошной линией.
   */
  drawLegs(points, pickupNodeId) {
    const projected = points.map((point) => this.project(point));
    const split = pickupNodeId ? points.findIndex((point) => point.id === pickupNodeId) : -1;

    if (split > 0 && split < points.length) {
      this.drawRoute(projected.slice(0, split + 1), "route-line is-walk");
      this.drawRoute(projected.slice(split), "route-line");
      return;
    }
    this.drawRoute(projected, "route-line");
  }

  drawRoute(points, className) {
    if (points.length < 2) return;
    const line = document.createElementNS(SVG_NS, "polyline");
    line.setAttribute("points", points.map((p) => `${p.x},${p.y}`).join(" "));
    line.setAttribute("class", className);
    this.contentLayer.append(line);
  }

  /**
   * Метка постоянного экранного размера: внутри неё единица — пиксель,
   * начало координат — сама точка. Масштаб пересчитывает applyView,
   * поэтому метки не растут ни при щипке, ни на большом экране.
   */
  addMarker(point, className, parts) {
    const marker = group(`marker ${className}`);
    for (const [name, attributes] of parts) marker.append(svgElement(name, attributes));
    this.contentLayer.append(marker);
    this.markers.push({ marker, point });
    this.placeMarkers();
    return marker;
  }

  placeMarkers() {
    const scale = this.view.width / (this.svg.getBoundingClientRect().width || 320);
    for (const { marker, point } of this.markers) {
      marker.setAttribute("transform", `translate(${point.x} ${point.y}) scale(${scale})`);
    }
  }

  /** Борт: булавка с самолётом, остриё — на стоянке. */
  drawTarget(point) {
    this.addMarker(point, "route-target", [
      ["ellipse", { class: "marker-shadow", cx: 0, cy: 1, rx: 8, ry: 3 }],
      ["path", { class: "marker-body", d: "M0 0C-4 -8 -16 -15 -16 -28A16 16 0 0 1 16 -28C16 -15 4 -8 0 0Z" }],
      ["path", { class: "marker-icon", transform: "translate(-11 -39) scale(.92)", d: ICON_PLANE }],
    ]);
  }

  /** Машина, которую надо забрать. */
  drawVehicle(point) {
    this.addBadge(point, "route-vehicle", ICON_CAR);
  }

  drawWarehouse(point) {
    this.addBadge(point, "route-warehouse", ICON_WRENCH);
  }

  /** Круглый значок промежуточной точки. */
  addBadge(point, className, icon) {
    this.addMarker(point, className, [
      ["circle", { class: "marker-body", r: 14 }],
      ["path", { class: "marker-icon", transform: "translate(-9 -9) scale(.75)", d: icon }],
    ]);
  }

  setHeading(value) {
    this.heading = Number.isFinite(value) ? value : null;
    if (this.arrow) {
      this.arrow.style.display = this.heading === null ? "none" : "";
      this.arrow.setAttribute("transform", `rotate(${this.heading || 0})`);
    }
  }

  /** Инженер: точка с белым кольцом, расходящаяся волна и луч направления. */
  drawSelf(point) {
    const marker = this.addMarker(point, "route-self", [
      ["circle", { class: "self-pulse", r: 22 }],
      ["path", { class: "self-beam", d: "M0 0L-15 -34A37 37 0 0 1 15 -34Z" }],
      ["circle", { class: "self-ring", r: 11 }],
      ["circle", { class: "self-dot", r: 7.5 }],
    ]);
    // Метка пересоздаётся при каждой отрисовке — волна продолжается
    // с той же фазы, а не начинается заново.
    marker.querySelector(".self-pulse").style.animationDelay = `-${performance.now() % SELF_PULSE_MS}ms`;
    this.arrow = marker.querySelector(".self-beam");
    this.setHeading(this.heading);
  }

  /** Касание по карте — координаты для режима имитации. */
  handleTap(event) {
    if (!this.onTap || !this.view || this.suppressTap) {
      return;
    }
    const box = this.svg.getBoundingClientRect();
    if (!box.width || !box.height) {
      return;
    }
    // viewBox совпадает по пропорциям с областью на экране, поэтому
    // пиксели переводятся в метры простым отношением сторон.
    const x = this.view.minX + ((event.clientX - box.left) / box.width) * this.view.width;
    const y = this.view.minY + ((event.clientY - box.top) / box.height) * this.view.height;
    this.onTap(this.unproject(x, y));
  }
}

/* --- Web Mercator --- */

function lonToWorldX(lon, zoom) {
  return ((lon + 180) / 360) * TILE_SIZE * 2 ** zoom;
}

function latToWorldY(lat, zoom) {
  const sin = Math.sin((lat * Math.PI) / 180);
  return (0.5 - Math.log((1 + sin) / (1 - sin)) / (4 * Math.PI)) * TILE_SIZE * 2 ** zoom;
}

function groundResolution(lat, zoom) {
  return (
    (EARTH_CIRCUMFERENCE_M * Math.cos((lat * Math.PI) / 180)) / (TILE_SIZE * 2 ** zoom)
  );
}

/**
 * Номер тайла, в который попадает координата карты.
 *
 * Метры переводятся в пиксели мировой сетки Web Mercator относительно
 * опорной точки, затем делятся на размер тайла.
 */
function tileIndex(metres, resolution, worldOrigin) {
  return Math.floor((metres / resolution + worldOrigin) / TILE_SIZE);
}

function svgElement(name, attributes, text) {
  const element = document.createElementNS(SVG_NS, name);
  for (const [key, value] of Object.entries(attributes)) element.setAttribute(key, value);
  if (text !== undefined) element.textContent = text;
  return element;
}

function svgTitle(text) {
  return svgElement("title", {}, text);
}

function group(className) {
  const element = document.createElementNS(SVG_NS, "g");
  element.setAttribute("class", className);
  return element;
}

function midpoint(points) {
  const x = points.reduce((sum, p) => sum + p.x, 0) / (points.length || 1);
  const y = points.reduce((sum, p) => sum + p.y, 0) / (points.length || 1);
  return { x, y };
}

function distance(a, b) {
  return Math.hypot(a.x - b.x, a.y - b.y);
}
