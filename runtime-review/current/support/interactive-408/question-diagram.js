function drawQuestionDiagram(host, model, id, onStep, disclosure) {
  host.classList.add("diagram-workbench");
  const originalFrames = model.frames;
  const alternatives = model.scenarios || [];
  let scenario = 0;
  let paused = window.matchMedia?.("(prefers-reduced-motion: reduce)").matches || false;
  const ns = "http://www.w3.org/2000/svg";
  const svgElement = (parent, tag, attrs, value) => {
    const el = document.createElementNS(ns, tag);
    for (const [key, attr] of Object.entries(attrs)) el.setAttribute(key, attr);
    if (value !== undefined) el.textContent = value;
    parent.append(el);
    return el;
  };
  const element = (parent, tag, cls, value) => {
    const el = document.createElement(tag);
    el.className = cls;
    if (value !== undefined) el.textContent = value;
    parent.append(el);
    return el;
  };
  const controls = element(host, "div", "diagram-controls");
  const left = element(controls, "div", "diagram-nav");
  const button = (text, label) => {
    const el = element(left, "button", "btn", text);
    el.type = "button";
    el.setAttribute("aria-label", label);
    return el;
  };
  const prev = button("‹", "上一步");
  const next = button("›", "下一步");
  const slider = element(left, "input", "form-range");
  slider.type = "range";
  slider.min = "0";
  slider.max = String(model.frames.length - 1);
  slider.setAttribute("aria-label", "当前步骤");
  const stepLabel = element(left, "span", "diagram-stage");
  const count = element(controls, "output", "diagram-count");
  const delta = element(controls, "span", "diagram-delta text-small");
  const scroll = element(host, "div", "diagram-scroll");
  const localCaption = onStep ? null : element(host, "p", "diagram-local-caption");
  if (localCaption) localCaption.setAttribute("aria-live","polite");
  const measures = element(host, "div", "diagram-measures");
  const transport = element(controls, "div", "diagram-transport");
  transport.setAttribute("aria-label", "本步路径演示");
  const replay = element(transport, "button", "btn", "重播本步");
  const pause = element(transport, "button", "btn");
  replay.type = pause.type = "button";
  const pathControl = element(host, "nav", "diagram-rail");
  pathControl.setAttribute("aria-label", "按步骤查看机制");
  const stops = originalFrames.map((f,index) => {
    const stop = element(pathControl, "button", "btn diagram-stop");
    stop.type = "button";
    element(stop, "span", "diagram-stop-num", String(index+1));
    element(stop, "span", "diagram-stop-label", f.label || "第 "+(index+1)+" 步");
    stop.addEventListener("click", () => go(index));
    return stop;
  });
  pathControl.hidden = stops.length < 2;
  host.append(left);
  left.classList.add("diagram-navigation");
  const condition = element(host, "div", "diagram-condition");
  condition.hidden = !alternatives.length;
  if (alternatives.length) {
    controls.before(condition);
    element(condition, "span", "text-small", model.scenario_label || "条件");
    for (const [i,variant] of alternatives.entries()) {
      const option = element(condition, "button", "btn", variant.label);
      option.type = "button";
      option.addEventListener("click", () => { chooseScenario(i); paint(); save(); });
    }
  }
  function chooseScenario(i) {
    if (!Number.isInteger(i) || i<0 || i>=alternatives.length) return;
    scenario = i;
    model = {...model, frames: alternatives[i].frames};
    for (const [j,b] of [...condition.querySelectorAll("button")].entries()) b.setAttribute("aria-pressed",String(i===j));
  }
  chooseScenario(0);
  let step = model.initial || 0;
  let selected = null;
  const countRead = model.frames.some(frame => Number.isInteger(frame.count));
  count.hidden = !countRead;
  controls.hidden = model.frames.length < 2 && !countRead;
  prev.hidden = next.hidden = slider.hidden = model.frames.length < 2;

  let refreshCopyField;

  function paint() {
    if (!host.isConnected) return;
    refreshCopyField?.();
    const available = host.clientWidth || 680;
    const minWidth = model.illustrated ? 280 : model.nodes.length > 6 ? 520 : 360;
    const sceneWidth = Math.min(790, Math.max(available, minWidth));
    const sceneHeight = model.height;
    const pad = model.bands ? 0 : 73;
    const padY = model.bands ? 0 : 51;
    const frame = model.frames[step];
    const frameEdges = new Set(frame.edges || []);
    const past = new Set(model.keep_path_history ? model.frames.slice(0,step).flatMap(item=>item.edges || []) : []);
    const links = frame.visible_edges ? model.edges.filter(edge => frame.visible_edges.includes(edge.id) || past.has(edge.id)) : model.edges;
    const hidden = new Set(frame.hidden || []);
    const highlight = new Set(frame.active || []);
    const diagramRead = new Set(frame.reads || []);
    const focused = host.querySelector(":focus[data-object]")?.dataset.object;
    if (selected) {
      highlight.add(selected);
      for (const edge of links) {
        if (edge.from !== selected && edge.to !== selected) continue;
        highlight.add(edge.from);
        highlight.add(edge.to);
        frameEdges.add(edge.id);
      }
    }
    scroll.replaceChildren();
    const svg = svgElement(scroll, "svg", {
      class: "diagram-scene", width: sceneWidth, height: sceneHeight,
      viewBox: "0 0 " + sceneWidth + " " + sceneHeight, role: "group", "aria-label": "机制图",
    });
    for (const [i,stop] of stops.entries()) {
      if (i===step) stop.setAttribute("aria-current","step"); else stop.removeAttribute("aria-current");
      stop.toggleAttribute("data-before",i<step);
    }
    let moving = false;
    const defs = svgElement(svg, "defs", {});
    const arrowId = id + "-arrow";
    const marker = svgElement(defs, "marker", {id: arrowId, viewBox: "0 0 10 8", refX: 9, refY: 4,
      markerWidth: 6, markerHeight: 6, orient: "auto"});
    svgElement(marker, "path", {d: "M0 0L10 4L0 8Z", class: "diagram-arrow"});
    for (const {label, top, bottom} of model.bands || []) {
      svgElement(svg, "rect", {x:1, y:top * sceneHeight, width:sceneWidth - 2,
        height:(bottom - top) * sceneHeight, rx:10, class:"diagram-zone"});
      svgElement(svg, "text", {x:16, y:top * sceneHeight + 20, class:"diagram-zone-title"}, label);
    }
    const xy = new Map(model.nodes.map(node => {
      const [x,y] = frame.positions?.[node.id] || [node.x,node.y];
      return [node.id, {x:pad+(sceneWidth-pad*2)*x, y:padY+(sceneHeight-padY*2)*y}];
    }));
    const nodeWidth = node => {
      const here = xy.get(node.id);
      const local = [...xy.entries()].filter(([other, position]) => other !== node.id &&
                      Math.abs(position.y - here.y) < 48).map(([, position]) => Math.abs(position.x - here.x));
      const estimate = node.label.length * (/[\u3000-\u9fff]/.test(node.label) ? 13 : 7.3) + 28;
      const preferred = model.illustrated ? Math.max(estimate,sceneWidth>=520 ? 105 : 65) : estimate;
      return Math.max(34, Math.min(preferred, 126, ...local.map(gap => gap - 11)));
    };
    const widths = new Map(model.nodes.map(node => [node.id, nodeWidth(node)]));
    if (model.space_labels) {
      const placed = [];
      const inReadingOrder = [...model.nodes].sort((a,b) => a.y-b.y || a.x-b.x);
      for (const node of inReadingOrder) {
        const pos = xy.get(node.id);
        const values = [node.value, ...model.frames.map(state => state.values?.[node.id])].filter(Boolean);
        const measure = value => [...value].reduce((n,ch) => n+(/[\u3400-\u9fff]/.test(ch) ? 13 : 9),0);
        const room = Math.max(30, Math.min(pos.x,sceneWidth-pos.x)*2-10);
        const valueWidth = Math.min(room, Math.max(0,...values.map(measure)));
        const width = Math.max(widths.get(node.id),valueWidth);
        for (const previous of placed) {
          if (Math.abs(pos.x-previous.x)<(previous.width+width)/2+5) {
            pos.y = Math.max(pos.y,previous.y+(previous.hasValue?58:43));
          }
        }
        placed.push({x:pos.x,y:pos.y,width,hasValue:values.length>0});
      }
      const finalHeight = Math.max(sceneHeight, ...placed.map(row => row.y+51));
      svg.setAttribute("height",finalHeight);
      svg.setAttribute("viewBox","0 0 "+sceneWidth+" "+finalHeight);
    }
    function shapeEdge(pos, x, y, width) {
      const scale = 1 / Math.max(Math.abs(x) / (width/2 + 3), Math.abs(y) / 20, 1);
      return [pos.x + x * scale, pos.y + y * scale];
    }
    for (const edge of links) {
      if (hidden.has(edge.from) || hidden.has(edge.to)) continue;
      const a = xy.get(edge.from), b = xy.get(edge.to);
      const dx = b.x - a.x, dy = b.y - a.y;
      const active = frameEdges.has(edge.id);
      const [x1,y1] = shapeEdge(a,dx,dy,widths.get(edge.from));
      const [x2,y2] = shapeEdge(b,-dx,-dy,widths.get(edge.to));
      const bend = edge.lane * sceneWidth;
      const reverse = model.edges.some(peer => peer.from === edge.to && peer.to === edge.from && peer.arrow);
      const span = Math.hypot(dx,dy) || 1;
      const cx = (x1+x2)/2 - dy*19/span;
      const cy = (y1+y2)/2 + dx*19/span;
      const straight = reverse && edge.arrow
        ? "M" + x1 + " " + y1 + " Q" + cx + " " + cy + " " + x2 + " " + y2
        : "M" + x1 + " " + y1 + " L" + x2 + " " + y2;
      const loop = "M" + (a.x-14) + " " + (a.y-9) + " C" + (a.x-59) + " " + (a.y-61) +
                   " " + (a.x+57) + " " + (a.y-61) + " " + (a.x+14) + " " + (a.y-9);
      const path = edge.from === edge.to ? loop : edge.lane
        ? "M" + a.x + " " + y1 + " H" + bend + " V" + y2 + " H" + x2
        : straight;
      svgElement(svg, "path", {d:path, class:"diagram-wire", "data-connection":edge.id,
        ...(edge.role ? {"data-operation":edge.role} : {}),
        ...(past.has(edge.id) && !active ? {"data-history":""} : {}),
        ...(active ? {"data-active":""} : {"data-inactive":""}),
        ...(edge.arrow ? {"marker-end":"url(#" + arrowId + ")"} : {})});
      // Replay only a directed transfer explicitly active in this public frame.
      if (edge.arrow && frame.edges?.includes(edge.id)) {
        const attrs={class:"diagram-packet","aria-hidden":"true","data-operation":edge.role || "transfer"};
        const token=edge.role==="address"
          ? svgElement(svg,"rect",{...attrs,x:-3,y:-3,width:6,height:6})
          : svgElement(svg,"circle",{...attrs,r:4});
        svgElement(token,"animateMotion",{path,dur:"1.45s",fill:"freeze",repeatCount:"1"});
        moving=true;
      }
      const annotation = frame.edge_labels?.[edge.id] ?? edge.label;
      if (annotation && (!model.bands || active)) {
        const lx = edge.lane ? bend + 25 : (x1 + x2) / 2;
        const ly = (y1 + y2) / 2 - 7;
        svgElement(svg, "text", {x:lx, y:ly, "text-anchor":"middle", class:"diagram-edge-label"}, annotation);
      }
    }
    for (const node of model.nodes) {
      if (hidden.has(node.id)) continue;
      const {x,y} = xy.get(node.id), w = widths.get(node.id);
      const value = frame.values?.[node.id] ?? node.value;
      const wasRead = diagramRead.has(node.id);
      const group = svgElement(svg, "g", {transform:"translate("+x+" "+y+")",
        role:"button", class:"diagram-node", tabindex:0, "data-object":node.id,
        "aria-label":[node.label, value, wasRead ? "已读盘" : ""].filter(Boolean).join("，"),
        "aria-pressed":String(selected === node.id),
        ...(selected === node.id ? {"data-selected":""} : {}),
        ...(highlight.has(node.id) ? {"data-active":""} : {}),
        ...(wasRead ? {"data-read":""} : {})});
      if (model.illustrated && node.shape === "disk") {
        const radius = Math.max(22, w/2);
        svgElement(group,"path",{class:"body",d:`M${-radius} -8a${radius} 8 0 0 1 ${2*radius} 0v18a${radius} 8 0 0 1 ${-2*radius} 0z`});
        svgElement(group,"ellipse",{class:"diagram-object-face",cx:0,cy:-8,rx:radius,ry:8});
      } else if (model.illustrated && node.shape === "folder") {
        svgElement(group,"path",{class:"body",d:`M${-w/2} -16h${w*.37}l5 5h${w*.63-5}v25h${-w}z`});
      } else if (model.illustrated && (node.shape === "table" || node.shape === "file")) {
        svgElement(group,"path",{class:"body",d:`M${-w/2} -12l8 -6h${w}v25l-8 6h${-w}z`});
        svgElement(group,"path",{class:"diagram-object-face",d:`M${-w/2} -12h${w}l8 -6M${w/2} -12v25`});
      } else if (node.shape === "circle") {
        svgElement(group,"circle",{class:"body",cx:0,cy:0,r:Math.min(w/2,21)});
      } else {
        svgElement(group,"rect",{class:"body",x:-w/2,y:-17,width:w,height:34,rx:node.shape==="disk"?11:5});
      }
      const icons = {
        folder:"M-6 3V-5H-1L1-3H7V5H-6Z",
        file:"M-5-7H2L6-3V7H-5Z M2-7V-3H6 M-2 0H3 M-2 3H3",
        disk:"M-7-4C-7-9 7-9 7-4V4C7 9-7 9-7 4Z M-7-4C-7 1 7 1 7-4",
        table:"M-7-6H7V6H-7Z M-7-2H7 M-7 2H7 M-2-2V6",
      };
      const glyph = model.illustrated ? null : icons[node.shape];
      if (glyph) svgElement(group,"path",{class:"diagram-icon",d:glyph,transform:"translate("+(-w/2+12)+" 0)"});
      const label = svgElement(group,"text",{x:glyph ? 8 : 0,y:model.illustrated && node.shape==="disk" ? 10 : 4,"text-anchor":"middle",class:"diagram-name"},node.label);
      const usable = w - (glyph ? 20 : 8);
      if (label.getComputedTextLength() > usable) {
        label.setAttribute("textLength", String(usable));
        label.setAttribute("lengthAdjust","spacingAndGlyphs");
      }
      if (value || wasRead) {
        const label = svgElement(group,"text",{x:0,y:30,"text-anchor":"middle",class:"diagram-value"},
          value || "已读盘");
        const room = Math.max(30, Math.min(x, sceneWidth-x) * 2 - 10);
        if (label.getComputedTextLength() > room) {
          label.setAttribute("textLength", String(room));
          label.setAttribute("lengthAdjust","spacingAndGlyphs");
        }
      }
      const pick = () => { selected = selected === node.id ? null : node.id; paint(); save(); };
      group.addEventListener("click",pick);
      group.addEventListener("keydown",event => {
        if (event.key === "Enter" || event.key === " ") {
          event.preventDefault();
          pick();
        }
      });
      if (focused === node.id) group.focus();
    }
    prev.disabled = step === 0;
    next.disabled = step === model.frames.length - 1;
    transport.hidden = !moving;
    controls.hidden = !moving && !countRead;
    pause.textContent = paused ? "播放" : "暂停";
    pause.setAttribute("aria-label",paused ? "播放本步路径" : "暂停本步路径");
    if (paused) svg.pauseAnimations?.();
    slider.value = String(step);
    stepLabel.textContent = frame.label || (model.frames.length > 1 ? (step+1) + "/" + model.frames.length : "");
    slider.setAttribute("aria-valuetext",stepLabel.textContent);
    count.hidden = !Number.isInteger(frame.count);
    if (!count.hidden) count.textContent = "读盘 " + frame.count + " 次";
    delta.hidden = count.hidden || step===0 || !Number.isInteger(model.frames[step-1].count);
    if (!delta.hidden) {
      const difference=frame.count-model.frames[step-1].count;
      delta.textContent = "本步 " + (difference>=0 ? "+" : "") + difference + " 次";
    }
    measures.replaceChildren();
    for (const [label,value] of frame.metrics || []) {
      const metric=element(measures, "span", "diagram-measure", label+" ");
      element(metric, "strong", "diagram-measure-value", value);
    }
    measures.hidden = !measures.childElementCount;
    if (localCaption) {
      localCaption.textContent = model.object_notes?.[selected] || frame.caption || "";
      localCaption.hidden = !localCaption.textContent;
    }
    onStep?.(frame, step, selected);
  }

  const go = index => {
    step = Math.max(0,Math.min(model.frames.length-1,index));
    selected = null;
    paint();
    save();
  };
  function snapshot() {
    if (!disclosure) return null;
    return Object.freeze({
      schema: disclosure.schema, identity: disclosure.identity, source_version: disclosure.source_version,
      view: disclosure.view, hint_level: disclosure.hint_level, content_sha256: disclosure.content_sha256,
      binding_sha256: disclosure.binding_sha256, state_id: disclosure.state_ids[step] || null,
      selected_node: selected, selected_edge: null
    });
  }
  const root = host.closest(".cs408-drawing");
  if (root && disclosure) Object.defineProperty(root, "cs408Selection", { value: snapshot, configurable:true });
  // This local control reads the same public frame the diagram paints.
  // It does not send a message or write widget/learning state.
  if (root && disclosure && model.template === "vm-address-path-v1" &&
      disclosure.state_ids.includes("tlb_miss_present")) {
    const group = element(host, "div", "diagram-copy");
    const copy = element(group, "button", "btn", "复制当前状态");
    copy.type = "button";
    copy.title = "复制本步已公开条件，可粘贴到当前聊天继续讨论";
    const status = element(group, "span", "text-small");
    status.setAttribute("role", "status");
    const field = element(group, "textarea", "diagram-copy-text");
    field.readOnly = true;
    field.rows = 5;
    field.hidden = true;
    field.setAttribute("aria-label", "可手动复制的当前状态");

    function summary() {
      const state = snapshot();
      const frame = model.frames[step];
      if (!frame?.id || state.state_id !== frame.id) return "";
      const lines = [
        "场景 " + model.template + " · " + state.identity,
        "版本 " + state.source_version,
        "公开阶段 " + state.view + " / L" + state.hint_level,
        "当前步骤 " + state.state_id + " · " + frame.label,
      ];
      for (const id of ["tlb", "pte"]) {
        const value = frame.values?.[id];
        if (typeof value === "string" && frame.active?.includes(id) && !frame.hidden?.includes(id)) {
          const node = model.nodes.find(node => node.id === id);
          if (node) lines.push(node.label + "：" + value);
        }
      }
      if (state.selected_node && !frame.hidden?.includes(state.selected_node)) {
        const node = model.nodes.find(node => node.id === state.selected_node);
        if (node) lines.push("选中对象 " + node.label);
      }
      if (frame.caption) lines.push(frame.caption);
      return lines.join("\n");
    }

    refreshCopyField = () => {
      if (field.hidden) status.textContent = "";
      else field.value = summary();
    };
    copy.addEventListener("click", async () => {
      const value = summary();
      if (!value) return;
      try {
        await navigator.clipboard.writeText(value);
        if (summary() === value) {
          field.hidden = true;
          status.textContent = "已复制，可粘贴到聊天。";
        }
      } catch {
        field.value = summary();
        field.hidden = false;
        field.focus();
        field.select();
        status.textContent = "请复制下方已选文字。";
      }
    });
  }
  function save() {
    // Topic cards control their own selection state.
    if (!root) return;
    const state = snapshot();
    const result = window.openai?.setWidgetState?.({privateContent:{diagram:id, step, selected, state, scenario, paused},
      ...(state ? {modelContent:{visual_selection:state, interaction_is_not_learning_evidence:true}} : {})});
    result?.catch(() => {});
  }
  function restore(state) {
    const saved = state?.privateContent;
    if (saved?.diagram !== id) return;
    if (disclosure && saved.state?.binding_sha256 !== disclosure.binding_sha256) return;
    chooseScenario(saved.scenario);
    if (typeof saved.paused==="boolean") paused=saved.paused;
    const index = disclosure ? disclosure.state_ids.indexOf(saved.state.state_id) : saved.step;
    if (!Number.isInteger(index) || index < 0 || index >= model.frames.length) return;
    step = index;
    const nodeId = disclosure ? saved.state.selected_node : saved.selected;
    selected = model.nodes.some(node => node.id === nodeId && !model.frames[step].hidden?.includes(nodeId)) ? nodeId : null;
    paint();
  }
  prev.addEventListener("click",() => go(step-1));
  next.addEventListener("click",() => go(step+1));
  slider.addEventListener("input",() => go(Number(slider.value)));
  replay.addEventListener("click",() => {
    const svg=scroll.querySelector("svg");
    if (!svg) return;
    svg.setCurrentTime?.(0);
    if (paused) svg.pauseAnimations?.(); else svg.unpauseAnimations?.();
    save();
  });
  pause.addEventListener("click",() => {
    const svg=scroll.querySelector("svg");
    if (!svg) return;
    paused=!paused; if(paused) svg.pauseAnimations?.(); else svg.unpauseAnimations?.();
    pause.textContent=paused?"播放":"暂停";
    pause.setAttribute("aria-label",paused?"播放本步路径":"暂停本步路径");save();
  });
  host.onkeydown = event => {
    if (event.altKey || event.ctrlKey || event.metaKey || event.target.matches("input,select,textarea")) return;
    if (event.key==="ArrowRight" || event.key==="ArrowLeft") {
      event.preventDefault();go(step+(event.key==="ArrowRight"?1:-1));
    } else if (event.key.toLowerCase()==="r" && !transport.hidden) {
      event.preventDefault();replay.click();
    }
  };
  const resize = new ResizeObserver(() => {
    if (host.isConnected) paint();
    else resize.disconnect();
  });
  resize.observe(host);
  paint();
  restore(window.openai?.widgetState);
  if (host.closest(".cs408-drawing")) {
    window.addEventListener("openai:set_globals",event=>restore(event.detail?.globals?.widgetState));
  }
}

function drawSerialFrame(host, model) {
  const frame = document.createElement("div");
  frame.className = "serial-frame";
  frame.setAttribute("role","group");
  frame.setAttribute("aria-label","按真实位宽排列的字段");
  frame.style.gridTemplateColumns = model.bits.map(field => field.width + "fr").join(" ");
  host.append(frame);
  for (const field of model.bits) {
    const button = document.createElement("button");
    button.type = "button";
    button.dataset.field = field.key;
    button.setAttribute("aria-pressed","false");
    const label = document.createElement("span");
    label.textContent = field.label;
    const size = document.createElement("small");
    size.textContent = field.width + " 位";
    button.append(label,size);
    frame.append(button);
    button.addEventListener("click",() => {
      const selected = button.getAttribute("aria-pressed") !== "true";
      for (const peer of frame.children) peer.setAttribute("aria-pressed",String(peer === button && selected));
    });
  }
  const total = document.createElement("output");
  total.className = "diagram-total";
  total.textContent = model.width + " 位";
  host.append(total);
}

function drawInterruptTiming(host, model) {
  const ns = "http://www.w3.org/2000/svg";
  const svg = document.createElementNS(ns,"svg");
  svg.setAttribute("viewBox","0 0 630 186");
  svg.setAttribute("class","diagram-timing");
  svg.setAttribute("role","img");
  svg.setAttribute("aria-label","外设准备与 CPU 服务的重叠时序，分段示意");
  host.append(svg);
  const add = (tag, attrs, value) => {
    const element = document.createElementNS(ns,tag);
    for (const [key, value] of Object.entries(attrs)) element.setAttribute(key,value);
    if (value !== undefined) element.textContent = value;
    svg.append(element);
    return element;
  };
  const controls = document.createElement("div");
  controls.className = "timing-choices";
  host.insertBefore(controls, svg);
  let selected = 0;
  const steps = model.busy_only ? ["每轮","累加忙时"] : ["相邻两轮","重叠","末次"];
  for (const [index,value] of steps.entries()) {
    const button = document.createElement("button");
    button.type = "button";
    button.textContent = value;
    button.setAttribute("aria-pressed",String(selected === index));
    button.addEventListener("click",() => {
      selected = index;
      for (const [i,peer] of [...controls.children].entries()) peer.setAttribute("aria-pressed",String(i === selected));
      paint();
    });
    controls.append(button);
  }
  function paint() {
    svg.replaceChildren();
    const x0 = 70, response = 267, front = 320, tail = 399, endTail = 463, next = 605;
    const rail = (from,to,y,value,kind,on) => {
      add("rect",{x:from,y,width:to-from,height:31,rx:4,class:"timing-span "+kind,
        ...(on ? {"data-active":""} : {})});
      add("text",{x:(from+to)/2,y:y+20,"text-anchor":"middle"},value);
    };
    add("text",{x:16,y:40},"设备 D");
    add("text",{x:16,y:94},"CPU");
    rail(x0,response,22,model.device,"device",!model.busy_only && selected === 0);
    if (selected !== 2 || model.busy_only) rail(tail,next,22,"下一字符","device",!model.busy_only && selected === 1);
    rail(response,front,76,model.response,"cpu",selected !== 2);
    rail(front,tail,76,model.front,"cpu",selected !== 2);
    rail(tail,endTail,76,model.tail,"cpu",model.busy_only || selected >= 1);
    add("line",{x1:tail,x2:tail,y1:54,y2:76,class:"diagram-wire","data-active":""});
    for (const [x,text] of [[(response+front)/2,"响应"],[(front+tail)/2,"前 15 条"],[(tail+endTail)/2,"后 5 条"]]) {
      add("text",{x,y:125,"text-anchor":"middle",class:"diagram-value"},text);
    }
    add("line",{x1:x0,x2:next,y1:142,y2:142,class:"diagram-wire"});
    add("text",{x:x0,y:164,class:"diagram-zone-title"},"时间 →");
    add("text",{x:next,y:164,"text-anchor":"end",class:"diagram-name"},
      selected === steps.length-1 ? model.total : model.last);
  }
  paint();
}

function drawTimeline(host, model) {
  const ns = "http://www.w3.org/2000/svg";
  const scroll = document.createElement("div");
  scroll.className = "diagram-scroll";
  host.append(scroll);
  const plot = document.createElementNS(ns, "svg");
  const width = 720, left = 128, right = width - 26;
  const height = 63 + 40 * model.lanes.length;
  const x = point => left + (right-left) * (point-model.start) / (model.end-model.start);
  plot.setAttribute("viewBox", `0 0 ${width} ${height}`);
  plot.setAttribute("class", "question-timeline");
  plot.setAttribute("role", "img");
  plot.setAttribute("aria-label", model.title);
  scroll.append(plot);
  const add = (tag, attributes, text) => {
    const el = document.createElementNS(ns, tag);
    for (const [key, value] of Object.entries(attributes)) el.setAttribute(key, value);
    if (text !== undefined) el.textContent = text;
    plot.append(el);
  };
  add("text", {x:12,y:25}, model.axis);
  for (const [point, label] of model.ticks) {
    add("line", {x1:x(point),x2:x(point),y1:33,y2:height-22,class:"wave-boundary"});
    add("text", {x:x(point),y:24,"text-anchor":"middle"}, label);
  }
  for (const [index, {label, spans}] of model.lanes.entries()) {
    const y = 34 + index*40;
    add("text", {x:8,y:y+20,class:"diagram-name"}, label);
    add("line", {x1:left,x2:right,y1:y+30,y2:y+30,class:"diagram-wire"});
    for (const [start,end,value,tone] of spans) {
      add("rect", {x:x(start)+1,y:y+2,width:x(end)-x(start)-2,height:26,rx:3,
                   class:"timeline-block", "data-tone":tone});
      add("text", {x:(x(start)+x(end))/2,y:y+20,"text-anchor":"middle"}, value);
    }
  }
}

function drawWaveforms(host, model) {
  const ns = "http://www.w3.org/2000/svg";
  const picks = document.createElement("div");
  picks.className = "wave-picks";
  picks.setAttribute("role","group");
  picks.setAttribute("aria-label",model.title);
  host.append(picks);
  const plot = document.createElementNS(ns,"svg");
  const width = 640, left = 20, cell = (width-40)/model.bits.length, height = model.traces.length * 112;
  plot.setAttribute("class","wave-plot");
  plot.setAttribute("viewBox",`0 0 ${width} ${height}`);
  plot.setAttribute("role","img");
  plot.setAttribute("aria-label",model.title);
  host.append(plot);
  const add = (tag, attrs, value) => {
    const element = document.createElementNS(ns,tag);
    for (const [key, value] of Object.entries(attrs)) element.setAttribute(key,value);
    if (value !== undefined) element.textContent = value;
    plot.append(element);
  };
  let selected = 0;
  function draw() {
    plot.replaceChildren();
    for (const [row,trace] of model.traces.entries()) {
      const y0 = row*112;
      add("text",{x:left,y:y0+20,class:"diagram-name"},trace.label);
      add("rect",{x:left+cell*selected,y:y0+24,width:cell,height:78,rx:3,class:"wave-bit"});
      const level = value => y0+(value ? 48 : 80);
      let path = "";
      for (const [index,pair] of trace.levels.entries()) {
        const x = left+index*cell;
        add("line",{x1:x,y1:y0+32,x2:x,y2:y0+96,class:"wave-boundary"});
        add("line",{x1:x+cell/2,y1:y0+33,x2:x+cell/2,y2:y0+96,class:"wave-mid"});
        path += (index ? " V" + level(pair[0]) : "M" + x + " " + level(pair[0])) +
          " H" + (x+cell/2) + " V" + level(pair[1]) + " H" + (x+cell);
      }
      add("line",{x1:width-left,y1:y0+32,x2:width-left,y2:y0+96,class:"wave-boundary"});
      const cursor = left+cell*(selected+(model.mode==="middle"?.5:0));
      add("line",{x1:cursor,y1:y0+26,x2:cursor,y2:y0+99,class:"wave-cursor"});
      add("path",{d:path,class:"wave-line"});
    }
    for (const [index,button] of [...picks.children].entries()) {
      button.setAttribute("aria-pressed",String(index===selected));
    }
  }
  for (const [index,bit] of model.bits.entries()) {
    const button = document.createElement("button");
    button.type = "button";
    button.textContent = bit;
    button.setAttribute("aria-label",`第 ${index+1} 位，${bit}`);
    button.addEventListener("click",() => {selected=index;draw();});
    picks.append(button);
  }
  draw();
}
