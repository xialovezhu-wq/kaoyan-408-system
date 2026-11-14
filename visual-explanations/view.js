import { bindDrag, createFocusRecovery, createSelection, definePrimitive } from '@ui-sdk/core';
import { fractionOf, scrubTo, stepBy, stepFromKey, valueAt } from '@ui-sdk/core/number';

const fieldSelection = definePrimitive({
  name: 'visual-selection',
  state: () => ({ keys: [] }),
  api(context) {
    const order = () => context.options().order();
    const selection = createSelection(context, {
      key: 'keys',
      order,
      selectable: key => order().includes(key),
      set(keys) {
        context.store.set({ keys });
        return true;
      }
    });
    return { ...selection, clear: () => context.store.set({ keys: [] }) };
  },
  slots: () => ({})
});

function element(tag, text = '', className = '') {
  const node = document.createElement(tag);
  node.textContent = text;
  if (className) node.className = className;
  return node;
}

function button(text, className = 'btn') {
  const node = element('button', text, className);
  node.type = 'button';
  return node;
}

function svgNode(tag, attributes) {
  const node = document.createElementNS('http://www.w3.org/2000/svg', tag);
  for (const [key, value] of Object.entries(attributes)) node.setAttribute(key, value);
  return node;
}

function signal(fromLeft, lost) {
  const svg = svgNode('svg', { viewBox: '0 0 100 16', preserveAspectRatio: 'none', 'aria-hidden': 'true' });
  const start = fromLeft ? 11 : 89;
  const end = lost === false ? (fromLeft ? 89 : 11) : (fromLeft ? 62 : 38);
  svg.append(svgNode('line', { x1: start, x2: end, y1: 4, y2: 12, class: 've-arrow',
    'data-lost': lost === true, 'data-sent-only': lost === null }));
  if (lost === true) {
    svg.append(svgNode('path', { d: 'M ' + (end - 2) + ' 8 l 4 8 m 0 -8 l -4 8', class: 've-arrow' }));
  } else {
    const back = fromLeft ? end - 3 : end + 3;
    svg.append(svgNode('polygon', { points: end + ',12 ' + back + ',8 ' + back + ',15', class: 've-arrow-head' }));
  }
  return svg;
}

function createStepScrubber(data, seek, save) {
  const timeline = element('div', '', 've-step-timeline');
  const labels = element('ol', '', 've-step-stops');
  const track = element('div', '', 've-step-track');
  track.setAttribute('aria-hidden', 'true');
  track.append(element('div', '', 've-step-rail'), element('div', '', 've-step-thumb'));
  const range = { min: 0, max: data.steps.length - 1, step: 1 };
  labels.setAttribute('aria-label', '步骤');
  timeline.style.setProperty('--ve-step-count', data.steps.length);
  timeline.style.setProperty('--ve-track-inset', (50 / data.steps.length) + '%');
  timeline.append(labels, track);

  const stops = data.steps.map((label, index) => {
    const item = element('li');
    const stop = button(String(index + 1), 'btn btn-block');
    stop.dataset.visualStep = String(index);
    stop.setAttribute('aria-label', '第 ' + (index + 1) + ' 步：' + label);
    stop.dataset.tooltip = label;
    stop.addEventListener('click', () => seek(index, true));
    item.append(stop);
    labels.append(item);
    return stop;
  });
  let cleanup;
  function point(x) {
    const box = track.getBoundingClientRect();
    if (box.width > 0) seek(scrubTo(valueAt((x - box.left) / box.width, range), range).value, false);
  }
  track.addEventListener('pointerdown', event => {
    if (!event.isPrimary || event.button !== 0) return;
    const previous = stops.findIndex(stop => stop.hasAttribute('aria-current'));
    const startX = event.clientX;
    let moved = false;
    cleanup?.();
    event.preventDefault();
    track.dataset.dragging = 'true';
    point(event.clientX);
    cleanup = bindDrag(track, event, {
      cancelOnEscape: true,
      onMove: sample => {
        moved ||= Math.abs(sample.x - startX) >= 3;
        point(sample.x);
      },
      onEnd: (event, canceled) => {
        cleanup = undefined;
        delete track.dataset.dragging;
        if (canceled && previous >= 0) seek(previous, false);
        else if (moved) point(event.clientX);
        save();
      }
    });
  });
  return {
    timeline, stops,
    update(step) {
      stops.forEach((stop, index) => {
        stop.setAttribute('aria-pressed', String(step === index));
        if (step === index) stop.setAttribute('aria-current', 'step');
        else stop.removeAttribute('aria-current');
      });
      track.style.setProperty('--ve-step-progress', fractionOf(step, range) * 100 + '%');
    }
  };
}

function fieldKey(panelIndex, field) {
  return 'field/' + panelIndex + '/' + field.id;
}

function fieldChanges(panel, step, field) {
  const previous = step === 0 ? null : panel.frames[step - 1].fields.find(item => item.id === field.id)?.value;
  const hasValue = field.value !== '—';
  const comparable = hasValue && previous != null && previous !== '—';
  const changed = comparable && previous !== field.value;
  const comparison = changed ? previous + ' → ' + field.value :
    hasValue ? field.value + (comparable ? '（未变）' : '') : '本步未提供状态值';
  return { changed, description: [comparison, field.detail].filter(Boolean).join('。') };
}

function eventKey(panel, index, event) {
  return 'event/' + panel + '/' + event.id + '/' + index;
}

function statusButton(field, panel, panelIndex, inspect) {
  const key = fieldKey(panelIndex, field);
  if (field.value === '—' && !field.detail) return element('span', '—', 'text-muted');
  return inspect(key, field.value, [panel.label, field.label, field.value].filter(Boolean).join(' '), 'btn viz-tile ve-state-value');
}

function appendFieldChange(cell, panel, step, field) {
  if (fieldChanges(panel, step, field).changed) {
    cell.append(element('span', '本步变化', 've-status-change text-small'));
  }
}

function stateComparison(data, step, inspect, id) {
  const table = element('table', '', 've-status-comparison');
  table.append(element('caption', '当前状态'));
  const headings = element('tr');
  for (const title of ['状态', ...data.panels.map(panel => panel.label)]) {
    const heading = element('th', title);
    heading.id = id + '-column-' + headings.children.length;
    heading.scope = 'col';
    headings.append(heading);
  }
  const head = element('thead');
  head.append(headings);
  table.append(head);
  const body = element('tbody');
  const columns = data.panels.map(panel => new Map(panel.frames[step].fields.map(field => [field.label, field])));
  const labels = new Set(columns.flatMap(column => [...column.keys()]));
  for (const label of labels) {
    const row = element('tr');
    row.dataset.fieldLabel = label;
    const heading = element('th', label);
    heading.id = id + '-row-' + body.children.length;
    heading.scope = 'row';
    row.append(heading);
    columns.forEach((column, index) => {
      const field = column.get(label);
      const cell = element('td', '', 've-status-cell');
      const panel = data.panels[index];
      cell.setAttribute('headers', heading.id + ' ' + headings.children[index + 1].id);
      const compactLabel = element('span', panel.label, 've-compact-side text-small');
      compactLabel.setAttribute('aria-hidden', 'true');
      cell.append(compactLabel);
      if (field) {
        cell.dataset.active = String(field.active);
        cell.append(statusButton(field, panel, index, inspect));
        appendFieldChange(cell, panel, step, field);
      } else {
        cell.append(element('span', '—', 'text-muted'));
      }
      row.append(cell);
    });
    body.append(row);
  }
  table.append(body);
  return table;
}

function canAlignFields(panels, step) {
  if (panels.length !== 2 || panels.some(panel => !panel.label) || panels[0].label === panels[1].label) return false;
  const labels = panels.map(panel => panel.frames[step].fields.map(field => field.label));
  return labels.every(items => new Set(items).size === items.length) && labels[0].some(label => labels[1].includes(label));
}

function eventLabel(panel, event, index, state) {
  const status = event.lost === true ? '，途中丢失，未到达' :
    event.lost === null ? '，仅显示发出' :
    event.arrives > state ? '，传输中，尚未到达' : '';
  return panel.label + '：' + panel.lanes[event.from] + ' → ' + panel.lanes[1 - event.from] +
    '，' + event.label + '，第 ' + (index + 1) + ' 步发出' + status;
}

function timeline(panel, panelIndex, state, inspect, showFields, jump) {
  const region = element('section', '', 've-panel');
  region.append(element('h4', panel.label));
  const axis = element('div', '', 've-time-axis');
  axis.append(element('span', panel.lanes[0]), element('span', '时间 ↓', 'text-small'), element('span', panel.lanes[1]));
  const lanes = element('div', '', 've-lanes');
  const list = element('ol', '', 've-events');
  list.setAttribute('aria-label', panel.label + '报文历史');
  const hasArrivals = panel.frames.some(frame => frame.event?.arrives !== undefined);
  const trajectory = hasArrivals ? svgNode('svg', {
    class: 've-trajectory', viewBox: '0 0 100 ' + ((state + 1) * 100),
    preserveAspectRatio: 'none', 'aria-hidden': 'true'
  }) : null;
  if (trajectory) {
    list.dataset.spanning = 'true';
    lanes.append(trajectory);
  }
  // Keep reached rows without building a nested scroller in the conversation.
  for (const [index, frame] of panel.frames.slice(0, state + 1).entries()) {
    const event = frame.event;
    const row = element('li', '', 've-event');
    const current = index === state;
    row.dataset.current = String(current);
    if (current) row.setAttribute('aria-current', 'step');
    if (event) {
      const key = eventKey(panelIndex, index, event);
      const accessible = eventLabel(panel, event, index, state);
      if (trajectory && event.arrives !== undefined) {
        const left = event.from === 0;
        const landed = event.arrives <= state;
        const start = left ? 11 : 89;
        const end = landed ? (left ? 89 : 11) : (left ? 52 : 48);
        const y = (landed ? event.arrives * 100 + 15 : index * 100 + 89);
        const edge = svgNode('g', { 'data-message-path': key });
        edge.append(svgNode('line', { x1: start, x2: end, y1: index * 100 + 58, y2: y,
          class: 've-arrow', 'data-sent-only': !landed }));
        const tail = left ? end - 3 : end + 3;
        edge.append(svgNode('polygon', { class: 've-arrow-head', 'data-landed': landed,
          points: end + ',' + y + ' ' + tail + ',' + (y - 10) + ' ' + tail + ',' + (y + 5) }));
        trajectory.append(edge);
      } else {
        row.append(signal(event.from === 0, event.lost));
      }
      const label = element('div', '', 've-event-hit');
      label.append(inspect(key, event.label, accessible, current ? 'btn viz-tile' : 'btn btn-ghost',
        { jump: () => jump(index, key), eventStep: index }));
      row.append(label);
    } else {
      row.append(element('span', current ? '本步无报文' : '·', 've-event-label text-small'));
    }
    list.append(row);
  }
  lanes.append(list);
  region.append(axis, lanes);
  if (showFields) {
    const fields = element('dl', '', 've-fields');
    for (const field of panel.frames[state].fields) {
      const row = element('div', '', 've-field');
      const value = element('dd', '', 've-status-cell');
      row.dataset.active = String(field.active);
      value.append(statusButton(field, panel, panelIndex, inspect));
      appendFieldChange(value, panel, state, field);
      row.append(element('dt', field.label), value);
      fields.append(row);
    }
    region.append(fields);
  }
  return region;
}

function flow(panel, panelIndex, step, inspect, showChange) {
  const frame = panel.frames[step];
  const region = element('section', '', 've-panel');
  if (panel.label) region.append(element('h4', panel.label));
  const graph = element('div', '', 've-flow');
  for (const [index, field] of frame.fields.entries()) {
    const row = element('div', '', 've-flow-node ve-field');
    row.dataset.active = String(field.active);
    const value = element('div', '', 've-status-cell');
    if (showChange) {
      value.append(statusButton(field, panel, panelIndex, inspect));
      appendFieldChange(value, panel, step, field);
    } else {
      value.textContent = field.value;
    }
    row.append(element('span', field.label), value);
    graph.append(row);
    if (index !== frame.fields.length - 1 && field.link) {
      graph.append(element('div', '↓ ' + field.link, 've-flow-edge text-small'));
    }
  }
  region.append(graph);
  return region;
}

function sliceView(data, position, width) {
  const { xmin, xmax, ymin, ymax, samples, active } = data;
  const height = Math.min(305, Math.round(width * 0.66));
  const margin = { left: 37, right: 12, top: 12, bottom: 28 };
  const x = value => margin.left + (width - margin.left - margin.right) * (value - xmin) / (xmax - xmin);
  const y = value => height - margin.bottom - (height - margin.top - margin.bottom) * (value - ymin) / (ymax - ymin);
  const chart = svgNode('svg', { class: 've-slice', viewBox: '0 0 ' + width + ' ' + height, width, height,
    role: 'img', 'aria-label': '区间图，竖线标出当前固定横坐标' });
  const upper = samples.map(p => x(p[0]) + ',' + y(p[2]));
  const lower = samples.slice().reverse().map(p => x(p[0]) + ',' + y(p[1]));
  const axis = { stroke: 'var(--muted-foreground)', 'stroke-width': 1 };
  const label = (px, py, content, anchor) => {
    const text = svgNode('text', { x: px, y: py, 'text-anchor': anchor, fill: 'currentColor', 'font-size': '0.9em' });
    text.textContent = content;
    chart.append(text);
  };
  chart.append(
    svgNode('polygon', { points: upper.concat(lower).join(' '), fill: 'var(--viz-series-1)', 'fill-opacity': 0.17 }),
    svgNode('polyline', { points: upper.join(' '), stroke: 'var(--viz-series-1)', fill: 'none' }),
    svgNode('polyline', { points: lower.join(' '), stroke: 'var(--viz-series-1)', fill: 'none' }),
    svgNode('line', { ...axis, x1: x(xmin), x2: x(xmax), y1: y(ymin), y2: y(ymin) }),
    svgNode('line', { ...axis, x1: x(xmin), x2: x(xmin), y1: y(ymin), y2: y(ymax) }),
    svgNode('line', { x1: x(position), x2: x(position), y1: y(ymin), y2: y(ymax), stroke: 'var(--muted-foreground)', 'stroke-dasharray': '4 3' }),
  );
  if (active) {
    chart.append(svgNode('line', { x1: x(position), x2: x(position), y1: y(active[0]), y2: y(active[1]), stroke: 'var(--viz-series-1)', 'stroke-width': 3 }));
    for (const value of new Set(active)) {
      chart.append(svgNode('circle', { cx: x(position), cy: y(value), r: 3, fill: 'var(--viz-series-1)' }));
    }
  }
  label(x(xmin), height - 6, String(xmin), 'middle');
  label(x(xmax), height - 6, String(xmax), 'end');
  label(x(xmin) - 7, y(ymin) + 4, String(ymin), 'end');
  label(x(xmin) - 7, y(ymax) + 4, String(ymax), 'end');
  return chart;
}

export function start(root, host, source, adapter) {
  const names = ['controls', 'plot', 'detail'];

  function setup(targets, data, interaction) {
    const { controls, plot, detail } = targets;
    if (data.kind === 'slice') {
      const label = element('label', '', 'form-label');
      const range = element('input', '', 'form-range');
      range.type = 'range';
      range.min = data.xmin;
      range.max = data.xmax;
      range.step = data.step;
      range.value = data.initial;
      const readout = element('output');
      readout.setAttribute('aria-live', 'polite');
      const id = root.id + '-slice';
      range.id = id;
      label.htmlFor = id;
      label.append(element('span', data.control), range);
      controls.append(label, readout);
      const format = n => String(Number(n.toPrecision(7)));
      function draw() {
        const at = Number(range.value);
        const upper = data.samples.findIndex(p => p[0] >= at);
        const right = upper === -1 ? data.samples.length - 1 : upper;
        const before = data.samples[Math.max(0, right - 1)];
        const after = data.samples[right];
        const ratio = after[0] !== before[0] ? (at - before[0]) / (after[0] - before[0]) : 0;
        const active = [1, 2].map(i => before[i] + (after[i] - before[i]) * ratio);
        plot.replaceChildren(sliceView({ ...data, active }, at, Math.max(220, Math.floor(plot.clientWidth))));
        const [lower, higher] = active.map(format);
        readout.textContent = '横坐标 ' + format(at) + ' · ' + (data.approximate ? '近似下界 ' : '下界 ') +
          lower + '，' + (data.approximate ? '近似上界 ' : '上界 ') + higher;
      }
      range.addEventListener('input', draw);
      new ResizeObserver(draw).observe(plot);
      draw();
      return;
    }

    const prev = button('上一步');
    const next = button('下一步', 'btn btn-primary');
    const stepLabel = element('span', '', 've-step-label');
    stepLabel.setAttribute('aria-live', 'polite');
    controls.className = 've-controls';
    controls.append(prev, next, stepLabel);
    const scrubber = data.steps.length > 1 ? createStepScrubber(data, seek, save) : null;
    if (scrubber) controls.after(scrubber.timeline);
    detail.setAttribute('aria-live', 'polite');
    let step = Math.max(0, data.initial);
    let cleanups = [];
    const buttons = new Map();
    const recovery = createFocusRecovery({
      fallback: lost => buttons.get(lost.key) ?? (!next.disabled ? next : !prev.disabled ? prev : null)
    });
    const currentObjects = () => new Map(data.panels.flatMap((panel, panelIndex) => {
      const { fields } = panel.frames[step];
      const entries = fields.filter(field => field.value !== '—' || field.detail).map(field => [
        fieldKey(panelIndex, field), [panel.label, field.label, fieldChanges(panel, step, field).description].filter(Boolean).join(' · ')
      ]);
      panel.frames.slice(0, step + 1).forEach(({ event }, index) => {
        if (event) entries.push([eventKey(panelIndex, index, event), [
          eventLabel(panel, event, index, step), event.detail
        ].filter(Boolean).join('。')]);
      });
      return entries;
    }));
    let available = currentObjects();
    const selection = fieldSelection({ id: root.id + '-state-selection', order: () => [...available.keys()] });
    function updateDetail() {
      const [key] = selection.get().keys;
      let selectedButton;
      for (const node of plot.querySelectorAll('[data-visual-object]')) {
        node.setAttribute('aria-pressed', String(node.dataset.visualObject === key));
        if (node.dataset.visualObject === key) selectedButton = node;
      }
      for (const path of plot.querySelectorAll('[data-message-path]')) {
        path.dataset.selected = String(path.dataset.messagePath === key);
      }
      const explanation = available.get(key) || '';
      const row = selectedButton?.closest('[data-field-label]');
      const cell = selectedButton?.closest('.ve-status-cell');
      plot.querySelector('.ve-state-detail')?.remove();
      if (row && explanation) {
        const detailRow = element('tr', '', 've-state-detail');
        const value = element('td', explanation);
        value.colSpan = data.panels.length + 1;
        detailRow.append(value);
        row.after(detailRow);
      } else if (cell && explanation) {
        cell.append(element('p', explanation, 've-state-detail'));
      }
      detail.classList.toggle('sr-only', Boolean(cell));
      if (detail.textContent !== explanation) detail.textContent = explanation;
    }
    function inspect(key, value, accessible, className, action) {
      const hit = button(value, className);
      hit.dataset.visualObject = key;
      if (action) hit.dataset.eventStep = String(action.eventStep);
      hit.setAttribute('aria-label', accessible);
      hit.addEventListener('click', () => {
        if (action && action.eventStep !== step) {
          action.jump();
        } else {
          const selected = selection.get().keys[0] === key ? null : key;
          change({ step, selected }, true);
        }
      });
      cleanups.push(recovery.bind(hit, { key, props: {} }));
      buttons.set(key, hit);
      return hit;
    }
    function show() {
      // Keep old bindings through removal so focus recovery can identify the lost control.
      const previous = cleanups;
      cleanups = [];
      buttons.clear();
      available = currentObjects();
      const aligned = canAlignFields(data.panels, step);
      // Preserve the vertical lifelines for sequences. For a field-only comparison
      // the table already shows both flows unless one has explicit links to draw.
      const drawPanels = data.kind === 'sequence' || !aligned ||
        data.panels.some(panel => panel.frames[step].fields.some(field => field.link));
      const contents = [];
      if (drawPanels) {
        const grid = element('div', '', 've-panels');
        grid.dataset.pair = String(data.panels.length === 2);
        data.panels.forEach((panel, index) => {
          grid.append(data.kind === 'sequence' ? timeline(panel, index, step, inspect, !aligned,
            (target, selected) => change({ step: target, selected }, true))
            : flow(panel, index, step, inspect, !aligned));
        });
        contents.push(grid);
      }
      if (aligned) contents.push(stateComparison(data, step, inspect, root.id));
      plot.replaceChildren(...contents);
      for (const cleanup of previous) cleanup();
      prev.disabled = step === 0;
      next.disabled = step === data.steps.length - 1;
      stepLabel.textContent = (step + 1) + '/' + data.steps.length + ' · ' + data.steps[step];
      scrubber?.update(step);
      const oneStep = data.steps.length < 2;
      prev.hidden = oneStep;
      next.hidden = oneStep;
      stepLabel.hidden = oneStep;
    }
    function save() {
      const openai = window.openai;
      if (openai?.setWidgetState) Promise.resolve(openai.setWidgetState({
        privateContent: { key: root.id, step, selected: selection.get().keys[0] ?? null }
      })).catch(() => {});
    }
    function applyInteraction(state) {
      if (state.step === step && state.selected === (selection.get().keys[0] ?? null)) return;
      const focusedStepButton = document.activeElement === prev || document.activeElement === next;
      if (state.step !== step) {
        step = state.step;
        show();
      }
      if (state.selected && available.has(state.selected)) {
        selection.api.choose(state.selected, { reason: 'restore' });
      } else {
        selection.api.clear();
      }
      updateDetail();
      if (focusedStepButton && document.activeElement !== next && document.activeElement !== prev) {
        (!next.disabled ? next : prev).focus({ preventScroll: true });
      } else if (focusedStepButton && document.activeElement?.disabled) {
        (!next.disabled ? next : prev).focus({ preventScroll: true });
      }
    }
    function change(state, persist) {
      if (interaction) interaction.set(state);
      else applyInteraction(state);
      if (persist) save();
    }
    function seek(changed, persist) {
      if (changed !== step) change({ step: changed, selected: selection.get().keys[0] ?? null }, persist);
    }
    function go(offset) {
      seek(Math.max(0, Math.min(data.steps.length - 1, step + offset)), true);
    }
    function restore(snapshot) {
      const state = snapshot?.privateContent;
      if (state?.key !== root.id || !Number.isInteger(state.step) || state.step < 0 || state.step >= data.steps.length) return;
      change({ step: state.step, selected: typeof state.selected === 'string' ? state.selected : null }, false);
    }
    prev.addEventListener('click', () => go(-1));
    next.addEventListener('click', () => go(1));
    root.addEventListener('keydown', event => {
      if (event.altKey || event.ctrlKey || event.metaKey || event.shiftKey ||
          !(event.target.closest('[data-visual-object], [data-visual-step]') ||
          event.target === prev || event.target === next)) return;
      const isEvent = event.target.hasAttribute('data-event-step');
      const vertical = isEvent && ['ArrowUp', 'ArrowDown'].includes(event.key);
      if (!vertical && !['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) return;
      const move = stepFromKey(event, { arrows: vertical ? 'vertical' : 'horizontal',
        dir: getComputedStyle(root).direction });
      if (!move) return;
      event.preventDefault();
      const range = { min: 0, max: data.steps.length - 1, step: 1 };
      if (vertical) {
        const messages = [...event.target.closest('.ve-events').querySelectorAll('[data-event-step]')];
        const adjacent = messages[messages.indexOf(event.target) - move.direction];
        adjacent?.focus({ preventScroll: true });
        return;
      }
      const target = stepBy(step, move, range);
      const inScrubber = event.target.matches('[data-visual-step]');
      seek(target, true);
      if (inScrubber) scrubber.stops[step].focus({ preventScroll: true });
    });
    show();
    if (interaction) interaction.subscribe(applyInteraction);
    updateDetail();
    restore(window.openai?.widgetState);
    window.addEventListener('openai:set_globals', event => restore(event.detail?.globals?.widgetState));
  }

  function native() {
    const scene = source.scene ?? source.message.createSurface.dataModel.scene;
    host.dataset.renderer = 'native';
    const targets = Object.fromEntries(names.map(name => {
      const child = element('div');
      child.dataset['visual' + name[0].toUpperCase() + name.slice(1)] = '';
      host.append(child);
      return [name, child];
    }));
    setup(targets, scene);
  }

  if (!adapter || !source.message) {
    if (source.message) host.dataset.a2uiFallback = 'true';
    native();
  } else {
    adapter.mount(host, source.message).then(({ targets, scene, interaction }) => {
      host.dataset.renderer = 'a2ui';
      setup(targets, scene, interaction);
    }).catch(error => {
      console.warn('A2UI surface did not initialize', error.message);
      host.replaceChildren();
      host.dataset.a2uiFallback = 'true';
      native();
    });
  }
}
