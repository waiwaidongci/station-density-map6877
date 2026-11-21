"""精确求三维点集内体积最大的轴对齐长方体空区。

算法：分支定界。以三项读数的圈定范围为根盒，若盒内严格包含某个站点，
就在该站的三个坐标处把盒切成六个子盒（任何只含空内部的盒都完整落在
其中一个子盒里），按体积从大到小深度优先展开；当前盒的体积就是该分支
的体积上界。因此不需要枚举六个边界的全部组合，也能得到精确最优解。

边界约定：
* 站点落在盒面上（任一坐标等于盒边界）不算占据盒内部，只统计严格内部；
* 噪声站在样本里没有特殊标记，所有冻结站点一律作为障碍点；
* 同体积时按六个边界值 (温度下界, 温度上界, 湿度下界, 湿度上界,
  颗粒下界, 颗粒上界) 依次比较取小。

坐标按十进制字面量缩放成整数，体积与同体积比较都是精确整数运算，避免
浮点误差改变边界选择。检查点只保存六个整数边界组成的待展开前沿，进程
中断后从该前沿继续搜索；同一前沿被重复展开只会让已知最优解更优，不会
漏掉候选。
"""
import hashlib
import json
from decimal import Decimal

FEATURES = ('temperature', 'humidity', 'particles')
MAX_PLACES = 400
CHECKPOINT_VERSION = 1


def _decimal_places(value):
    d = Decimal(str(float(value)))
    if not d.is_finite():
        raise ValueError('读数必须为有限数值')
    return max(0, -d.as_tuple().exponent)


def _scaled(value, scale):
    return int((Decimal(str(float(value))) * scale).to_integral_value())


def _volume(box):
    return (box[1] - box[0]) * (box[3] - box[2]) * (box[5] - box[4])


def _strictly_inside(point, box):
    return (box[0] < point[0] < box[1]
            and box[2] < point[1] < box[3]
            and box[4] < point[2] < box[5])


def prepare(points, ranges):
    """把原始读数与圈定范围转换成精确整数坐标。

    points: [(temperature, humidity, particles), ...]
    ranges: [(lo, hi), (lo, hi), (lo, hi)]
    """
    ranges = [tuple(r) for r in ranges]
    if len(ranges) != 3:
        raise ValueError('需要同时圈定三项读数范围')
    values = [v for p in points for v in p] + [v for r in ranges for v in r]
    places = max([_decimal_places(v) for v in values] or [0])
    if places > MAX_PLACES:
        raise ValueError('读数小数位超过 400 位，无法进行精确空区求解')
    scale = 10 ** places
    int_points = sorted({tuple(_scaled(v, scale) for v in p) for p in points})
    int_ranges = [(_scaled(lo, scale), _scaled(hi, scale)) for lo, hi in ranges]
    for (lo, hi) in int_ranges:
        if not lo < hi:
            raise ValueError('每项读数范围都需要下界小于上界')
    # 落在圈定范围面上或范围外的站点不可能是任何候选盒的严格内部点。
    active = [p for p in int_points
              if all(int_ranges[k][0] < p[k] < int_ranges[k][1] for k in range(3))]
    payload = {'version': CHECKPOINT_VERSION, 'scale': scale,
               'ranges': int_ranges, 'points': int_points}
    checksum = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(',', ':')).encode()
    ).hexdigest()
    return {'points': active, 'all_points': int_points, 'ranges': int_ranges,
            'scale': scale, 'checksum': checksum}


def _initial_best(ctx):
    """用沿单一维度的空隙板做初始下界，体积不会超过最优解。"""
    lo = [r[0] for r in ctx['ranges']]
    hi = [r[1] for r in ctx['ranges']]
    root = (lo[0], hi[0], lo[1], hi[1], lo[2], hi[2])
    best_box, best_volume = root, _volume(root) if not ctx['points'] else -1
    for axis in range(3):
        coords = sorted({p[axis] for p in ctx['points']})
        stops = [lo[axis], *coords, hi[axis]]
        for a, b in zip(stops, stops[1:]):
            if a == b:
                continue
            box = [lo[0], hi[0], lo[1], hi[1], lo[2], hi[2]]
            box[2 * axis] = a
            box[2 * axis + 1] = b
            box = tuple(box)
            volume = _volume(box)
            if volume > best_volume or (volume == best_volume and (best_box is None or box < best_box)):
                best_volume, best_box = volume, box
    return best_volume, best_box


def _inner_indices(points, box):
    return [i for i, p in enumerate(points) if _strictly_inside(p, box)]


def new_state(ctx):
    """构造可跨区块继续展开的内存状态。"""
    lo = [r[0] for r in ctx['ranges']]
    hi = [r[1] for r in ctx['ranges']]
    root = (lo[0], hi[0], lo[1], hi[1], lo[2], hi[2])
    best_volume, best_box = _initial_best(ctx)
    return {
        'stack': [(root, _inner_indices(ctx['points'], root))],
        'best_volume': int(best_volume),
        'best_box': best_box,
        'nodes': 0,
    }


def load_state(ctx, checkpoint):
    """从检查点恢复内存状态；校验版本与输入校验和。"""
    if checkpoint.get('version') != CHECKPOINT_VERSION:
        raise ValueError('搜索检查点版本不兼容，无法恢复')
    if checkpoint.get('checksum') != ctx['checksum']:
        raise ValueError('冻结样本或圈定范围与检查点不一致，无法恢复搜索')
    state = {
        'stack': [(tuple(int(v) for v in box), _inner_indices(ctx['points'], tuple(int(v) for v in box)))
                  for box in checkpoint['stack']],
        'best_volume': int(checkpoint['best_volume']),
        'best_box': tuple(int(v) for v in checkpoint['best_box']) if checkpoint.get('best_box') is not None else None,
        'nodes': int(checkpoint.get('nodes', 0)),
    }
    return state


def dump_checkpoint(ctx, state):
    return {
        'version': CHECKPOINT_VERSION,
        'checksum': ctx['checksum'],
        'scale': ctx['scale'],
        'best_volume': int(state['best_volume']),
        'best_box': list(state['best_box']) if state['best_box'] is not None else None,
        'stack': [list(box) for box, _ in state['stack']],
        'nodes': int(state['nodes']),
        'complete': not state['stack'],
    }


def start_checkpoint(ctx):
    return dump_checkpoint(ctx, new_state(ctx))


def _split_index(points, box, inner):
    """在盒内站点中取最靠近盒中心的点，保持确定性。"""
    cx, cy, cz = box[0] + box[1], box[2] + box[3], box[4] + box[5]
    best_index, best_distance = inner[0], None
    for index in inner:
        p = points[index]
        distance = (2 * p[0] - cx) ** 2 + (2 * p[1] - cy) ** 2 + (2 * p[2] - cz) ** 2
        if best_distance is None or distance < best_distance:
            best_index, best_distance = index, distance
    return best_index


def advance_state(ctx, state, max_nodes=None):
    """展开至多 max_nodes 个节点；返回前沿是否已清空。"""
    points = ctx['points']
    stack = state['stack']
    best_volume, best_box = state['best_volume'], state['best_box']
    budget = None if max_nodes is None else max(0, int(max_nodes))
    processed = 0
    while stack and (budget is None or processed < budget):
        box, inner = stack.pop()
        processed += 1
        state['nodes'] += 1
        volume = _volume(box)
        if best_box is not None and volume < best_volume:
            continue
        if not inner:
            if best_box is None or volume > best_volume or (volume == best_volume and box < best_box):
                best_volume, best_box = volume, box
            continue
        if volume == best_volume:
            # 子盒体积严格更小，不可能再得到同体积空盒。
            continue
        px, py, pz = points[_split_index(points, box, inner)]
        children = []
        for child in (
            (box[0], px, box[2], box[3], box[4], box[5]),
            (px, box[1], box[2], box[3], box[4], box[5]),
            (box[0], box[1], box[2], py, box[4], box[5]),
            (box[0], box[1], py, box[3], box[4], box[5]),
            (box[0], box[1], box[2], box[3], box[4], pz),
            (box[0], box[1], box[2], box[3], pz, box[5]),
        ):
            child_volume = _volume(child)
            if child_volume < best_volume:
                continue
            child_inner = [i for i in inner if _strictly_inside(points[i], child)]
            children.append((child_volume, child, child_inner))
        # 体积大的先展开（栈后进先出），更快收紧上界。
        children.sort(key=lambda item: (item[0], item[1]))
        stack.extend((child, child_inner) for _, child, child_inner in children)
    state['best_volume'], state['best_box'] = best_volume, best_box
    return not stack


def advance(ctx, checkpoint, max_nodes=None):
    """先恢复检查点、再展开的便捷封装，返回 (新检查点, 是否完成)。"""
    state = load_state(ctx, checkpoint)
    done = advance_state(ctx, state, max_nodes)
    return dump_checkpoint(ctx, state), done


def _decimal_text(integer, scale):
    value = Decimal(integer) / Decimal(scale)
    text = format(value.normalize(), 'f')
    if '.' in text:
        text = text.rstrip('0').rstrip('.')
    return text or '0'


def finalize(ctx, state):
    """把整数盒转换成可直接展示的边界与精确体积。"""
    box = state.get('best_box')
    if box is None:
        raise ValueError('搜索没有产生候选空区')
    box = tuple(int(v) for v in box)
    scale = ctx['scale']
    volume = _volume(box)
    bounds = [_decimal_text(v, scale) for v in box]
    spans = [_decimal_text(box[2 * k + 1] - box[2 * k], scale) for k in range(3)]
    return {
        'box': {FEATURES[k]: [float(Decimal(bounds[2 * k])), float(Decimal(bounds[2 * k + 1]))]
                for k in range(3)},
        'bounds': bounds,
        'spans': spans,
        'volume': float(Decimal(volume) / (Decimal(scale) ** 3)),
        'volume_exact': _decimal_text(volume, scale ** 3),
        'volume_units': '℃·%·μg/m³',
        'nodes': int(state['nodes']),
        'points_checked': len(ctx['all_points']),
    }


def verify(ctx, state):
    """回查盒内严格内部无站；不通过时返回 False。"""
    box = state.get('best_box')
    if box is None:
        return False
    box = tuple(int(v) for v in box)
    return not any(_strictly_inside(p, box) for p in ctx['all_points'])
