FROM nginx:1.27-alpine

COPY ui/nginx.conf /etc/nginx/conf.d/default.conf
COPY ui/index.html ui/styles.css ui/app.js /usr/share/nginx/html/

EXPOSE 80

HEALTHCHECK --interval=10s --timeout=3s --retries=12 \
    CMD wget -q -O /dev/null http://localhost/ || exit 1
