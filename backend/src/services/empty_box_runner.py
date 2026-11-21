"""补测空区的后台求解：分块推进、检查点续跑与发布回查。

* 每次执行在领取时生成唯一 token，并记录领取时的版本号 revision。
* 检查点与最终结果的落盘都是带条件的单条 UPDATE：
  `WHERE id=? AND worker_token=? AND revision=?`，行数为 0 说明并发改动范围
  （revision 递增）已经作废旧答案，本次执行立即放弃，旧结果不会发布。
* 每批节点结束后先把前沿写成检查点再继续；进程中断后重新入队即可从检查点
  继续。检查点自带输入校验和，冻结样本或圈定范围被替换时拒绝恢复。
* 只有搜索前沿清空、且回查确认盒内严格内部无站后，才发布 completed。
"""
import logging
import secrets
from sqlalchemy import select, update
from ..config.database import Session
from ..config.settings import CONFIG
from ..models.run import Run
from ..models.empty_box import EmptyBoxSearch
from ..utils.serialization import now
from ..services import empty_box as solver
from ..services.audit import log
from ..services.empty_boxes import validate_frozen_records

CHUNK_NODES = int((CONFIG.get('empty_box') or {}).get('chunk_nodes') or 20000)


def claim(db, search_id):
    """领取一次执行；只有排队中的任务会被领取，重复消息不会打断正在运行的执行。"""
    s = db.scalar(select(EmptyBoxSearch).where(EmptyBoxSearch.id == search_id).with_for_update())
    if not s or s.state != 'queued':
        return None
    token = secrets.token_hex(16)
    s.worker_token = token
    s.state = 'running'
    s.updated_at = now()
    db.commit()
    return s.id, token, s.revision, s.ranges, s.run_id


def _save_checkpoint(db, sid, token, revision, checkpoint):
    result = db.execute(
        update(EmptyBoxSearch)
        .where(EmptyBoxSearch.id == sid,
               EmptyBoxSearch.worker_token == token,
               EmptyBoxSearch.revision == revision)
        .values(checkpoint=checkpoint, updated_at=now())
    )
    db.commit()
    return result.rowcount == 1


def _publish(db, sid, token, revision, result):
    return db.execute(
        update(EmptyBoxSearch)
        .where(EmptyBoxSearch.id == sid,
               EmptyBoxSearch.worker_token == token,
               EmptyBoxSearch.revision == revision)
        .values(result=result, state='completed', checkpoint=None, error='',
                worker_token='', completed_at=now(), updated_at=now())
    ).rowcount == 1


def _fail(db, sid, token, revision, message):
    result = db.execute(
        update(EmptyBoxSearch)
        .where(EmptyBoxSearch.id == sid,
               EmptyBoxSearch.worker_token == token,
               EmptyBoxSearch.revision == revision)
        .values(state='failed', error=message[:2000], worker_token='', updated_at=now())
    )
    db.commit()
    return result.rowcount == 1


def recover_orphans():
    """worker 启动时把上一个进程遗留的 running 搜索重新入队。

    BLPOP 取出消息后若进程中断，消息已经不在队列里；这里按检查点重新投递，
    已完成的搜索不会被复活。token+revision 条件写入保证旧进程即使仍在运行
    也无法覆盖新执行的结果。
    """
    from .empty_boxes import _enqueue
    with Session() as db:
        rows = db.execute(
            update(EmptyBoxSearch)
            .where(EmptyBoxSearch.state == 'running')
            .values(state='queued', worker_token='', updated_at=now())
            .returning(EmptyBoxSearch.id)
        ).scalars().all()
        db.commit()
    for search_id in rows:
        logging.info('empty box search %s requeued after worker restart', search_id)
        _enqueue(search_id)


def execute(search_id):
    with Session() as db:
        job = claim(db, search_id)
        if not job:
            return
        sid, token, revision, ranges, run_id = job
        try:
            run = db.get(Run, run_id)
            if not run or run.state != 'completed' or not run.result:
                raise ValueError('来源研究尚未完成，无法求解补测空区')
            records = run.snapshot.get('records') or []
            validate_frozen_records(records)
            points = [(r['values']['temperature'], r['values']['humidity'], r['values']['particles'])
                      for r in records]
            ctx = solver.prepare(points, [ranges[k] for k in solver.FEATURES])
            row = db.get(EmptyBoxSearch, sid)
            db.refresh(row)
            checkpoint = row.checkpoint
            state = solver.load_state(ctx, checkpoint) if checkpoint else solver.new_state(ctx)
            while True:
                done = solver.advance_state(ctx, state, CHUNK_NODES)
                if not done:
                    if not _save_checkpoint(db, sid, token, revision,
                                            solver.dump_checkpoint(ctx, state)):
                        logging.info('empty box search %s abandoned: superseded', sid)
                        return
                    continue
                if not solver.verify(ctx, state):
                    raise ValueError('回查发现盒内仍有站点，搜索结果未发布')
                result = solver.finalize(ctx, state)
                result['verified'] = True
                result['revision'] = revision
                result['ranges'] = {k: ranges[k] for k in solver.FEATURES}
                result['frozen_checksum'] = ctx['checksum']
                if not _publish(db, sid, token, revision, result):
                    logging.info('empty box search %s not published: superseded', sid)
                    return
                log(db, 'worker', 'empty_box.completed', sid,
                    '版本 ' + str(revision) + ' · 体积 ' + result['volume_exact'])
                db.commit()
                return
        except Exception as e:
            logging.exception('empty box search failed')
            if _fail(db, sid, token, revision, str(e)):
                log(db, 'worker', 'empty_box.failed', sid, str(e)[:2000])
                db.commit()
