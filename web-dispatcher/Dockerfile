# Образ веб-диспетчера: статика на nginx.
#
# Сборки нет и не нужно — чистые HTML, CSS и JS без фреймворков.
# Это осознанный выбор: жюри читает исходники так же, как их видит
# браузер, без промежуточного шага компиляции.
FROM nginx:alpine

COPY web-dispatcher/nginx.conf /etc/nginx/conf.d/default.conf
# Все страницы разом, а не по именам: перечисление уже приводило к тому,
# что новый экран отдавался как 404, хотя лежал в репозитории.
COPY web-dispatcher/*.html /usr/share/nginx/html/
COPY web-dispatcher/css/ /usr/share/nginx/html/css/
COPY web-dispatcher/js/ /usr/share/nginx/html/js/

EXPOSE 80
