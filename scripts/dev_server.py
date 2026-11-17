"""Servidor de desarrollo sin Docker ni AWS: DynamoDB/SQS simulados (moto) y Redis en memoria.

    .venv/Scripts/python scripts/dev_server.py     →  http://localhost:8000
Inicia sesión con cualquier nombre (modo dev); "nombre:admin" para administrador.
El worker de clics corre en un hilo para que las métricas se vean en vivo.
"""
import os
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.update({
    "AWS_ACCESS_KEY_ID": "dev", "AWS_SECRET_ACCESS_KEY": "dev", "AWS_DEFAULT_REGION": "us-east-1",
    "KUT_AUTH_MODE": "dev", "KUT_PUBLIC_HOST": "localhost:8000", "KUT_PUBLIC_SCHEME": "http",
    "KUT_FRAME_ANCESTORS": "'self'",
})

import boto3  # noqa: E402
import fakeredis  # noqa: E402
import uvicorn  # noqa: E402
from moto import mock_aws  # noqa: E402

from tests.tables import create_tables  # noqa: E402

mock = mock_aws()
mock.start()
create_tables(boto3.client("dynamodb", region_name="us-east-1"))
os.environ["KUT_CLICKS_QUEUE_URL"] = boto3.client("sqs", region_name="us-east-1").create_queue(QueueName="clicks")["QueueUrl"]

from app import cache  # noqa: E402

_fake = fakeredis.FakeRedis(decode_responses=True)
cache.client = lambda: _fake

from app.main import app  # noqa: E402
from worker import worker  # noqa: E402


def worker_loop():
    import json
    sqs = boto3.client("sqs", region_name="us-east-1")
    q = os.environ["KUT_CLICKS_QUEUE_URL"]
    while True:
        msgs = sqs.receive_message(QueueUrl=q, MaxNumberOfMessages=10).get("Messages", [])
        if msgs:
            worker.process([json.loads(m["Body"]) for m in msgs])
            for m in msgs:
                sqs.delete_message(QueueUrl=q, ReceiptHandle=m["ReceiptHandle"])
        time.sleep(1)


threading.Thread(target=worker_loop, daemon=True).start()
uvicorn.run(app, host="127.0.0.1", port=8000, log_level="info")
