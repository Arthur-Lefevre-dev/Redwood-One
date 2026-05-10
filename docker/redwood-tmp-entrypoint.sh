#!/bin/sh
set -e
# Named volume tmp_data:/tmp/redwood shadows the image tree; it is often root-owned on first mount.
# API and worker run as user redwood (uid 1000) and must write uploads/torrents here.
mkdir -p /tmp/redwood/uploads /tmp/redwood/torrents
chown -R redwood:redwood /tmp/redwood
exec gosu redwood "$@"
