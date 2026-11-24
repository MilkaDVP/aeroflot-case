/*
 * Оформление: стиль системы и тема человека.
 *
 * Стиль выбирает технический администратор для всех: «legacy» — прежний,
 * «aero» — Аэрофлот. В стиле Аэрофлота каждый выбирает себе тему:
 * «night» или «day». Итог — атрибут data-theme на <html>.
 *
 * Подключается в <head> ДО css: тема встаёт до первой отрисовки по
 * запомненному выбору, затем стиль сверяется с сервером.
 *
 * Файл одинаковый у диспетчера и у инженера.
 */
(function () {
  "use strict";

  const STYLE_KEY = "aq.style";
  const THEME_KEY = "aq.theme";
  const META_COLOR = { legacy: "#0b0f14", night: "#04091c", day: "#eef1f6" };
  const CHECK_MS = 30000;
  const SUN = '<circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M2 12h2M20 12h2M4.900 4.900l1.400 1.400M17.700 17.700l1.400 1.400M4.900 19.100l1.400-1.400M17.700 6.300l1.400-1.400"/>';
  const MOON = '<path d="M21 13A9 9 0 1 1 11 3a7 7 0 0 0 10 10z"/>';

  function recall(key, allowed, fallback) {
    try {
      const value = localStorage.getItem(key);
      return allowed.includes(value) ? value : fallback;
    } catch (error) {
      return fallback;
    }
  }

  function remember(key, value) {
    try {
      localStorage.setItem(key, value);
    } catch (error) {
      // Хранилище недоступно — выбор действует до закрытия страницы.
    }
  }

  let style = recall(STYLE_KEY, ["legacy", "aero"], "aero");
  let theme = recall(THEME_KEY, ["night", "day"], "night");

  function apply() {
    const shown = style === "legacy" ? "legacy" : theme;
    document.documentElement.dataset.theme = shown;
    document.querySelector('meta[name="theme-color"]')?.setAttribute("content", META_COLOR[shown]);
    for (const button of document.querySelectorAll(".theme-toggle")) paint(button);
    document.dispatchEvent(new CustomEvent("aq-theme", { detail: { style, theme: shown } }));
  }

  function paint(button) {
    const next = theme === "day" ? "Ночная тема" : "Дневная тема";
    // В прежнем стиле тема одна — переключать нечего.
    button.hidden = style === "legacy";
    button.title = next;
    button.setAttribute("aria-label", next);
    button.innerHTML = '<svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="2" ' +
      `stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${theme === "day" ? MOON : SUN}</svg>` +
      (button.dataset.label === undefined ? "" : `<span>${next}</span>`);
  }

  function setTheme(value) {
    theme = value === "day" ? "day" : "night";
    remember(THEME_KEY, theme);
    apply();
  }

  function setStyle(value) {
    if (!["legacy", "aero"].includes(value)) return;
    remember(STYLE_KEY, value);
    if (value === style) return;
    style = value;
    apply();
  }

  async function check() {
    if (document.hidden || typeof CONFIG === "undefined") return;
    try {
      const response = await fetch(`${CONFIG.apiBase}/api/ui`, { cache: "no-store" });
      if (response.ok) setStyle((await response.json()).style);
    } catch (error) {
      // Нет связи — действует запомненный стиль.
    }
  }

  apply();

  document.addEventListener("DOMContentLoaded", () => {
    if (!document.querySelector(".theme-toggle")) {
      // Страницы без шапки: кнопка в углу.
      const button = document.createElement("button");
      button.type = "button";
      button.className = "theme-toggle is-floating";
      document.body.append(button);
    }
    apply();
    check();
    setInterval(check, CHECK_MS);
  });
  document.addEventListener("visibilitychange", check);
  document.addEventListener("click", (event) => {
    if (event.target.closest?.(".theme-toggle")) setTheme(theme === "day" ? "night" : "day");
  });
  // Выбор в соседней вкладке.
  window.addEventListener("storage", (event) => {
    if (event.key !== THEME_KEY && event.key !== STYLE_KEY) return;
    style = recall(STYLE_KEY, ["legacy", "aero"], style);
    theme = recall(THEME_KEY, ["night", "day"], theme);
    apply();
  });

  window.AeroTheme = { get style() { return style; }, get theme() { return theme; }, setTheme, setStyle, check };
})();
