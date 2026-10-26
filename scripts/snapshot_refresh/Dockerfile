FROM registry.fly.io/typeahead-startup-20261024@sha256:9822d2b735cbfd5cda65b1bc4b8c6f95fcfd74e3d109c78e22b9a5b596247573
COPY typeahead-ingester admit-prefix /app/
COPY install.py /app/install-snapshot.py
ARG SOURCE_REVISION
LABEL org.opencontainers.image.revision=$SOURCE_REVISION
CMD ["python3", "/app/install-snapshot.py"]
