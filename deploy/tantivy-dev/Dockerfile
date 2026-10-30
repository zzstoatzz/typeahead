FROM python:3.12-slim@sha256:12316debbfb86890f6b2d825820612741ba527ad5a44e3902759141d572bfe8f
WORKDIR /app
COPY --chmod=0755 typeahead-tantivy /app/typeahead-tantivy
COPY input /app/input
COPY tests /app/tests
COPY reference /app/reference
COPY build-metadata.json /app/build-metadata.json
COPY bootstrap.py /app/bootstrap.py
COPY source-manifest.json /app/source-manifest.json
ENV PYTHONUNBUFFERED=1
ENV PYTHONPATH=/app/reference
ENV TA_IDENTITY_POLICY_TEST_PATH=/app/reference/identity-quarantine.json
EXPOSE 8080
CMD ["python3", "/app/bootstrap.py"]
