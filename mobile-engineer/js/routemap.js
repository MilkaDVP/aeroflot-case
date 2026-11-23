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

// Масштаб, который инженер задаёт сам (щипок, колесо, кнопки): во сколько
// раз ближе автоматического вида. Пределы — чтобы не «улететь» в одну
// стоянку или в целый город.
const USER_ZOOM_MIN = 0.4;
const USER_ZOOM_MAX = 12;
// Смещение пальца, после которого касание — уже сдвиг карты, а не «я здесь».
const TAP_SLOP_PX = 8;

const ESRI_IMAGERY =
  "https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}";

class RouteMap {
  constructor(svgElement, onTap) {
    this.svg = svgElement;
    this.onTap = onTap;
    this.reference = null;
    this.view = null;
    this.showImagery = true;

    this.tileLayer = group("layer-tiles");
    this.contentLayer = group("layer-content");
    this.svg.append(this.tileLayer, this.contentLayer);

    // Свой вид инженера переживает обновления данных: карта больше не
    // возвращается к автоматическому охвату на каждом опросе.
    this.userZoom = 1;
    this.userPan = { x: 0, y: 0 };
    this.base = null;
    this.lastArgs = null;
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
      this.pointers.delete(event.pointerId);
      if (this.pointers.size === 0) {
        if (this.gesture && this.gesture.moved) {
          // Касание, которым закончился сдвиг, — не «я здесь».
          this.suppressTap = true;
          setTimeout(() => { this.suppressTap = false; }, 0);
          this.rerender();
        }
        this.gesture = null;
      } else {
        this.startGesture();
      }
    };
    svg.addEventListener("pointerup", end);
    svg.addEventListener("pointercancel", end);
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
      pan: { x: this.userPan.x, y: this.userPan.y },
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
    // Сдвиг одним пальцем: от положения в начале жеста.
    this.userPan = { x: gesture.pan.x, y: gesture.pan.y };
    this.applyView();
    this.panBy(dx, dy);
  }

  /** Сдвиг вида на экранные пиксели (карта едет за пальцем). */
  panBy(dx, dy) {
    const box = this.svg.getBoundingClientRect();
    const mpp = this.view.width / (box.width || 1);
    this.userPan.x -= dx * mpp;
    this.userPan.y -= dy * mpp;
    this.applyView();
  }

  /** Масштаб с неподвижной точкой под пальцем или курсором. */
  zoomAt(factor, clientX, clientY) {
    if (!this.view || !this.base) return;
    const next = Math.min(USER_ZOOM_MAX, Math.max(USER_ZOOM_MIN, this.userZoom * factor));
    const box = this.svg.getBoundingClientRect();
    const fx = (clientX - box.left) / (box.width || 1);
    const fy = (clientY - box.top) / (box.height || 1);
    // Точка под пальцем в метрах — после масштаба она должна остаться там же.
    const px = this.view.minX + fx * this.view.width;
    const py = this.view.minY + fy * this.view.height;
    this.userZoom = next;
    this.applyView();
    this.userPan.x += px - (this.view.minX + fx * this.view.width);
    this.userPan.y += py - (this.view.minY + fy * this.view.height);
    this.applyView();
  }

  /** Кнопки «+» и «−»: масштаб от центра карты. */
  zoomBy(factor) {
    const box = this.svg.getBoundingClientRect();
    this.zoomAt(factor, box.left + box.width / 2, box.top + box.height / 2);
    this.rerender();
  }

  /** «◎» — обратно к автоматическому виду: маршрут и своя точка целиком. */
  resetView() {
    this.userZoom = 1;
    this.userPan = { x: 0, y: 0 };
    this.rerender();
  }

  isUserView() {
    return this.userZoom !== 1 || this.userPan.x !== 0 || this.userPan.y !== 0;
  }

  /** Автоматический охват плюс масштаб и сдвиг инженера. */
  applyView() {
    if (!this.base) return;
    const width = this.base.width / this.userZoom;
    const height = this.base.height / this.userZoom;
    const cx = this.base.cx + this.userPan.x;
    const cy = this.base.cy + this.userPan.y;
    this.view = {
      minX: cx - width / 2,
      minY: cy - height / 2,
      width,
      height,
      scaleRef: Math.min(width, height),
    };
    this.svg.setAttribute("viewBox", `${this.view.minX} ${this.view.minY} ${width} ${height}`);
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

  setImagery(enabled) {
    this.showImagery = enabled;
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
  render(routePoints, target, position, fallback, vehicle, pickupNodeId, centre) {
    this.lastArgs = [routePoints, target, position, fallback, vehicle, pickupNodeId, centre];
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
    const usingFallback = all.length === 0 && Boolean(fallback);
    if (usingFallback) {
      all.push(fallback);
    }

    this.contentLayer.replaceChildren();
    if (all.length === 0) {
      // Совсем не от чего оттолкнуться: ни вызова, ни координат,
      // ни аэропорта. Пустая карта честнее случайной точки на глобусе.
      this.tileLayer.replaceChildren();
      return;
    }

    this.reference = centre || all[0];
    const extent = centre ? [centre] : all;
    this.fit(extent.map((point) => this.project(point)), !centre && points.length > 0, centre || usingFallback);
    this.drawTiles();

    if (points.length >= 2) {
      this.drawLegs(points, pickupNodeId);
    }
    if (pickupVehicle) {
      this.drawVehicle(this.project(pickupVehicle));
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
  fit(points, hasRoute, usingFallback) {
    const box = this.svg.getBoundingClientRect();
    const boxWidth = box.width || 320;
    const boxHeight = box.height || 320;

    const xs = points.map((point) => point.x);
    const ys = points.map((point) => point.y);

    const extent = Math.max(
      Math.max(...xs) - Math.min(...xs),
      Math.max(...ys) - Math.min(...ys)
    );
    const minimum = hasRoute
      ? MIN_ROUTE_SPAN_M
      : usingFallback
        ? FALLBACK_SPAN_M
        : IDLE_SPAN_M;
    const span = Math.max(extent, minimum) * (1 + PADDING_RATIO * 2);

    // Заданный охват укладывается в меньшую сторону, большая получает
    // пропорционально больше местности.
    const aspect = boxWidth / boxHeight;
    const width = aspect >= 1 ? span * aspect : span;
    const height = aspect >= 1 ? span : span / aspect;

    const centreX = (Math.max(...xs) + Math.min(...xs)) / 2;
    const centreY = (Math.max(...ys) + Math.min(...ys)) / 2;

    // Автоматический охват; поверх него — масштаб и сдвиг инженера.
    // Опорный размер значков (scaleRef) берётся от итогового вида, чтобы
    // они не зависели ни от масштаба, ни от того, насколько вытянут экран.
    this.base = { cx: centreX, cy: centreY, width, height };
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

  drawTarget(point) {
    const marker = document.createElementNS(SVG_NS, "circle");
    marker.setAttribute("cx", point.x);
    marker.setAttribute("cy", point.y);
    marker.setAttribute("r", this.view.scaleRef * 0.035);
    marker.setAttribute("class", "route-target");
    this.contentLayer.append(marker);
  }

  /** Машина, которую надо забрать: квадрат, как на карте диспетчера. */
  drawVehicle(point) {
    const size = this.view.scaleRef * 0.05;
    const marker = document.createElementNS(SVG_NS, "rect");
    marker.setAttribute("x", point.x - size / 2);
    marker.setAttribute("y", point.y - size / 2);
    marker.setAttribute("width", size);
    marker.setAttribute("height", size);
    marker.setAttribute("rx", size * 0.2);
    marker.setAttribute("class", "route-vehicle");
    this.contentLayer.append(marker);
  }

  drawSelf(point) {
    const halo = document.createElementNS(SVG_NS, "circle");
    halo.setAttribute("cx", point.x);
    halo.setAttribute("cy", point.y);
    halo.setAttribute("r", this.view.scaleRef * 0.055);
    halo.setAttribute("class", "route-self-halo");

    const marker = document.createElementNS(SVG_NS, "circle");
    marker.setAttribute("cx", point.x);
    marker.setAttribute("cy", point.y);
    marker.setAttribute("r", this.view.scaleRef * 0.028);
    marker.setAttribute("class", "route-self");

    this.contentLayer.append(halo, marker);
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
