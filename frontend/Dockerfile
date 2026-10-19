FROM nginx:1.27-alpine

# Rendered by the nginx image's entrypoint (envsubst) into /etc/nginx/conf.d/default.conf
COPY nginx/default.conf.template /etc/nginx/templates/default.conf.template
COPY public/ /usr/share/nginx/html/

# Port nginx listens on, where /api/* is forwarded, and which backend
# frontend (camera set and options) this container serves
ENV LISTEN_PORT=8010 \
    API_UPSTREAM=http://api:8011 \
    FRONTEND_NAME=main

EXPOSE 8010
