from dotenv import load_dotenv
load_dotenv()

from celery import Celery

celery = Celery(
    "spinnaker_web",
    broker="redis://localhost:6379/0",
    backend="redis://localhost:6379/0",
    include=["tasks"],
)

celery.conf.update(
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    timezone="UTC",
    worker_concurrency=1,        # SpiNNaker2: tek işlem aynı anda
    task_acks_late=True,         # Görev bitmeden ack gönderme
    worker_prefetch_multiplier=1, # Sıradaki işi önceden alma
)
