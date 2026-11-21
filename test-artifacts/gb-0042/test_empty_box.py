"""补测空区求解与接口的自动化测试。

运行方式（在 backend 目录）：
    DATABASE_URL=sqlite:////tmp/empty-box-test.db ARTIFACT_DIR=/tmp/empty-box-artifacts \
    REDIS_FAKE=1 python -m pytest ../test-artifacts/gb-0042/test_empty_box.py -q

测试覆盖：分支定界对穷举 oracle 的精确等价、盒面不占据语义、同体积字典序、
检查点中断续跑、重复投递幂等、并发改动范围后旧答案不得发布、权限与参数校验。
"""
import itertools
import json
import os
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[2] / 'backend'
sys.path.insert(0, str(BACKEND))
os.environ.setdefault('DATABASE_URL', 'sqlite:////tmp/empty-box-test.db')
os.environ.setdefault('ARTIFACT_DIR', '/tmp/empty-box-artifacts')
try:
    import fakeredis
    import redis
    redis.Redis.from_url = lambda *a, **k: fakeredis.FakeRedis()
except ImportError:  # pragma: no cover - 仅测试环境缺失时跳过
    pass

import pytest

from src.services import empty_box as solver


def brute(points, ranges):
    axes = []
    for k in range(3):
        lo, hi = ranges[k]
        axes.append(sorted({lo, hi, *[p[k] for p in points if lo <= p[k] <= hi]}))
    best = None
    for x1, x2 in itertools.combinations(axes[0], 2):
        for y1, y2 in itertools.combinations(axes[1], 2):
            for z1, z2 in itertools.combinations(axes[2], 2):
                box = (x1, x2, y1, y2, z1, z2)
                if any(box[0] < p[0] < box[1] and box[2] < p[1] < box[3] and box[4] < p[2] < box[5]
                       for p in points):
                    continue
                volume = (x2 - x1) * (y2 - y1) * (z2 - z1)
                key = (-volume, box)
                if best is None or key < best[0]:
                    best = (key, box, volume)
    return best


@pytest.mark.parametrize('seed', range(12))
def test_matches_bruteforce(seed):
    import random
    rng = random.Random(seed)
    points = [(rng.choice([0, 1, 2, 3]), rng.choice([0, 1, 2, 3]), rng.choice([0, 1, 2, 3]))
              for _ in range(rng.randint(1, 7))]
    ranges = [(0, 3), (0, 3), (0, 3)]
    ctx = solver.prepare(points, ranges)
    state = solver.new_state(ctx)
    solver.advance_state(ctx, state)
    result = solver.finalize(ctx, state)
    expected = brute(points, ranges)
    assert tuple(float(v) for v in result['bounds']) == expected[1]
    assert result['volume'] == pytest.approx(expected[2])
    assert solver.verify(ctx, state)


def test_face_points_do_not_occupy():
    points = [(0, 0, 0), (5, 5, 5)]
    ctx = solver.prepare(points, [(0, 5), (0, 5), (0, 5)])
    state = solver.new_state(ctx)
    solver.advance_state(ctx, state)
    assert solver.finalize(ctx, state)['bounds'] == ['0', '5', '0', '5', '0', '5']


def test_equal_volume_tie_break_is_lexicographic():
    # 两个候选空区体积相同，应取六个边界值字典序更小者。
    points = [(2, 2, 2)]
    ctx = solver.prepare(points, [(0, 4), (0, 4), (0, 4)])
    state = solver.new_state(ctx)
    solver.advance_state(ctx, state)
    result = solver.finalize(ctx, state)
    box = tuple(float(v) for v in result['bounds'])
    assert box == brute([(2, 2, 2)], [(0, 4), (0, 4), (0, 4)])[1]


def test_resume_from_checkpoint_equals_uninterrupted():
    import random
    rng = random.Random(5)
    points = [(rng.randint(0, 20), rng.randint(0, 20), rng.randint(0, 20)) for _ in range(120)]
    ranges = [(0, 20)] * 3
    ctx = solver.prepare(points, ranges)

    straight = solver.new_state(ctx)
    solver.advance_state(ctx, straight)

    checkpoint = solver.start_checkpoint(solver.prepare(points, ranges))
    ctx2 = solver.prepare(points, ranges)
    while True:
        checkpoint, done = solver.advance(ctx2, checkpoint, max_nodes=7)
        checkpoint = json.loads(json.dumps(checkpoint))
        if done:
            break
    assert solver.finalize(ctx, straight)['bounds'] == solver.finalize(ctx2, checkpoint)['bounds']
    assert solver.verify(ctx2, checkpoint)


def test_checkpoint_rejects_changed_inputs():
    ctx = solver.prepare([(0, 0, 0), (1, 1, 1)], [(0, 2)] * 3)
    checkpoint = solver.start_checkpoint(ctx)
    changed = solver.prepare([(0, 0, 0), (1, 1, 1), (1.5, 1.5, 1.5)], [(0, 2)] * 3)
    with pytest.raises(ValueError):
        solver.load_state(changed, checkpoint)


# ---------------------------------------------------------------- 接口与并发流程

import importlib


@pytest.fixture(scope='module')
def app_env():
    import tempfile
    db_path = Path(tempfile.mkdtemp(prefix='empty-box-')) / 'app.db'
    os.environ['DATABASE_URL'] = 'sqlite:///' + str(db_path)
    import src.config.database as database_module
    from src.models import audit, empty_box, note, record, run, source, user  # noqa: F401
    database_module.Base.metadata.create_all(database_module.engine)
    import src.main as main_module
    from src.services import empty_box_runner
    from fastapi.testclient import TestClient
    return database_module, main_module, empty_box_runner, TestClient(main_module.app)


SOURCE_COUNTER = {'n': 0}


def _seed(database_module, n=800, seed=42):
    import random
    from src.models.user import User
    from src.models.source import Source
    from src.models.record import Record
    from src.models.run import Run
    from src.utils.security import hash_password
    from src.utils.serialization import now
    SOURCE_COUNTER['n'] += 1
    tag = 'HL-T-%02d' % SOURCE_COUNTER['n']
    rng = random.Random(seed)
    with database_module.Session() as db:
        for role in ('admin', 'analyst', 'viewer'):
            email = role + '@station-density.demo'
            if not db.query(User).filter_by(email=email).first():
                db.add(User(email=email, password_hash=hash_password('Research2026!'), role=role))
        src = Source(key=tag, name='测试来源 ' + tag, filename=tag + '.csv', description='')
        db.add(src)
        db.flush()
        for i in range(n):
            cls = i % 3
            if i % 19 == 0:
                values = {'temperature': round(rng.uniform(12, 42), 2),
                          'humidity': round(rng.uniform(12, 92), 2),
                          'particles': round(rng.uniform(4, 140), 2)}
            else:
                values = {'temperature': round(rng.gauss([19, 26, 32][cls], .8), 2),
                          'humidity': round(rng.gauss([72, 45, 28][cls], 2.5), 2),
                          'particles': round(max(.1, rng.gauss([22, 48, 85][cls], 3)), 2)}
            db.add(Record(source_id=src.id, reference=tag + '-%04d' % (i + 1), values=values))
        db.flush()
        records = [{'id': r.id, 'source_id': r.source_id, 'reference': r.reference, 'values': r.values}
                   for r in db.query(Record).filter_by(source_id=src.id).all()]
        run = Run(source_id=src.id, name='已完成研究 ' + tag, state='completed',
                  parameters={'eps': .55, 'min_samples': 5},
                  snapshot={'source': {'id': src.id, 'name': src.name, 'filename': src.filename},
                            'records': records},
                  created_at=now(), result={'summary': []})
        db.add(run)
        db.commit()
        return run.id, records


def _auth(client, email):
    token = client.post('/auth/login', json={'email': email, 'password': 'Research2026!'}).json()['token']
    return {'Authorization': 'Bearer ' + token}


def _extent_ranges(records):
    return {k: [min(r['values'][k] for r in records), max(r['values'][k] for r in records)]
            for k in solver.FEATURES}


def test_api_flow_and_permissions(app_env):
    database_module, main_module, runner_module, TestClient = app_env
    run_id, records = _seed(database_module)
    ranges = _extent_ranges(records)
    client = TestClient
    analyst = _auth(client, 'analyst@station-density.demo')
    viewer = _auth(client, 'viewer@station-density.demo')

    assert client.get('/runs/%d/empty-box' % run_id, headers=analyst).json()['search'] is None
    assert client.post('/runs/%d/empty-box' % run_id, headers=viewer, json={'ranges': ranges}).status_code == 403
    outside = {**ranges, 'temperature': [ranges['temperature'][0] - 1, ranges['temperature'][1]]}
    assert client.post('/runs/%d/empty-box' % run_id, headers=analyst, json={'ranges': outside}).status_code == 422
    bad_order = {**ranges, 'humidity': [ranges['humidity'][1], ranges['humidity'][0]]}
    assert client.post('/runs/%d/empty-box' % run_id, headers=analyst, json={'ranges': bad_order}).status_code == 422
    missing = {'temperature': ranges['temperature'], 'humidity': ranges['humidity']}
    assert client.post('/runs/%d/empty-box' % run_id, headers=analyst, json={'ranges': missing}).status_code == 422

    search = client.post('/runs/%d/empty-box' % run_id, headers=analyst, json={'ranges': ranges}).json()['search']
    assert search['state'] == 'queued' and search['revision'] == 1 and search['result'] is None
    assert client.get('/runs/%d/empty-box' % run_id, headers=viewer).status_code == 200

    runner_module.CHUNK_NODES = 100
    runner_module.execute(search['id'])
    published = client.get('/runs/%d/empty-box' % run_id, headers=analyst).json()['search']
    assert published['state'] == 'completed'
    assert published['result']['verified'] is True
    assert published['result']['volume_exact']
    assert published['result']['revision'] == 1

    bounds = [float(v) for v in published['result']['bounds']]
    for r in records:
        p = [r['values'][k] for k in solver.FEATURES]
        assert not (bounds[0] < p[0] < bounds[1] and bounds[2] < p[1] < bounds[3] and bounds[4] < p[2] < bounds[5])

    assert client.post('/runs/%d/empty-box/resume' % run_id, headers=analyst).status_code == 409


def test_stale_worker_cannot_publish_after_range_change(app_env):
    database_module, main_module, runner_module, TestClient = app_env
    run_id, records = _seed(database_module, seed=7)
    ranges = _extent_ranges(records)
    lo, hi = ranges['particles']
    client = TestClient
    analyst = _auth(client, 'analyst@station-density.demo')
    search_id = client.post('/runs/%d/empty-box' % run_id, headers=analyst,
                            json={'ranges': ranges}).json()['search']['id']

    with database_module.Session() as db:
        sid, token, revision, claimed_ranges, _ = runner_module.claim(db, search_id)
    changed = client.post('/runs/%d/empty-box' % run_id, headers=analyst,
                          json={'ranges': {**ranges, 'particles': [lo, hi - 1]}}
                          ).json()['search']
    assert changed['revision'] == revision + 1

    with database_module.Session() as db:
        assert runner_module._save_checkpoint(db, sid, token, revision, {'stack': [], 'nodes': 1}) is False
        assert runner_module._publish(db, sid, token, revision, {'volume_exact': '999', 'verified': True}) is False
        assert runner_module._fail(db, sid, token, revision, 'stale') is False

    after = client.get('/runs/%d/empty-box' % run_id, headers=analyst).json()['search']
    assert after['state'] == 'queued' and after['result'] is None and after['revision'] == revision + 1

    runner_module.CHUNK_NODES = 50
    runner_module.execute(search_id)
    # 队列消息按 search id 投递，重复投递只会推进当前修订，不会复活旧答案
    final = client.get('/runs/%d/empty-box' % run_id, headers=analyst).json()['search']
    assert final['state'] == 'completed' and final['revision'] == revision + 1
    assert final['result']['ranges']['particles'] == [lo, hi - 1]


def test_crash_resume_and_idempotent_retry(app_env):
    database_module, main_module, runner_module, TestClient = app_env
    run_id, records = _seed(database_module, seed=11)
    ranges = _extent_ranges(records)
    client = TestClient
    analyst = _auth(client, 'analyst@station-density.demo')
    search_id = client.post('/runs/%d/empty-box' % run_id, headers=analyst,
                            json={'ranges': ranges}).json()['search']['id']

    original = runner_module.solver.advance_state
    calls = {'n': 0}

    def crash_after_three(ctx, state, max_nodes=None):
        calls['n'] += 1
        if calls['n'] > 3:
            raise KeyboardInterrupt('simulated crash')
        return original(ctx, state, max_nodes)

    runner_module.CHUNK_NODES = 150
    runner_module.solver.advance_state = crash_after_three
    try:
        with pytest.raises(KeyboardInterrupt):
            runner_module.execute(search_id)
    finally:
        runner_module.solver.advance_state = original

    from src.models.empty_box import EmptyBoxSearch
    with database_module.Session() as db:
        row = db.get(EmptyBoxSearch, search_id)
        assert row.state == 'running' and row.worker_token
        assert row.checkpoint and row.checkpoint['stack']
        pending = len(row.checkpoint['stack'])

    resumed = client.post('/runs/%d/empty-box/resume' % run_id, headers=analyst)
    assert resumed.status_code == 200
    assert resumed.json()['search']['state'] == 'queued'
    assert resumed.json()['search']['progress']['pending_boxes'] == pending

    runner_module.CHUNK_NODES = 20000
    runner_module.execute(search_id)
    final = client.get('/runs/%d/empty-box' % run_id, headers=analyst).json()['search']
    assert final['state'] == 'completed'

    ctx = solver.prepare([(r['values']['temperature'], r['values']['humidity'], r['values']['particles'])
                          for r in records], [ranges[k] for k in solver.FEATURES])
    state = solver.new_state(ctx)
    solver.advance_state(ctx, state)
    reference = solver.finalize(ctx, state)
    assert final['result']['bounds'] == reference['bounds']
    assert final['result']['volume_exact'] == reference['volume_exact']

    runner_module.execute(search_id)
    again = client.get('/runs/%d/empty-box' % run_id, headers=analyst).json()['search']
    assert again['state'] == 'completed' and again['result']['bounds'] == final['result']['bounds']


def test_worker_restart_requeues_orphaned_running_search(app_env):
    database_module, main_module, runner_module, TestClient = app_env
    run_id, records = _seed(database_module, seed=23)
    ranges = _extent_ranges(records)
    client = TestClient
    analyst = _auth(client, 'analyst@station-density.demo')
    search_id = client.post('/runs/%d/empty-box' % run_id, headers=analyst,
                            json={'ranges': ranges}).json()['search']['id']

    original = runner_module.solver.advance_state
    calls = {'n': 0}

    def crash_immediately(ctx, state, max_nodes=None):
        calls['n'] += 1
        if calls['n'] > 1:
            raise KeyboardInterrupt('simulated worker death')
        return original(ctx, state, max_nodes)

    runner_module.CHUNK_NODES = 100
    runner_module.solver.advance_state = crash_immediately
    try:
        with pytest.raises(KeyboardInterrupt):
            runner_module.execute(search_id)
    finally:
        runner_module.solver.advance_state = original

    from src.models.empty_box import EmptyBoxSearch
    with database_module.Session() as db:
        assert db.get(EmptyBoxSearch, search_id).state == 'running'

    # 重启 worker：遗留 running 搜索重新入队，不复活已完成搜索
    runner_module.recover_orphans()
    with database_module.Session() as db:
        row = db.get(EmptyBoxSearch, search_id)
        assert row.state == 'queued' and not row.worker_token

    runner_module.CHUNK_NODES = 20000
    runner_module.execute(search_id)
    final = client.get('/runs/%d/empty-box' % run_id, headers=analyst).json()['search']
    assert final['state'] == 'completed'

    ctx = solver.prepare([(r['values']['temperature'], r['values']['humidity'], r['values']['particles'])
                          for r in records], [ranges[k] for k in solver.FEATURES])
    state = solver.new_state(ctx)
    solver.advance_state(ctx, state)
    assert final['result']['bounds'] == solver.finalize(ctx, state)['bounds']
