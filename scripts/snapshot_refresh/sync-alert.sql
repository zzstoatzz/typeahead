WITH serving AS (
    SELECT service_name, max(start_timestamp) AS latest_probe
    FROM records
    WHERE span_name = 'http.health'
      AND attributes->>'production_probe' = 'true'
      AND service_name IN ('typeahead-startup-20261024', 'typeahead-search-refresh')
    GROUP BY service_name
    ORDER BY latest_probe DESC
    LIMIT 1
)
SELECT count(sync.span_id) AS sync_spans
FROM serving
LEFT JOIN records AS sync
  ON sync.service_name = serving.service_name
 AND sync.span_name = 'sync.incremental'
 AND sync.level < 17
 AND sync.attributes->>'skipped' IS NULL
HAVING count(sync.span_id) = 0
