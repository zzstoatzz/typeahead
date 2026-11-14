set -u
format() {
 printf '{"region":"%s","phase":"%s","query":"%s","dns":%%{time_namelookup},"connect":%%{time_connect},"tls":%%{time_appconnect},"ttfb":%%{time_starttransfer},"total":%%{time_total},"status":%%{http_code},"remote_ip":"%%{remote_ip}","connections":%%{num_connects},"http_version":"%%{http_version}","headers":%%{header_json}}\n' "$FLY_REGION" "$1" "$2"
}
base=https://typeahead.waow.tech/xrpc/app.bsky.actor.searchActorsTypeahead
curl -sS --connect-timeout 3 --max-time 6 -o /dev/null https://typeahead.waow.tech/cdn-cgi/trace || true
for iteration in $(seq 1 40); do
 printf '\nTRACE_REQUEST_%s\n' "$iteration"
 curl -v --trace-time --connect-timeout 3 --max-time 6 -A typeahead-regional-diagnostic/1.0 -H 'X-Typeahead-Probe: eu-trace-20261104' -H 'X-Client: bench' -o /dev/null -w "$(format timestamped-fresh jola.dev)\n" "$base?q=jola.dev&limit=19" || true
 sleep 1.2
done
