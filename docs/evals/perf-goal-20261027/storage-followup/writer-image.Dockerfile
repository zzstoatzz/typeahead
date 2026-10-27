FROM registry.fly.io/typeahead-startup-20261024@sha256:ce3e939708e59c3f39ccf05fb463ef5b95c3dfec6f99edec7d5867421ef49d9e
COPY baseline candidate cloud_trial.py fly_trial.py frozen_trial.py qualify.py /app/perf/
LABEL tech.waow.typeahead.perf_candidate="2ffd938"
