set -u
format() {
 printf '{"region":"%s","phase":"%s","query":"%s","dns":%%{time_namelookup},"connect":%%{time_connect},"tls":%%{time_appconnect},"ttfb":%%{time_starttransfer},"total":%%{time_total},"status":%%{http_code},"remote_ip":"%%{remote_ip}","connections":%%{num_connects},"http_version":"%%{http_version}","headers":%%{header_json}}\n' "$FLY_REGION" "$1" "$2"
}
base=https://typeahead.waow.tech/xrpc/app.bsky.actor.searchActorsTypeahead
curl -sS --connect-timeout 3 --max-time 6 -o /dev/null -w "$(format dns-warmup trace)\n" https://typeahead.waow.tech/cdn-cgi/trace || true
for q in a b jo jola jola.dev nate zzstoatzz.io; do
 for phase in fresh-first fresh-repeat; do
  curl -sS --connect-timeout 3 --max-time 6 -A typeahead-regional-diagnostic/1.0 -H 'X-Typeahead-Probe: eu-instrumented-20261104' -H 'X-Client: bench' -o /dev/null -w "$(format "$phase" "$q")\n" "$base?q=$q&limit=13" || true
  sleep 1
 done
done
for q in a b jola.dev; do
 curl -sS --connect-timeout 3 --max-time 6 -A typeahead-regional-diagnostic/1.0 -H 'X-Typeahead-Probe: eu-instrumented-20261104' -H 'X-Client: bench' -o /dev/null -w "$(format reused-first "$q")\n" "$base?q=$q&limit=13" --next -sS --connect-timeout 3 --max-time 6 -A typeahead-regional-diagnostic/1.0 -H 'X-Typeahead-Probe: eu-instrumented-20261104' -H 'X-Client: bench' -o /dev/null -w "$(format reused-second "$q")\n" "$base?q=$q&limit=13" || true
 sleep 1
done
for q in johanna jolan jola.de; do
 curl -sS --connect-timeout 3 --max-time 6 -A typeahead-regional-diagnostic/1.0 -H 'X-Typeahead-Probe: eu-instrumented-20261104' -o /dev/null -w "$(format direct-engine "$q")\n" "https://typeahead-tantivy-b.fly.dev/search?q=$q&limit=13" || true
 sleep 1
done

for q in a b jola.dev; do
 curl -sS --connect-timeout 3 --max-time 6 -A typeahead-regional-diagnostic/1.0 -H 'X-Typeahead-Probe: eu-instrumented-20261104' -o /dev/null -w "$(format bluesky "$q")\n" "https://public.api.bsky.app/xrpc/app.bsky.actor.searchActorsTypeahead?q=$q&limit=13" || true
 sleep 1
done
