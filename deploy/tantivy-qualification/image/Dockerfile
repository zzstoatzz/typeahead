FROM python:3.12-slim@sha256:12316debbfb86890f6b2d825820612741ba527ad5a44e3902759141d572bfe8f
WORKDIR /app
COPY --chmod=0755 bin/ /app/bin/
COPY input/ /app/input/
COPY entrypoint.py /app/entrypoint.py
COPY pilot_bootstrap.py generator.py full_prepare.py source-manifest.json /app/
COPY full_bootstrap.py full_generator.py telemetry.py full_fixture.py identity-quarantine.json /app/
RUN ln -s bin/tantivy-raw-oracle /app/tantivy-raw-oracle
COPY build-metadata.json /app/build-metadata.json
ENV PYTHONUNBUFFERED=1
ENTRYPOINT ["python3", "/app/entrypoint.py"]
CMD ["serve"]
