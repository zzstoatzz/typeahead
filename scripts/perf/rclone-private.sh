#!/bin/sh
exec /usr/local/bin/rclone --ignore-checksum "$@"
