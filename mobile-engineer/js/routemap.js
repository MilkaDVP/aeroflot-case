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

const MIN_TILE_ZOOM = 12;
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

    this.svg.addEventListener("click", (event) => this.handleTap(event));
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
  render(routePoints, target, position, fallback) {
    const points = routePoints || [];
    const all = [...points];
    if (target) {
      all.push(target);
    }
    if (position) {
      all.push(position);
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

    this.reference = all[0];
    this.fit(all.map((point) => this.project(point)), points.length > 0, usingFallback);
    this.drawTiles();

    if (points.length >= 2) {
      this.drawRoute(points.map((point) => this.project(point)));
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

    this.view = {
      minX: centreX - width / 2,
      minY: centreY - height / 2,
      width,
      height,
      // Опорный размер для значков: они не должны зависеть от того,
      // насколько вытянут экран.
      scaleRef: Math.min(width, height),
    };

    this.svg.setAttribute("viewBox", `${this.view.minX} ${this.view.minY} ${width} ${height}`);
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
    this.tileLayer.replaceChildren();
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

    for (let cx = columns[0]; cx <= columns[1]; cx += 1) {
      for (let cy = rows[0]; cy <= rows[1]; cy += 1) {
        if (cx < 0 || cy < 0 || cx >= limit || cy >= limit) {
          continue;
        }
        const image = document.createElementNS(SVG_NS, "image");
        image.setAttribute(
          "href",
          ESRI_IMAGERY.replace("{z}", zoom).replace("{x}", cx).replace("{y}", cy)
        );
        image.setAttribute("x", (cx * TILE_SIZE - worldX0) * resolution);
        image.setAttribute("y", (cy * TILE_SIZE - worldY0) * resolution);
        image.setAttribute("width", tileSpanM + bleed);
        image.setAttribute("height", tileSpanM + bleed);
        image.setAttribute("preserveAspectRatio", "none");
        image.setAttribute("class", "map-tile");
        image.addEventListener("load", () => image.classList.add("loaded"));
        this.tileLayer.append(image);
      }
    }

    const scrim = document.createElementNS(SVG_NS, "rect");
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

  drawRoute(points) {
    const line = document.createElementNS(SVG_NS, "polyline");
    line.setAttribute("points", points.map((p) => `${p.x},${p.y}`).join(" "));
    line.setAttribute("class", "route-line");
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
    if (!this.onTap || !this.view) {
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
