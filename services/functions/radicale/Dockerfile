# Your calendar server (Radicale, CalDAV). Not one of my functions: it registers no tools. Beacon reads and
# writes it over my stack's network; your iPhone reaches it through the proxy at /dav/, over Tailscale only.
FROM python:3.12-slim
RUN pip install --no-cache-dir radicale==3.8.1 bcrypt==5.0.0 \
 && useradd --system --uid 5232 --home /data radicale
COPY config /etc/radicale/config
COPY start.sh /start.sh
EXPOSE 5232
# Its events live in /data/collections (./data/radicale on the server: back it up by copying the folder).
# Its one password lives in /auth/users, which Beacon writes when you set it on the calendar form (empty, and
# so no one gets in, until then).
CMD ["/bin/sh", "/start.sh"]
