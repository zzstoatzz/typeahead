set -u
format() {
 printf '{"region":"%s","phase":"%s","query":"%s","dns":%%{time_namelookup},"connect":%%{time_connect},"tls":%%{time_appconnect},"ttfb":%%{time_starttransfer},"total":%%{time_total},"status":%%{http_code},"remote_ip":"%%{remote_ip}","connections":%%{num_connects},"http_version":"%%{http_version}","headers":%%{header_json}}\n' "$FLY_REGION" "$1" "$2"
}
base=https://typeahead.waow.tech/xrpc/app.bsky.actor.searchActorsTypeahead
curl -sS --connect-timeout 3 --max-time 6 -o /dev/null https://typeahead.waow.tech/cdn-cgi/trace || true
for iteration in $(seq 1 12); do
 for q in a b jola.dev nate jo; do
  curl -sS --connect-timeout 3 --max-time 6 -A typeahead-regional-diagnostic/1.0 -H 'X-Typeahead-Probe: eu-extended-20261104' -H 'X-Client: bench' -o /dev/null -w "$(format extended-fresh "$q")\n" "$base?q=$q&limit=17" || true
  sleep 1.2
 done
done
