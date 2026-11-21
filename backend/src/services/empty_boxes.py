import math
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from fastapi import HTTPException
from redis import Redis
from ..config.settings import REDIS_URL
from ..models.empty_box import EmptyBoxSearch
from ..utils.serialization import row, now
from .runs import get_run
from .audit import log
from . import empty_box as solver
queue = Redis.from_url(REDIS_URL)
QUEUE = 'empty-box:queue'
FEATURE_LABELS = {'temperature': '温度', 'humidity': '相对湿度', 'particles': '颗粒浓度'}


def _public(s, checkpoint=None):
    data = row(s)
    checkpoint = checkpoint if checkpoint is not None else (s.checkpoint or {})
    nodes = int(checkpoint.get('nodes', 0))
    if not nodes and s.result:
        nodes = int(s.result.get('nodes', 0))
    data['progress'] = {'nodes': nodes,
                        'pending_boxes': len(checkpoint.get('stack') or [])}
    data.pop('checkpoint', None)
    data.pop('worker_token', None)
    if not (s.state == 'completed' and s.result and s.result.get('verified')):
        data['result'] = None
    return data


def validate_ranges(records, ranges):
    if not isinstance(ranges, dict):
        raise ValueError('读数范围需要是包含三项读数的对象')
    unknown = set(ranges) - set(solver.FEATURES)
    if unknown:
        raise ValueError('包含不支持的读数范围：' + ','.join(sorted(map(str, unknown))))
    out = {}
    for key in solver.FEATURES:
        if key not in ranges:
            raise ValueError('需要同时圈定温度、相对湿度与颗粒浓度的范围')
        pair = ranges[key]
        if not isinstance(pair, (list, tuple)) or len(pair) != 2:
            raise ValueError(FEATURE_LABELS[key] + '范围需要是 [下界, 上界]')
        values = []
        for v in pair:
            if isinstance(v, bool):
                raise ValueError(FEATURE_LABELS[key] + '范围必须为数值')
            try:
                v = float(v)
            except (TypeError, ValueError):
                raise ValueError(FEATURE_LABELS[key] + '范围必须为数值')
            if not math.isfinite(v):
                raise ValueError(FEATURE_LABELS[key] + '范围必须是有限数值')
            values.append(v)
        if not values[0] < values[1]:
            raise ValueError(FEATURE_LABELS[key] + '范围需要下界小于上界')
        out[key] = values
    for key in solver.FEATURES:
        observed = [float(r['values'][key]) for r in records]
        low, high = min(observed), max(observed)
        if not low < high:
            raise ValueError(FEATURE_LABELS[key] + '在冻结样本中为常量，无法在该维度搜索空区')
        tolerance = 1e-9 * max(1.0, abs(low), abs(high))
        if out[key][0] < low - tolerance or out[key][1] > high + tolerance:
            raise ValueError(FEATURE_LABELS[key] + '范围需落在冻结样本的 '
                             + format(low, 'g') + ' 至 ' + format(high, 'g') + ' 之间')
    return out


def _enqueue(search_id):
    try:
        queue.rpush(QUEUE, search_id)
    except Exception:
        return False
    return True


def get_search(db, run_id):
    get_run(db, run_id)
    s = db.scalar(select(EmptyBoxSearch).where(EmptyBoxSearch.run_id == run_id))
    return _public(s) if s else None


def _locked(db, run_id):
    return db.scalar(select(EmptyBoxSearch).where(EmptyBoxSearch.run_id == run_id).with_for_update())


def create_search(db, user, run_id, data):
    # 先锁住搜索行再读任务，保证与 worker 的领取/写入串行，避免读到旧修订。
    s = _locked(db, run_id)
    r = get_run(db, run_id)
    if r.state != 'completed' or not r.result:
        raise HTTPException(409, '仅已完成的研究可以圈定补测空区')
    records = r.snapshot.get('records') or []
    if not records:
        raise HTTPException(422, '冻结样本为空，无法求解空区')
    try:
        validate_frozen_records(records)
        ranges = validate_ranges(records, data.ranges)
    except ValueError as e:
        raise HTTPException(422, str(e))
    except (KeyError, TypeError):
        raise HTTPException(422, '冻结样本结构无法用于空区搜索')
    if s:
        # 版本号递增会让正在运行的旧搜索在落盘时被拒绝，旧答案不会发布。
        s.revision += 1
        s.ranges = ranges
        s.state = 'queued'
        s.checkpoint = None
        s.result = None
        s.error = ''
        s.worker_token = ''
        s.updated_at = now()
        s.completed_at = ''
        action = 'empty_box.ranges_updated'
    else:
        s = EmptyBoxSearch(run_id=run_id, state='queued', revision=1, ranges=ranges,
                           requested_by=user.email, created_at=now(), updated_at=now())
        db.add(s)
        action = 'empty_box.queued'
    try:
        db.flush()
    except IntegrityError:
        # 两个首次圈定并发到达时，唯一约束让后来者退化为修订更新。
        db.rollback()
        s = _locked(db, run_id)
        if not s:
            raise
        s.revision += 1
        s.ranges = ranges
        s.state = 'queued'
        s.checkpoint = None
        s.result = None
        s.error = ''
        s.worker_token = ''
        s.updated_at = now()
        s.completed_at = ''
        action = 'empty_box.ranges_updated'
        db.flush()
    log(db, user, action, s.id, '版本 ' + str(s.revision))
    db.commit()
    if not _enqueue(s.id):
        s.state = 'failed'
        s.error = '队列暂不可用，请稍后继续搜索'
        s.updated_at = now()
        db.commit()
        raise HTTPException(503, s.error)
    return _public(s)


def validate_frozen_records(records):
    for r in records:
        values = r.get('values') or {}
        for key in solver.FEATURES:
            if key not in values:
                raise ValueError('冻结样本缺少读数：' + FEATURE_LABELS[key])
            try:
                v = float(values[key])
            except (TypeError, ValueError):
                raise ValueError('冻结样本读数不是数值：' + FEATURE_LABELS[key])
            if not math.isfinite(v):
                raise ValueError('冻结样本读数不是有限数值：' + FEATURE_LABELS[key])
        if not 0 <= float(values['humidity']) <= 100 or float(values['particles']) < 0:
            raise ValueError('冻结样本读数超出业务范围：湿度需在0–100且颗粒浓度非负')
    return records


def resume_search(db, user, run_id):
    get_run(db, run_id)
    s = _locked(db, run_id)
    if not s:
        raise HTTPException(404, '尚未圈定补测空区')
    if s.state == 'completed':
        raise HTTPException(409, '本次搜索已完成；如需改变范围请重新圈定')
    s.state = 'queued'
    s.error = ''
    s.worker_token = ''
    s.updated_at = now()
    log(db, user, 'empty_box.resumed', s.id, '版本 ' + str(s.revision))
    db.commit()
    if not _enqueue(s.id):
        s.state = 'failed'
        s.error = '队列暂不可用，请稍后重试'
        s.updated_at = now()
        db.commit()
        raise HTTPException(503, s.error)
    return _public(s)
