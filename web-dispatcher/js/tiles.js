/*
 * Растровая подложка карты: спутниковый снимок и дорожная сеть.
 *
 * Зачем. Схематичный граф из OSM показывает связность рулёжек, но не даёт
 * картины местности: диспетчер не видит здания терминалов, перроны,
 * фактическое покрытие. Снимок эту картину возвращает, а граф поверх него
 * остаётся тем, по чему считается маршрут.
 *
 * Про проекции — главное место этого модуля.
 * Граф приходит в метрах от опорной точки аэропорта (равнопромежуточная
 * проекция), тайлы отдаются в Web Mercator. В общем случае это разные
 * системы, но вблизи одной широты они отличаются равномерным множителем
 * 1/cos(широты) по обеим осям, то есть подобны. На размерах аэропорта
 * (7 x 3 км) накопленная ошибка около полуметра, поэтому снимок можно
 * положить прямо в метровую систему графа без перепроецирования.
 *
 * Подложка требует интернета. Это осознанное ограничение: схема остаётся
 * режимом по умолчанию и работает офлайн, а если тайлы не загрузились,
 * система об этом честно сообщает, а не показывает пустоту.
 */

const TILE_SIZE = 256;
const EARTH_CIRCUMFERENCE_M = 40075016.686;

// Меньше 10 — бессмысленно (аэропорт в один тайл), больше 19 — источники
// уже не отдают снимок такой детализации.
const MIN_TILE_ZOOM = 10;
const MAX_TILE_ZOOM = 19;

// Предохранитель от лавины запросов при неверном расчёте масштаба.
const MAX_TILES_PER_UPDATE = 240;

const ESRI_IMAGERY =
  "https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}";

// Подложек две. Слой дорожной сети поверх снимка на перроне бесполезен:
// он подписывает городские улицы, а рулёжки и стоянки диспетчер видит
// на самом снимке и в графе.
const BASEMAPS = {
  scheme: {
    title: "Схема",
    layers: [],
    credit: "Граф: OpenStreetMap (ODbL)",
  },
  satellite: {
    title: "Спутник",
    layers: [ESRI_IMAGERY],
    credit: "Снимок: Esri, Maxar, Earthstar Geographics · Граф: OpenStreetMap",
  },
};

/* --- Web Mercator --- */

function lonToWorldX(lon, zoom) {
  return ((lon + 180) / 360) * TILE_SIZE * 2 ** zoom;
}

function latToWorldY(lat, zoom) {
  const sin = Math.sin((lat * Math.PI) / 180);
  return (0.5 - Math.log((1 + sin) / (1 - sin)) / (4 * Math.PI)) * TILE_SIZE * 2 ** zoom;
}

/** Метров на один пиксель тайла — на широте аэропорта. */
function groundResolution(lat, zoom) {
  return (
    (EARTH_CIRCUMFERENCE_M * Math.cos((lat * Math.PI) / 180)) /
    (TILE_SIZE * 2 ** zoom)
  );
}

class TileLayer {
  /**
   * @param {AirportMap} map карта, чью систему координат используем
   * @param {SVGGElement} group слой под графом, куда кладутся снимки
   * @param {Function} onStatus сообщает наружу о недоступности подложки
   */
  constructor(map, group, onStatus) {
    this.map = map;
    this.group = group;
    this.onStatus = onStatus || (() => {});
    this.basemap = "scheme";
    // Уже добавленные тайлы: ключ «слой/зум/столбец/строка».
    this.tiles = new Map();
    this.failures = 0;
    this.requested = 0;
  }

  get credit() {
    return BASEMAPS[this.basemap].credit;
  }

  /** Переключает подложку. Схема очищает слой и ничего не грузит. */
  setBasemap(key) {
    if (!BASEMAPS[key]) {
      return;
    }
    this.basemap = key;
    this.clear();
    this.failures = 0;
    this.requested = 0;
    this.refresh();
  }

  clear() {
    this.group.replaceChildren();
    this.tiles.clear();
  }

  /**
   * Подбирает уровень детализации под текущий масштаб.
   *
   * Цель — чтобы пиксель тайла примерно совпал с пикселем экрана: более
   * крупный уровень даёт мыло, более мелкий — лишний трафик без выигрыша.
   */
  tileZoom() {
    const pxPerMetre = this.map.basePxPerMetre * this.map.transform.scale;
    const lat = this.map.graph.ref_point.lat;
    const exact = Math.log2(
      ((EARTH_CIRCUMFERENCE_M * Math.cos((lat * Math.PI) / 180)) / TILE_SIZE) * pxPerMetre
    );
    return Math.max(MIN_TILE_ZOOM, Math.min(MAX_TILE_ZOOM, Math.round(exact)));
  }

  /** Видимая область в координатах содержимого (метры от опорной точки). */
  visibleContentRect() {
    const rect = this.map.visibleRect();
    const { x, y, scale } = this.map.transform;
    return {
      minX: (rect.minX - x) / scale,
      minY: (rect.minY - y) / scale,
      maxX: (rect.minX + rect.width - x) / scale,
      maxY: (rect.minY + rect.height - y) / scale,
    };
  }

  /** Догружает тайлы, попавшие в кадр, и убирает уехавшие далеко за него. */
  refresh() {
    if (!this.map.graph || !BASEMAPS[this.basemap].layers.length) {
      return;
    }

    const zoom = this.tileZoom();
    const reference = this.map.graph.ref_point;
    const resolution = groundResolution(reference.lat, zoom);
    const worldX0 = lonToWorldX(reference.lon, zoom);
    const worldY0 = latToWorldY(reference.lat, zoom);
    const tileSpanM = TILE_SIZE * resolution;

    const view = this.visibleContentRect();
    const firstColumn = Math.floor((view.minX / resolution + worldX0) / TILE_SIZE);
    const lastColumn = Math.floor((view.maxX / resolution + worldX0) / TILE_SIZE);
    const firstRow = Math.floor((view.minY / resolution + worldY0) / TILE_SIZE);
    const lastRow = Math.floor((view.maxY / resolution + worldY0) / TILE_SIZE);

    const limit = 2 ** zoom;
    const needed = new Set();
    let added = 0;

    for (const template of BASEMAPS[this.basemap].layers) {
      for (let column = firstColumn; column <= lastColumn; column += 1) {
        for (let row = firstRow; row <= lastRow; row += 1) {
          if (column < 0 || row < 0 || column >= limit || row >= limit) {
            continue;
          }
          const key = `${template}|${zoom}|${column}|${row}`;
          needed.add(key);

          if (this.tiles.has(key) || added >= MAX_TILES_PER_UPDATE) {
            continue;
          }
          this.tiles.set(
            key,
            this.createTile(template, zoom, column, row, {
              x: (column * TILE_SIZE - worldX0) * resolution,
              y: (row * TILE_SIZE - worldY0) * resolution,
              size: tileSpanM,
            })
          );
          added += 1;
        }
      }
    }

    this.dropOffscreen(needed, zoom);
  }

  createTile(template, zoom, column, row, box) {
    const image = document.createElementNS("http://www.w3.org/2000/svg", "image");
    const url = template
      .replace("{z}", zoom)
      .replace("{x}", column)
      .replace("{y}", row);

    image.setAttribute("href", url);
    image.setAttribute("x", box.x);
    image.setAttribute("y", box.y);
    // Доля процента перекрытия: без неё между тайлами видны светлые
    // швы от округления при растеризации.
    image.setAttribute("width", box.size * 1.004);
    image.setAttribute("height", box.size * 1.004);
    // Соседние тайлы должны стыковаться без щелей и без растягивания.
    image.setAttribute("preserveAspectRatio", "none");
    image.setAttribute("class", "basemap-tile");

    this.requested += 1;
    image.addEventListener("error", () => this.registerFailure());
    image.addEventListener("load", () => image.classList.add("loaded"));

    this.group.append(image);
    return image;
  }

  /**
   * Считает неудачные загрузки.
   *
   * Один-два сбоя — обычное дело на краю покрытия. Если же не грузится
   * почти ничего, значит нет интернета, и об этом надо сказать прямо:
   * иначе диспетчер решит, что сломалась карта.
   */
  registerFailure() {
    this.failures += 1;
    if (this.requested >= 4 && this.failures >= this.requested / 2) {
      this.onStatus("Подложка недоступна: нет доступа в интернет");
    }
  }

  /** Убирает тайлы другого уровня детализации и давно уехавшие из кадра. */
  dropOffscreen(needed, zoom) {
    for (const [key, image] of this.tiles) {
      const tileZoom = Number(key.split("|")[1]);
      if (tileZoom !== zoom || !needed.has(key)) {
        image.remove();
        this.tiles.delete(key);
      }
    }
  }
}
