# Образ PWA инженера: та же статика на nginx, что и веб-диспетчер.
#
# Отдельный сервис, а не папка внутри диспетчера: приложения
# разворачиваются независимо, у них разная аудитория и разный цикл
# обновления. Инженеру нельзя выкатывать правки диспетчерского экрана.
FROM nginx:alpine

COPY mobile-engineer/nginx.conf /etc/nginx/conf.d/default.conf
COPY mobile-engineer/index.html mobile-engineer/manifest.json \
     mobile-engineer/sw.js /usr/share/nginx/html/
COPY mobile-engineer/css/ /usr/share/nginx/html/css/
COPY mobile-engineer/js/ /usr/share/nginx/html/js/
COPY mobile-engineer/icons/ /usr/share/nginx/html/icons/

EXPOSE 80
