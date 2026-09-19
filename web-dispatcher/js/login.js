/*
 * Экран входа.
 *
 * После успешного входа сервер сообщает, привязан ли пользователь ровно
 * к одному аэропорту. Если да — шаг выбора пропускается, но смену выбрать
 * всё равно нужно: контекст смены определяет, кто вообще является
 * кандидатом на назначение.
 */

const form = document.getElementById("login-form");
const errorBox = document.getElementById("error");
const submitButton = document.getElementById("submit");

// Если действующая сессия уже есть, экран входа показывать незачем.
if (Session.token) {
  Api.me()
    .then((user) => {
      Session.user = user;
      window.location.href = user.airport_icao && user.shift
        ? "dispatch.html"
        : "select-airport.html";
    })
    .catch(() => Session.clear());
}

form.addEventListener("submit", async (event) => {
  event.preventDefault();
  showError(null);
  submitButton.disabled = true;
  submitButton.textContent = "Проверка…";

  try {
    const result = await Api.login(
      document.getElementById("login").value.trim(),
      document.getElementById("password").value
    );

    Session.token = result.token;
    Session.user = result;

    // Инженеру рабочее место диспетчера не предназначено — у него PWA.
    if (result.role === "engineer") {
      showError("Учётная запись инженера. Используйте мобильное приложение.");
      Session.clear();
      return;
    }

    window.location.href = "select-airport.html";
  } catch (error) {
    showError(error.detail || "Не удалось войти");
  } finally {
    submitButton.disabled = false;
    submitButton.textContent = "Войти";
  }
});

function showError(message) {
  errorBox.hidden = !message;
  errorBox.textContent = message || "";
}
