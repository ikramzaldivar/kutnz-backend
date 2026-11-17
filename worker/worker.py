"""Consumidor de eventos de clic (SQS → DynamoDB).

Agrega por enlace: clics por día, país, dispositivo, navegador, sistema operativo, sitio de
origen y visitantes únicos por día. Los mensajes que fallan vuelven a la cola y, tras varios
intentos, terminan en la dead-letter queue configurada en Terraform.
"""
import json
import logging
import signal
from collections import Counter, defaultdict

from app import store
from app.clicks import _sqs
from app.config import get_settings

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s worker %(message)s")
log = logging.getLogger("kutnz.worker")
_running = True


def _stop(*_):
    global _running
    _running = False


def process(events: list[dict]) -> None:
    """Agrupa los contadores del lote para hacer menos escrituras a DynamoDB."""
    dims: Counter = Counter()
    clicks: Counter = Counter()
    visitors: dict = defaultdict(set)
    for e in events:
        code = e["code"]
        for dim in (f"day#{e['day']}", f"country#{e['country']}", f"device#{e['device']}",
                    f"browser#{e['browser']}", f"os#{e['os']}", f"referer#{e['referer']}"):
            dims[(code, dim)] += 1
        visitors[(code, e["day"])].add(e["visitor"])
        if not e.get("counted"):
            clicks[code] += 1
    for (code, dim), n in dims.items():
        store.add_stat(code, dim, n)
    for (code, day), vs in visitors.items():
        store.add_unique_visitors(code, day, vs)  # ADD sobre un conjunto: idempotente ante reintentos
    for code, n in clicks.items():
        store.add_clicks(code, n)


def run() -> None:
    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    queue = get_settings().clicks_queue_url
    sqs = _sqs()
    log.info("escuchando %s", queue)
    while _running:
        resp = sqs.receive_message(QueueUrl=queue, MaxNumberOfMessages=10, WaitTimeSeconds=20)
        msgs = resp.get("Messages", [])
        if not msgs:
            continue
        try:
            process([json.loads(m["Body"]) for m in msgs])
        except Exception:  # noqa: BLE001 — los mensajes regresan a la cola por el visibility timeout
            log.exception("error procesando lote de %d mensajes", len(msgs))
            continue
        sqs.delete_message_batch(QueueUrl=queue,
                                 Entries=[{"Id": str(i), "ReceiptHandle": m["ReceiptHandle"]} for i, m in enumerate(msgs)])


if __name__ == "__main__":
    run()
