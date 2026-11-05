FROM nginx:1.27-alpine

COPY ui/nginx.conf /etc/nginx/conf.d/default.conf
COPY ui/index.html ui/styles.css ui/app.js /usr/share/nginx/html/

EXPOSE 80

# Use 127.0.0.1, not localhost: inside the container localhost resolves to
# ::1, while nginx listens on IPv4 only.
HEALTHCHECK --interval=10s --timeout=3s --retries=12 \
    CMD wget -q -O /dev/null http://127.0.0.1/ || exit 1
