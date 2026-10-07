#!/bin/sh
# The data folder is a bind mount owned by the host: give it to the radicale user, then run as that user
# (never as root).
set -e
mkdir -p /data/collections
chown -R radicale:radicale /data
# Its one login is set on Beacon's setup form. Until then an empty file: Radicale starts (so a release that
# brings it goes live) and lets no one in.
[ -e /auth/users ] || : > /auth/users
chmod 0644 /auth/users
exec setpriv --reuid=radicale --regid=radicale --init-groups python -m radicale --config /etc/radicale/config
