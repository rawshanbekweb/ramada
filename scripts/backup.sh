#!/bin/sh
# Kunlik zaxira nusxa, 90 kun saqlanadi (TT 13-bo'lim).
set -e
while true; do
  now=$(date +%H%M)
  if [ "$now" = "0330" ]; then
    stamp=$(date +%Y%m%d)
    pg_dump -h db -U shaffof -Fc shaffof > /backups/shaffof_$stamp.dump
    tar czf /backups/storage_$stamp.tgz -C /srv storage 2>/dev/null || true
    find /backups -type f -mtime +90 -delete
    echo "zaxira tayyor: $stamp"
    sleep 61
  fi
  sleep 30
done
