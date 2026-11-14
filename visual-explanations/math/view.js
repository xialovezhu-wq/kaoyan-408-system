import { scrubTo, stepBy, stepFromKey } from '@ui-sdk/core/number';

const NS = 'http://www.w3.org/2000/svg';
const fmt = value => String(Number(value.toPrecision(8)));
const ink = { a: 'var(--viz-series-1)', b: 'var(--viz-series-2)', result: 'var(--foreground)', neutral: 'var(--muted-foreground)' };

function node(tag, label = '', cls = '') {
  const item = document.createElement(tag);
  item.textContent = label;
  if (cls) item.className = cls;
  return item;
}

function button(label) {
  const item = node('button', label, 'btn mv-button');
  item.type = 'button';
  return item;
}

function svg(tag, attrs) {
  const item = document.createElementNS(NS, tag);
  for (const [key, value] of Object.entries(attrs)) item.setAttribute(key, value);
  return item;
}

function line(a, b, attrs) {
  return svg('line', { x1: a[0], y1: a[1], x2: b[0], y2: b[1], ...attrs });
}

function svgLabel(chart, label, x, y, anchor = 'start') {
  const item = svg('text', { x, y, 'text-anchor': anchor, class: 'mv-axis-label' });
  item.textContent = label;
  chart.append(item);
}

function chartFor(parent, data, description, square = false) {
  const width = Math.max(180, Math.floor(parent.clientWidth));
  const side = Math.min(width - 60, 235);
  const height = square ? side + 48 : Math.min(320, Math.round(width * 0.62));
  const [xmin, xmax, ymin, ymax] = data.bounds;
  const left = square ? (width - side) / 2 : 42, right = square ? left + side : width - 23;
  const top = 19, bottom = square ? top + side : height - 31;
  const clamp = (value, lo, hi) => Math.max(lo, Math.min(hi, value));
  const px = x => left + (right - left) * (x - xmin) / (xmax - xmin);
  const py = y => bottom - (bottom - top) * (y - ymin) / (ymax - ymin);
  const map = p => [px(p[0]), py(p[1])];
  const chart = svg('svg', { class: 'mv-chart', viewBox: '0 0 ' + width + ' ' + height, width, height,
    role: 'img', 'aria-label': description });
  const originX = xmin <= 0 && 0 <= xmax, originY = ymin <= 0 && 0 <= ymax;
  const horizontal = py(clamp(0, ymin, ymax)), vertical = px(clamp(0, xmin, xmax));
  const axis = { class: 'mv-axis' };
  chart.append(line([left, horizontal], [right, horizontal], { ...axis, 'data-window-edge': !originY }),
    line([vertical, top], [vertical, bottom], { ...axis, 'data-window-edge': !originX }));
  svgLabel(chart, data.axes[0], right + 5, horizontal + 5);
  svgLabel(chart, data.axes[1], vertical + 5, top - 5);
  if (data.kind !== 'given' && !square) {
    for (const x of [xmin, xmax]) svgLabel(chart, fmt(x), px(x), height - 8, x === xmax ? 'end' : 'middle');
    for (const y of [ymin, ymax]) svgLabel(chart, fmt(y), left - 5, py(y) + 4, 'end');
  }
  return { chart, map, screen: { top, bottom, left, right } };
}

function offlineState(initial) {
  let value = initial, change = () => {};
  return {
    get: () => value,
    set(next) { value = next; change(next); },
    subscribe(listener) { change = listener; return { unsubscribe: () => { change = () => {}; } }; },
  };
}

function model(root, binding, initial, verify, draw) {
  const state = binding || offlineState(initial);
  let restored = false, acted = false;
  state.set(initial);
  state.subscribe(draw);
  const restore = snapshot => {
    // The host can deliver its initial snapshot after the surface mounts.
    // Once the learner interacts, a late snapshot must not undo their choice.
    if (acted || restored) return;
    const saved = snapshot?.privateContent;
    if (saved?.mathVisualId !== root.id) return;
    const next = verify(saved.interaction);
    if (next) {
      restored = true;
      state.set(next);
    }
  };
  restore(window.openai?.widgetState);
  window.addEventListener('openai:set_globals', event => restore(event.detail?.globals?.widgetState));
  draw(state.get());
  const save = () => {
    Promise.resolve(window.openai?.setWidgetState?.({
      privateContent: { mathVisualId: root.id, interaction: state.get() }
    })).catch(() => {});
  };
  return {
    read: state.get,
    set(value, commit = false) {
      acted = true;
      state.set(value);
      if (commit) save();
    },
    save,
  };
}

function drawWhenSized(target, draw) {
  if (typeof ResizeObserver === 'function') new ResizeObserver(draw).observe(target);
  else window.addEventListener('resize', draw);
}

function axesOnly(plot, data) {
  const draw = () => plot.replaceChildren(chartFor(plot, { ...data, bounds: [-1, 1, -1, 1] },
    '只标题面变量的坐标示意，无刻度或推导边界').chart);
  drawWhenSized(plot, draw);
  draw();
}

function sample(points, at, axis) {
  const right = points.findIndex(p => p[axis] >= at);
  if (right <= 0) return points[right < 0 ? points.length - 1 : 0][1 - axis];
  const p = points[right - 1], q = points[right];
  if (q[axis] === at) return q[1 - axis];
  const portion = (at - p[axis]) / (q[axis] - p[axis]);
  return p[1 - axis] + (q[1 - axis] - p[1 - axis]) * portion;
}

function region(root, targets, data, binding) {
  const { controls, plot, detail } = targets;
  const axis = data.sweep.axis === 'x' ? 0 : 1;
  const [min, max] = data.bounds.slice(2 * axis, 2 * axis + 2);
  const { initial, step } = data.sweep, stops = data.stops || [];
  const snap = { points: stops.map(s => s.at), threshold: Math.min(step * 0.55, (max - min) / 90) };
  const params = { min, max, step, snap };
  const pointerParams = { min, max, step: 0, snap };
  const boundaryIds = new Set(data.boundaries.map(b => b.id));
  const map = new Map(data.boundaries.map(b => [b.id, b]));
  const axisLabel = data.axes[axis], otherAxis = data.axes[1 - axis];
  const place = (t, v) => axis === 0 ? [t, v] : [v, t];
  const sideLabels = axis === 0 ? ['下界', '上界'] : ['左界', '右界'];
  const wrap = node('div', '', 'mv-parameters');
  const label = node('label', '固定 ' + axisLabel, 'form-label');
  const range = node('input', '', 'form-range');
  range.type = 'range'; range.min = min; range.max = max; range.step = 'any';
  range.id = root.id + '-parameter'; label.htmlFor = range.id;
  const readout = node('output', '', 'mv-readout');
  readout.htmlFor = range.id; readout.setAttribute('aria-live', 'off');
  const stopList = node('div', '', 'mv-wrap');
  stopList.setAttribute('role', 'group'); stopList.setAttribute('aria-label', '定位已公开关键位置');
  const stopsUI = stops.map(stop => {
    const item = button(stop.label + ' · ' + axisLabel + ' ' + fmt(stop.at));
    item.addEventListener('click', () => seek(stop.at, true));
    stopList.append(item);
    return item;
  });
  wrap.append(label, range, readout);
  controls.append(wrap);
  if (stops.length) controls.append(stopList);
  const stateLine = node('p', '', 'mv-status');
  const announce = node('span', '', 'sr-only');
  announce.setAttribute('aria-live', 'polite');
  const legend = node('div', '', 'mv-wrap');
  legend.setAttribute('role', 'group'); legend.setAttribute('aria-label', '定位边界曲线');
  const boundaryButtons = data.boundaries.map(boundary => {
    const item = button(boundary.label);
    item.addEventListener('click', () => choose(boundary.id));
    legend.append(item);
    return item;
  });
  detail.append(stateLine, legend, announce);

  let pending = 0;
  let latest;
  function schedule(state) {
    latest = state;
    if (!pending) pending = requestAnimationFrame(() => {
      pending = 0;
      paintPlot(latest);
    });
  }
  function paintPlot(state) {
    if (!state) return;
    const { at, selected } = state;
    const { chart, map: pixel, screen } = chartFor(plot, data, '阴影为区域，虚线为固定 ' + axisLabel + ' 的切线');
    chart.dataset.position = String(at);
    const trace = (curve, start, end) => [
      place(start, sample(curve, start, axis)),
      ...curve.filter(p => p[axis] > start && p[axis] < end),
      place(end, sample(curve, end, axis))
    ];
    const coords = points => points.map(p => pixel(p).join(',')).join(' ');
    for (const band of data.bands) {
      const low = trace(map.get(band.lower).points, band.from, band.to);
      const high = trace(map.get(band.upper).points, band.from, band.to);
      chart.append(svg('polygon', { points: coords(low.concat(high.reverse())), class: 'mv-fill' }));
    }
    for (const stop of stops) {
      const [x, y] = pixel(place(stop.at, axis === 0 ? data.bounds[2] : data.bounds[0]));
      const ends = axis === 0 ? [[x, screen.top], [x, screen.bottom]] : [[screen.left, y], [screen.right, y]];
      chart.append(line(...ends, { class: 'mv-stop', 'data-stop': fmt(stop.at) }));
    }
    const [x, y] = pixel(place(at, axis === 0 ? data.bounds[2] : data.bounds[0]));
    const ends = axis === 0 ? [[x, screen.top], [x, screen.bottom]] : [[screen.left, y], [screen.right, y]];
    chart.append(line(...ends, { class: 'mv-guide' }));
    const { band, pair, activeIds } = section(at);
    const boundaries = data.boundaries.map((boundary, index) => ({ boundary, index })).sort((a, b) =>
      Number(a.boundary.id === selected) - Number(b.boundary.id === selected));
    for (const { boundary, index } of boundaries) {
      const attrs = { points: coords(boundary.points), 'data-object': boundary.id };
      const active = activeIds.has(boundary.id), inspected = boundary.id === selected;
      const color = index === 0 ? ink.a : index === 1 ? ink.b : ink.neutral;
      chart.append(svg('polyline', { ...attrs, class: 'mv-boundary', stroke: color,
        'data-active': active, 'data-selected': inspected, 'stroke-dasharray': index % 2 ? '6 3' : 'none' }));
      chart.append(svg('polyline', { ...attrs, class: 'mv-hit cursor-interaction' }));
    }
    if (band && pair && (pair[1] > pair[0] || !data.open_endpoints)) {
      const endpoints = pair.map(v => pixel(place(at, v)));
      if (pair[1] > pair[0]) chart.append(line(...endpoints, { class: 'mv-cut' }));
      for (const v of new Set(pair)) {
        const [cx, cy] = pixel(place(at, v));
        chart.append(svg('circle', { cx, cy, r: 4, class: 'mv-endpoint', 'data-open': Boolean(data.open_endpoints) }));
      }
    }
    plot.replaceChildren(chart);
  }
  function section(at) {
    const adjacent = data.bands.filter(b => b.from <= at && at <= b.to);
    const band = adjacent.at(-1);
    const pair = band && [sample(map.get(band.lower).points, at, axis), sample(map.get(band.upper).points, at, axis)];
    return { adjacent, band, pair, activeIds: new Set(adjacent.flatMap(b => [b.lower, b.upper])) };
  }
  function describe(at) {
    const { adjacent, band, pair } = section(at);
    if (!band || (data.open_endpoints && pair[0] === pair[1])) return '无有效切段';
    const [left, right] = pair.map(fmt);
    if (pair[0] === pair[1]) return '切段缩为一点 · ' + otherAxis + ' ' + left;
    const names = side => [...new Set(adjacent.map(b => map.get(b[side]).label))].join('、');
    const nav = data.approximate ? '约 ' : '';
    return sideLabels[0] + '：' + nav + left + '（' + names('lower') + '）；' +
      sideLabels[1] + '：' + nav + right + '（' + names('upper') + '）';
  }
  function draw(state) {
    const { at, selected } = state;
    const current = '固定 ' + axisLabel + '：' + fmt(at);
    const summary = describe(at);
    range.value = at;
    range.setAttribute('aria-valuetext', current + '；' + summary);
    readout.textContent = fmt(at);
    stateLine.textContent = summary + (selected ? ' · 已定位：' + map.get(selected).label : '');
    const { activeIds } = section(at);
    data.boundaries.forEach((b, i) => {
      boundaryButtons[i].setAttribute('aria-pressed', String(selected === b.id));
      boundaryButtons[i].dataset.active = String(activeIds.has(b.id));
    });
    stopsUI.forEach((control, i) => control.setAttribute('aria-pressed', String(at === stops[i].at)));
    schedule(state);
  }
  if (data.windowed || data.approximate || data.open_endpoints) {
    detail.insertBefore(node('p', [
      data.windowed && '图仅显示所选窗口',
      data.approximate && '曲线与读数为采样近似',
      data.open_endpoints && '空心点表示端点不计入'
    ].filter(Boolean).join(' · '), 'mv-caption text-small'), legend);
  }
  function compatible(value) {
    if (!value || typeof value.at !== 'number' || !Number.isFinite(value.at) || value.at < min || value.at > max) return null;
    return { at: value.at, selected: boundaryIds.has(value.selected) ? value.selected : '' };
  }
  const control = model(root, binding, { at: initial, selected: '' }, compatible, draw);
  function speak() { announce.textContent = range.getAttribute('aria-valuetext'); }
  function seek(at, commit) {
    control.set({ ...control.read(), at }, commit);
    if (commit) speak();
  }
  function choose(id) {
    if (!boundaryIds.has(id)) return;
    control.set({ ...control.read(), selected: control.read().selected === id ? '' : id }, true);
    announce.textContent = stateLine.textContent;
  }
  plot.addEventListener('click', event => choose(event.target.closest('[data-object]')?.dataset.object));
  range.addEventListener('input', () => seek(scrubTo(range.valueAsNumber, pointerParams).value, false));
  range.addEventListener('change', () => { control.save(); speak(); });
  range.addEventListener('keydown', event => {
    if (event.ctrlKey || event.metaKey) return;
    const move = stepFromKey(event, { dir: getComputedStyle(range).direction });
    if (!move) return;
    event.preventDefault();
    const at = control.read().at;
    let next = move.bound ? (move.bound === 'min' ? min : max) : stepBy(at, move, params);
    // With an off-grid threshold stop, always allow the next key to leave it.
    if (!move.bound && next === at) next = Math.max(min, Math.min(max, at + move.direction * step));
    seek(next, true);
  });
  drawWhenSized(plot, () => schedule(control.read()));
}

function compare(root, targets, data, binding) {
  const { controls, plot, detail } = targets;
  const group = node('fieldset', '', 'mv-cases');
  group.append(node('legend', '切换条件', 'form-label'));
  const choices = node('div', '', 'mv-wrap');
  const cases = data.cases.map((item, index) => {
    const id = root.id + '-case-' + index;
    const input = node('input');
    input.type = 'radio'; input.name = root.id + '-conditions'; input.id = id;
    const label = node('label', '', 'mv-radio');
    label.htmlFor = id; label.append(input, node('span', item.label));
    input.addEventListener('change', () => { if (input.checked) control.set({ ...control.read(), case: index }, true); });
    choices.append(label);
    return input;
  });
  group.append(choices); controls.append(group);
  const pictures = node('div', '', 'mv-comparison');
  const names = ['基准', '当前条件'];
  const boxes = names.map(heading => {
    const wrap = node('div', '', 'mv-panel');
    const title = node('p', heading, 'mv-caption');
    const plotArea = node('div', '', 'mv-panel-plot');
    wrap.append(title, plotArea);
    pictures.append(wrap);
    return { title, plotArea };
  });
  plot.append(pictures);
  const inspect = node('div', '', 'mv-wrap');
  inspect.setAttribute('role', 'group'); inspect.setAttribute('aria-label', '在两种条件中定位同一对象');
  const description = node('p', '', 'mv-status');
  description.setAttribute('aria-live', 'polite');
  const objects = new Set(data.objects.map(o => o.id));
  const objectButtons = data.objects.map(obj => {
    const item = button(obj.label);
    item.addEventListener('click', () => choose(obj.id));
    inspect.append(item);
    return item;
  });
  detail.append(inspect, description);

  function drawPlot(panel, config, selected) {
    const { title, plotArea } = boxes[panel];
    title.textContent = names[panel] + ' · ' + config.label;
    const { chart, map } = chartFor(plotArea, data, title.textContent, true);
    // The result is drawn on top; overlapping lines remain distinguishable.
    const priority = obj => obj.ink === 'result' ? 2 : obj.id === selected ? 1 : 0;
    for (const obj of [...data.objects].sort((a, b) => priority(a) - priority(b))) {
      const value = config.items[obj.id];
      const points = value.points.map(map);
      const attrs = { 'data-object': obj.id, 'data-selected': selected === obj.id, stroke: ink[obj.ink],
        class: 'mv-geo cursor-interaction', 'data-ink': obj.ink };
      if (value.kind === 'point') {
        chart.append(svg('circle', { ...attrs, cx: points[0][0], cy: points[0][1], r: 5, fill: ink[obj.ink] }));
      } else {
        chart.append(line(...points, attrs));
        if (value.kind === 'vector') {
          const [a, b] = points, angle = Math.atan2(b[1] - a[1], b[0] - a[0]);
          for (const sign of [-1, 1]) {
            const offset = angle + sign * 0.45;
            chart.append(line(b, [b[0] - 9 * Math.cos(offset), b[1] - 9 * Math.sin(offset)], attrs));
          }
        }
        chart.append(line(...points, { 'data-object': obj.id, class: 'mv-hit cursor-interaction' }));
      }
    }
    plotArea.replaceChildren(chart);
  }
  function plots(state) {
    drawPlot(0, data.cases[0], state.selected);
    drawPlot(1, data.cases[state.case], state.selected);
  }
  function draw(state) {
    cases.forEach((item, i) => { item.checked = i === state.case; });
    data.objects.forEach((obj, i) => objectButtons[i].setAttribute('aria-pressed', String(obj.id === state.selected)));
    const obj = data.objects.find(entry => entry.id === state.selected);
    const previous = data.cases[0].items[obj.id].note, next = data.cases[state.case].items[obj.id].note;
    description.textContent = obj.label + '：' + (previous === next ? '两栏均为 ' + previous : previous + ' → ' + next);
    plots(state);
  }
  function verify(value) {
    return value && Number.isInteger(value.case) && value.case >= 0 && value.case < cases.length
      ? { case: value.case, selected: objects.has(value.selected) ? value.selected : data.objects[0].id } : null;
  }
  const control = model(root, binding, { case: 1, selected: data.objects.find(obj => obj.ink === 'result')?.id ?? data.objects[0].id }, verify, draw);
  function choose(id) {
    if (objects.has(id)) control.set({ ...control.read(), selected: id }, true);
  }
  plot.addEventListener('click', event => choose(event.target.closest('[data-object]')?.dataset.object));
  boxes.forEach(box => drawWhenSized(box.plotArea, () => plots(control.read())));
}

export function start(root, host, source, adapter) {
  function setup(targets, scene, interaction) {
    if (scene.kind === 'given') axesOnly(targets.plot, scene);
    else if (scene.kind === 'region') region(root, targets, scene, interaction);
    else compare(root, targets, scene, interaction);
  }
  function native() {
    const targets = Object.fromEntries(['controls', 'plot', 'detail'].map(name => {
      const el = node('div');
      el.setAttribute('data-visual-' + name, '');
      return [name, el];
    }));
    host.replaceChildren(...Object.values(targets));
    host.dataset.renderer = 'native';
    setup(targets, source.scene ?? source.message.createSurface.dataModel.scene);
  }
  if (source.message && adapter) {
    adapter.mount(host, source.message).then(({ targets, scene, interaction }) => {
      host.dataset.renderer = 'a2ui';
      setup(targets, scene, interaction);
    }).catch(native);
  } else native();
}
