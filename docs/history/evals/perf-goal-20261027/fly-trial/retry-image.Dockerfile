FROM registry.fly.io/typeahead-startup-20261024@sha256:5de8836cae5efdaaf6b9c77316e682f06d64d944d439405a3d80dc6692d3e7e0
COPY cloud_trial.py fly_trial.py /app/perf/
COPY --chmod=0755 rclone-private.sh /app/perf/rclone
LABEL tech.waow.typeahead.perf_harness_revision=668023d0b0dfc4a9b263ebdd189dedf5b353e58f
