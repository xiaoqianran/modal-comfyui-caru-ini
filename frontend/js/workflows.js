import { api } from './api.js';

// Region suffix of the Modal web URL. A URL parsed from a deploy log
// (server.py LINKS) takes precedence over this composed one.
const REGION = 'us-east';
const MAX_LABEL = 63;

let all = [], apps = {}, links = {}, workspace = '';
let selected = '', notify = () => {}, navigate = () => {}, generation = 0;
const busy = new Set();
let resumed = false;
const filter = { text: '', status: '' };

const $ = id => document.getElementById(id);
const root = $('workflow-list'), featured = $('featured'), detail = $('detail'), log = $('log-text');

const stateOf = w => String(apps[w.app]?.state || apps[w.app]?.lifecycle?.state || '未部署');
const isDeployed = w => stateOf(w) === 'deployed';
const shortName = w => w.id.replace(/^(comfyui|community|vendor|custom)-/, '').replaceAll('-', ' ');
const STATE_TEXT = { deployed: '已部署', stopped: '已停止' };

// https://<workspace>--<app>-comfyui-ui.<region>.modal.run
function urlFor(w) {
  if (links[w.id]) return { url: links[w.id], exact: true };
  if (!workspace) return { url: '', exact: false };
  const label = `${workspace}--${w.app}-comfyui-ui`;
  return { url: `https://${label}.${REGION}.modal.run/`, exact: label.length <= MAX_LABEL };
}

function badge(w) {
  const s = stateOf(w), el = document.createElement('span');
  el.className = 'badge ' + (s === 'deployed' ? 'ok' : s === 'stopped' ? 'warn' : '');
  el.textContent = STATE_TEXT[s] || s;
  return el;
}

function card(w) {
  const el = document.createElement('article');
  el.className = 'card';
  el.dataset.workflow = w.id;
  el.classList.toggle('selected', selected === w.id);

  const head = document.createElement('div');
  head.className = 'card-head';
  const name = document.createElement('span');
  name.className = 'card-name';
  name.textContent = shortName(w);
  head.append(name, badge(w));

  const id = document.createElement('p');
  id.className = 'card-id';
  id.textContent = w.id;

  const foot = document.createElement('div');
  foot.className = 'card-footer';
  const gpu = document.createElement('span');
  gpu.className = 'chip';
  gpu.textContent = w.gpu;
  const actions = document.createElement('span');
  actions.className = 'card-actions';
  const { url } = urlFor(w);
  if (isDeployed(w) && url) {
    const open = document.createElement('a');
    open.className = 'link';
    open.href = url;
    open.target = '_blank';
    open.rel = 'noopener noreferrer';
    open.textContent = '打开 ↗';
    open.onclick = e => e.stopPropagation();
    actions.append(open);
  }
  const dep = document.createElement('button');
  dep.className = 'link';
  dep.textContent = busy.has(w.id) ? '部署中…' : isDeployed(w) ? '重新部署' : '部署';
  dep.disabled = busy.has(w.id);
  dep.onclick = e => { e.stopPropagation(); select(w.id); operate('deploy'); };
  actions.append(dep);
  const more = document.createElement('button');
  more.className = 'link';
  more.textContent = '详情';
  more.onclick = e => { e.stopPropagation(); select(w.id); };
  actions.append(more);
  foot.append(gpu, actions);

  el.append(head, id, foot);
  el.onclick = () => select(w.id);
  return el;
}

function matches(w) {
  const q = filter.text.trim().toLowerCase();
  if (q && !(w.id + ' ' + w.gpu).toLowerCase().includes(q)) return false;
  if (filter.status === 'deployed') return isDeployed(w);
  if (filter.status === 'other') return !isDeployed(w);
  return true;
}

function emptyNote(text) {
  const n = document.createElement('div');
  n.className = 'empty';
  n.textContent = text;
  return n;
}

function renderCards() {
  const list = all.filter(matches);
  root.replaceChildren(...(list.length ? list.map(card) : [emptyNote('没有匹配的工作流')]));
  $('wf-count').textContent = `${list.length} / ${all.length} 个工作流`;
  const live = all.filter(isDeployed);
  featured.replaceChildren(...(live.length ? live.map(card) : [emptyNote('暂无已部署的工作流，前往「工作流」页部署')]));
}

function updateDetail() {
  const w = all.find(x => x.id === selected);
  if (!w) return;
  $('detail-title').textContent = shortName(w);
  $('detail-id').textContent = w.id;
  $('detail-gpu').textContent = w.gpu;
  $('detail-app').textContent = w.app;
  const b = $('detail-status'), nb = badge(w);
  b.className = nb.className;
  b.textContent = nb.textContent;
  $('stop').disabled = !isDeployed(w);

  const { url, exact } = urlFor(w), link = $('app-link');
  $('url-text').textContent = url || '读取 Modal 账号信息中…';
  link.hidden = !url;
  if (url) link.href = url;
  $('copy-url').disabled = !url;
  $('url-hint').textContent = !isDeployed(w)
    ? '尚未部署：部署完成后该地址才可访问。'
    : exact ? '首次访问会冷启动 GPU，需等待片刻。'
            : '应用名较长，Modal 可能截断域名；若打不开请到 Modal Dashboard 核对。';
}

function select(id) {
  selected = id;
  detail.hidden = false;
  renderCards();
  updateDetail();
  navigate('workflows');
  detail.scrollIntoView({ block: 'nearest', behavior: 'smooth' });
}

export function init(notice, go) {
  notify = notice;
  navigate = go;
  $('deploy').onclick = () => operate('deploy');
  $('stop').onclick = () => operate('stop');
  $('wf-search').oninput = e => { filter.text = e.target.value; renderCards(); };
  $('wf-status').onchange = e => { filter.status = e.target.value; renderCards(); };
  $('copy-url').onclick = async () => {
    try { await navigator.clipboard.writeText($('url-text').textContent); notify('地址已复制'); }
    catch { notify('复制失败，请手动选择地址'); }
  };
  $('logs-btn').onclick = async () => {
    if (!selected) return;
    log.textContent = '正在获取日志…';
    try { log.textContent = (await api.logs(selected)).log || '暂无日志'; }
    catch (e) { log.textContent = e.message; }
  };
}

export async function load() {
  all = await api.workflows();
  renderCards();
  $('count-all').textContent = all.length;
  $('asset-workflow').replaceChildren(new Option('全部工作流', ''), ...all.map(w => new Option(w.id, w.id)));
}

export async function refresh() {
  const token = ++generation;
  const res = await api.status();
  if (token !== generation) return;
  apps = res.apps || {};
  links = res.links || {};
  workspace = res.workspace || '';
  renderCards();
  updateDetail();
  if (!resumed) {
    resumed = true;
    try {
      for (const [wf, t] of Object.entries(await api.tasks())) {
        if (t.state === 'running' && !busy.has(wf)) { selected = wf; operate(t.action, t.id); }
      }
    } catch {}
  }
  const n = all.filter(isDeployed).length;
  $('count-deployed').textContent = n;
  $('count-other').textContent = all.length - n;
}

async function operate(action, resumeId) {
  if (!selected) return;
  if (action === 'stop' && !confirm('停止部署 ' + selected + '？')) return;
  const wf = selected;
  busy.add(wf); renderCards();
  const buttons = ['deploy', 'stop', 'logs-btn'].map($);
  buttons.forEach(x => x.disabled = true);
  log.textContent = '任务已提交，正在等待结果…';
  try {
    const id = resumeId || (await api.operate(wf, action)).id;
    for (;;) {
      const t = await api.task(id);
      if (selected === wf) { log.textContent = t.log || t.state; $('task-status').textContent = t.state; }
      if (t.state !== 'running') {
        notify(t.state === 'success' ? '操作完成' : '操作失败');
        await refresh();
        break;
      }
      await new Promise(r => setTimeout(r, 1600));
    }
  } catch (e) {
    notify(e.message);
  } finally {
    busy.delete(wf); renderCards();
    buttons.forEach(x => x.disabled = false);
    updateDetail();
  }
}
