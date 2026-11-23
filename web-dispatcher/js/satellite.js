/** Crop the matching quadrant of the parent imagery when a detailed tile is unavailable. */
function satelliteTile(template, level, column, row, onFailure) {
  const ns = "http://www.w3.org/2000/svg";
  const frame = document.createElementNS(ns, "svg");
  frame.setAttribute("overflow", "hidden");
  frame.setAttribute("preserveAspectRatio", "none");
  const image = document.createElementNS(ns, "image");
  image.setAttribute("width", 256);
  image.setAttribute("height", 256);
  frame.append(image);
  let zoom = level;
  function load() {
    const divisor = 2 ** (level - zoom);
    const side = 256 / divisor;
    frame.setAttribute("viewBox", [column % divisor * side, row % divisor * side, side, side].join(" "));
    const url = template.replace("{z}", zoom).replace("{x}", Math.floor(column / divisor))
      .replace("{y}", Math.floor(row / divisor));
    image.setAttribute("href", url + "?blankTile=false");
  }
  image.addEventListener("error", () => {
    if (!frame.isConnected) return;
    if (zoom > 8) { zoom--; load(); }
    else if (onFailure) onFailure();
  });
  image.addEventListener("load", () => frame.classList.add("loaded"));
  load();
  return frame;
}
