import {domain} from '../constants/domain.js';
import {esc} from '../utils/format.js';
import {emptyBoxApi} from '../api/research.js';
import {toast} from '../hooks/actions.js';

const LABELS = {temperature: '温度 ℃', humidity: '相对湿度 %', particles: '颗粒浓度 μg/m³'};
const STATES = {queued: '排队中', running: '搜索中', completed: '已发布', failed: '搜索失败'};

function statusTag(state) {
  const kind = state === 'completed' ? 'completed' : state === 'failed' ? 'failed' : 'running';
  return '<span class="status ' + kind + '">' + esc(STATES[state] || state) + '</span>';
}

function extentsOf(run) {
  const extents = {};
  for (const [key] of domain.fields) {
    const values = (run.snapshot.records || []).map(record => Number(record.values[key])).filter(Number.isFinite);
    extents[key] = values.length ? [Math.min(...values), Math.max(...values)] : [0, 0];
  }
  return extents;
}

function numberText(value) {
  return value == null ? '' : esc(String(Number(value)));
}

function rangeForm(run, search, writable) {
  if (!writable) return '<p class="fine">圈定范围由分析师或负责人提交。</p>';
  const extents = extentsOf(run);
  const rows = domain.fields.map(([key, label]) => {
    const [low, high] = (search && search.ranges && search.ranges[key]) || extents[key];
    const hint = '冻结值 ' + numberText(extents[key][0]) + ' – ' + numberText(extents[key][1]);
    return '<label>' + esc(label) + ' 范围 <small>' + hint + '</small>'
      + '<span class="range-pair"><input type="number" step="any" name="' + key + '_min" aria-label="' + esc(label) + ' 下界" value="' + numberText(low) + '" required>'
      + '<em>至</em><input type="number" step="any" name="' + key + '_max" aria-label="' + esc(label) + ' 上界" value="' + numberText(high) + '" required></span></label>';
  }).join('');
  return '<form id="empty-box-form" class="empty-box-form">'
    + '<p class="fine">三项范围的交集即搜索区域，须落在冻结样本的最小值与最大值之间。系统对盒内站点的补集做精确分支定界，不逐一枚举六个边界组合。</p>'
    + rows
    + '<button type="submit">' + (search ? '按新范围重新搜索 →' : '开始搜索补测空区 →') + '</button></form>';
}

function pendingBlock(search, writable) {
  return '<div class="pending"><span class="spinner"></span><h2>独立 worker 正在搜索空区</h2>'
    + '<p>已展开 ' + search.progress.nodes + ' 个节点，前沿还剩 ' + search.progress.pending_boxes + ' 个盒子；中断后可从这里继续。</p>'
    + (writable ? '<button id="empty-box-resume" class="secondary">继续搜索</button>' : '') + '</div>';
}

function resultBlock(result) {
  const rows = domain.fields.map(([key, label], index) => {
    return {feature: LABELS[key] || label,
            low: result.bounds[2 * index], high: result.bounds[2 * index + 1],
            span: (result.spans && result.spans[index]) || String(Number(result.bounds[2 * index + 1]) - Number(result.bounds[2 * index]))};
  });
  const body = rows.map(row => '<tr><td>' + esc(row.feature) + '</td><td>' + esc(row.low)
    + '</td><td>' + esc(row.high) + '</td><td>' + esc(row.span) + '</td></tr>').join('');
  return '<div class="published-box"><h2>已公布的最大空区 <small>完整搜索并回查盒内无站</small></h2>'
    + '<div class="table-wrap"><table><thead><tr><th>读数</th><th>下界</th><th>上界</th><th>跨度</th></tr></thead><tbody>' + body + '</tbody></table></div>'
    + '<p class="fine">体积 ' + esc(result.volume_exact) + ' ' + esc(result.volume_units)
    + ' · 探索节点 ' + result.nodes + ' · 回查 ' + result.points_checked + ' 个冻结站点，盒内严格内部无站。'
    + ' 冻结输入校验和 <code>' + esc(result.frozen_checksum) + '</code></p>'
    + '<p class="fine">下一轮补测可安排在这个组合的中心附近，先确认现场温湿度与颗粒浓度的可达性。</p></div>';
}

function statusBlock(run, search, writable) {
  if (!search) return '<p class="empty">尚未圈定补测空区。</p>';
  if (search.state === 'completed') return '';
  if (search.state === 'completed') return '<p class="empty">结果未通过回查，未公布。</p>';
  if (search.state === 'failed') {
    return '<div class="failure"><h2>空区搜索未完成</h2><p>' + esc(search.error || '未知错误') + '</p>'
      + (writable ? '<button id="empty-box-resume">从检查点继续搜索</button>' : '') + '</div>';
  }
  return pendingBlock(search, writable);
}

export function emptyBoxSection(run, search, writable) {
  if (!run || run.state !== 'completed' || !run.result) return '';
  const config = domain.empty_box || {};
  return '<section class="empty-box"><div class="run-title"><h2>' + esc(config.label || '补测空区') + '</h2>'
    + (search ? statusTag(search.state) + '<small>修订 ' + search.revision + '</small>' : '') + '</div>'
    + '<p class="fine">' + esc(config.description || '') + '</p>'
    + '<div class="split"><section>' + rangeForm(run, search, writable) + statusBlock(run, search, writable) + '</section>'
    + '<aside class="method-sheet"><p class="eyebrow">CLEARANCE RULES</p>'
    + '<p>' + esc(config.surface_rule || '') + '</p><p>' + esc(config.tie_rule || '') + '</p>'
    + '<p><b>噪声站照常计入</b>；空区只是下一轮补测的采样候选，不代表污染或热点判断。</p></aside></div>'
    + (search && search.state === 'completed' && search.result ? resultBlock(search.result) : '') + '</section>';
}

export function bindEmptyBox(run, search, refresh) {
  const form = document.querySelector('#empty-box-form');
  if (form) form.addEventListener('submit', async event => {
    event.preventDefault();
    const button = event.target.querySelector('button[type="submit"]');
    const data = new FormData(event.target);
    const ranges = {};
    for (const [key] of domain.fields) ranges[key] = [Number(data.get(key + '_min')), Number(data.get(key + '_max'))];
    if (button) button.disabled = true;
    try {
      await emptyBoxApi.save(run.id, ranges);
      await refresh();
      toast('已按新范围重新搜索；旧答案不会发布');
    } catch (error) {
      toast(error.message, true);
    } finally {
      if (button) button.disabled = false;
    }
  });
  const resume = document.querySelector('#empty-box-resume');
  if (resume) resume.addEventListener('click', async () => {
    resume.disabled = true;
    try {
      await emptyBoxApi.resume(run.id);
      await refresh();
      toast('已从检查点继续搜索');
    } catch (error) {
      toast(error.message, true);
      resume.disabled = false;
    }
  });
}
