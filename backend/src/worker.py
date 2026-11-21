import json
import logging
import time
from redis import Redis
from .config.database import Session
from .config.settings import REDIS_URL, ARTIFACT_DIR
from .models.run import Run
from .models import source, record, user, note, audit, empty_box
from .services.algorithm import calculate
from .services.audit import log
from .services.export import csv_response
from .services import empty_box_runner
from .services.empty_boxes import QUEUE as EMPTY_BOX_QUEUE
queue = Redis.from_url(REDIS_URL)
ARTIFACT_DIR.mkdir(parents=True, exist_ok=True)


def execute(id):
    with Session() as db:
        r = db.get(Run, id)
        if not r or r.state != 'queued':
            return
        r.state = 'running'
        db.commit()
        try:
            result = calculate(r.snapshot['records'], r.parameters)
            json.dumps(result, allow_nan=False)
            r.result = result
            r.state = 'completed'
            response = csv_response(result, 'run.csv')
            (ARTIFACT_DIR / f'run-{r.id}.csv').write_bytes(response.body)
            log(db, 'worker', 'run.completed', r.id)
        except Exception as e:
            logging.exception('analysis failed')
            r.state = 'failed'
            r.error = str(e)[:2000]
            log(db, 'worker', 'run.failed', r.id, r.error)
        db.commit()


def main():
    try:
        empty_box_runner.recover_orphans()
    except Exception:
        logging.exception('empty box recovery failed')
    while True:
        (ARTIFACT_DIR / 'heartbeat').write_text(str(time.time()))
        value = queue.blpop(['analysis:queue', EMPTY_BOX_QUEUE], timeout=2)
        if not value:
            continue
        if value[0].decode() == EMPTY_BOX_QUEUE:
            empty_box_runner.execute(int(value[1]))
        else:
            execute(int(value[1]))


if __name__ == '__main__':
    main()
