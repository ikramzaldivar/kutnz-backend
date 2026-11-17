import os

os.environ.update({
    "AWS_ACCESS_KEY_ID": "test", "AWS_SECRET_ACCESS_KEY": "test", "AWS_DEFAULT_REGION": "us-east-1",
    "KUT_AUTH_MODE": "dev", "KUT_PUBLIC_HOST": "kut.nz", "KUT_EXTRA_SELF_HOSTS": "d111.cloudfront.net",
})

import boto3  # noqa: E402
import fakeredis  # noqa: E402
import pytest  # noqa: E402
from moto import mock_aws  # noqa: E402

from tests.tables import create_tables  # noqa: E402


@pytest.fixture()
def env(monkeypatch):
    with mock_aws():
        create_tables(boto3.client("dynamodb", region_name="us-east-1"))
        queue = boto3.client("sqs", region_name="us-east-1").create_queue(QueueName="clicks")["QueueUrl"]
        monkeypatch.setenv("KUT_CLICKS_QUEUE_URL", queue)

        from app import cache, clicks, config, store
        config.get_settings.cache_clear()
        store._ddb.cache_clear()
        clicks._sqs.cache_clear()
        fake = fakeredis.FakeRedis(decode_responses=True)
        monkeypatch.setattr(cache, "client", lambda: fake)
        monkeypatch.setattr(cache, "_open_until", 0.0)

        import importlib

        from app import main
        importlib.reload(main)
        from fastapi.testclient import TestClient
        yield TestClient(main.app, follow_redirects=False), queue


def auth(user="ana", admin=False):
    return {"Authorization": f"Bearer dev:{user}{':admin' if admin else ''}"}
