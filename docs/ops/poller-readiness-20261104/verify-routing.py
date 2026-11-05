import json
import time
from pathlib import Path

import boto3
from botocore.exceptions import ClientError
from prefect.blocks.system import Secret

def secret(name):
    return Secret.load(name).get()

client = boto3.client("s3", endpoint_url=secret("index-r2-endpoint"), region_name="auto",
    aws_access_key_id=secret("index-r2-access-key-id"),
    aws_secret_access_key=secret("index-r2-secret-access-key"))
bucket = secret("index-r2-bucket")
response = client.get_object(Bucket=bucket, Key="control/search-routing.json")
with response["Body"] as stream:
    route = json.loads(stream.read(65536))
assert route["current"]["origin"] == "https://typeahead-tantivy-b.fly.dev"
assert route["current"]["admission_sha256"] == "8216daf108e7dbc4ae6bbe24ca14b056620f5bb88211dd245416a8ea7905dc03"
assert route["previous"]["origin"] == "https://typeahead-tantivy-a.fly.dev"
assert not route.get("mutation")
clear = []
for app in ["typeahead-tantivy-a", "typeahead-tantivy-b"]:
    try:
        client.head_object(Bucket=bucket, Key=f"control/tantivy-slots/{app}/pending.json")
    except ClientError as error:
        assert error.response["ResponseMetadata"]["HTTPStatusCode"] == 404
        clear.append(app)
    else:
        raise AssertionError("Pending slot operation still fences refresh")
result = dict(at=time.time(), route=route, etag=response["ETag"], pending_slots_clear=clear)
Path(__file__).with_name("routing-after.json").write_text(json.dumps(result, indent=2)+"\n")
print(json.dumps({"current":route["current"]["origin"],"previous":route["previous"]["origin"],"pending_slots_clear":clear}))
