function renderMechanism(panel, visual, markerId) {
  const ns = 'http://www.w3.org/2000/svg';
  const width = Math.max(260, Math.min(690, panel.clientWidth || 620));
  const add = (parent, tag, attributes, text) => {
    const el = document.createElementNS(ns, tag);
    for (const [key, value] of Object.entries(attributes)) el.setAttribute(key, value);
    if (text !== undefined) el.textContent = text;
    parent.append(el);
    return el;
  };
  function svg(height, label) {
    const picture = add(panel, 'svg', {viewBox:`0 0 ${width} ${height}`, width, height,
      role:'img', 'aria-label':label, class:'mechanism-svg', 'data-kind':visual.kind});
    const defs = add(picture, 'defs', {});
    const marker = add(defs, 'marker', {id:markerId, viewBox:'0 0 9 8',
      refX:8, refY:4, markerWidth:7, markerHeight:7, orient:'auto'});
    add(marker,'path',{d:'M0 0 L9 4 L0 8 Z',class:'mechanism-arrowhead'});
    return picture;
  }
  function line(picture,x1,y1,x2,y2,arrow=false,attributes={}) {
    return add(picture,'line',{x1,y1,x2,y2,class:'mechanism-line',
      ...(arrow ? {'marker-end':`url(#${markerId})`} : {}),...attributes});
  }
  function route(picture,d,arrow=true,attributes={}) {
    return add(picture,'path',{d,class:'mechanism-line',
      ...(arrow ? {'marker-end':`url(#${markerId})`} : {}),...attributes});
  }
  function label(picture,x,y,value,attributes={}) {
    return add(picture,'text',{x,y,'text-anchor':'middle',...attributes},value);
  }
  function fitLabel(text) {
    const bounds=text.getBBox();
    const shift=Math.max(4-bounds.x,0)-Math.max(bounds.x+bounds.width-(width-4),0);
    if (shift) text.setAttribute('x',Number(text.getAttribute('x'))+shift);
  }
  function box(picture,x,y,w,h,extra={}) {
    return add(picture,'rect',{x,y,width:w,height:h,class:'mechanism-box',...extra});
  }
  function sequence() {
    const height = 63 + visual.messages.length * 63;
    const picture = svg(height, '时间向下的消息时序');
    const start = 44, span = width - 86;
    const positions = visual.actors.map((_,index) => start + span*index/(visual.actors.length-1));
    line(picture,15,54,15,height-13,true,{'data-time-axis':'down'});
    label(picture,15,38,'时');
    for (const [index, actor] of visual.actors.entries()) {
      const x = positions[index];
      fitLabel(label(picture,x,22,actor,{'data-actor':index}));
      line(picture,x,32,x,height-12,false,{'data-lifeline':index});
    }
    for (const [index, message] of visual.messages.entries()) {
      const x1=positions[message.from],x2=positions[message.to],y=84+index*63;
      const direction=Math.sign(x2-x1), middle=(x1+x2)/2;
      const end=message.lost?x1+(x2-x1)*.72:x2-direction*3;
      line(picture,x1+direction*2,y,end,y+8,!message.lost,{
        'data-event':index,'data-from':message.from,'data-to':message.to,
        ...(visual.step===index?{'data-current-step':'true'}:{}),
        ...(message.lost?{'data-loss':'true','stroke-dasharray':'4 3'}:{})});
      if (message.lost) label(picture,end,y+12,'×');
      label(picture,middle,y-(message.fields?26:10),message.label,{'data-signal':index});
      if (message.fields) {
        const fields=label(picture,middle,y-10,undefined,{'data-fields':index,class:'mechanism-fields','xml:space':'preserve'});
        for (const [position,value] of message.fields.split(/  +/).entries()) {
          if (position) add(fields,'tspan',{},'  ');
          const focused=(visual.step===undefined||visual.step===index)
            &&value.toLowerCase().startsWith((visual.highlight||'')+'=');
          add(fields,'tspan',focused?{'data-field-emphasis':''}:{},value);
        }
      }
    }
  }
  function address() {
    const picture=svg(263,'有效分页映射。虚页号查页表，页框号进入物理地址，页内偏移原样保留。');
    const x0=26, x1=width-26, bits=visual.widths;
    const vmid=x0+(x1-x0)*(bits?(bits.va-bits.offset)/bits.va:.56);
    const pmid=x0+(x1-x0)*(bits?(bits.pa-bits.offset)/bits.pa:.56);
    const vhigh=(x0+vmid)/2, phigh=(x0+pmid)/2, voffset=(vmid+x1)/2, poffset=(pmid+x1)/2;
    const tableLeft=x0+22, tableRight=Math.min(x0+(x1-x0)*.65,voffset-18,poffset-18), tableMid=(tableLeft+tableRight)/2;
    const pfn=(tableMid+tableRight)/2;
    const focus=(key)=>visual.highlight===key?{'data-current-step':'true'}:{};
    route(picture,`M${vhigh} 65 V79 H${x0} V128 H${tableLeft-2}`,true,{'data-map-route':'vpn-to-pte',...focus('vpn')});
    route(picture,`M${pfn} 145 V178 H${phigh} V205`,true,{'data-map-route':'pfn-to-pa',...focus('pfn')});
    route(picture,`M${voffset} 65 V176 H${poffset} V205`,true,{'data-map-route':'offset','stroke-dasharray':'4 4',...focus('offset')});
    fitLabel(label(picture,voffset+8,160,'原样保留',{'text-anchor':'start',class:'mechanism-small'}));
    label(picture,x0,20,'虚拟地址 VA'+(bits?' · '+bits.va+' 位':''),{'text-anchor':'start'});
    label(picture,x0,199,'物理地址 PA'+(bits?' · '+bits.pa+' 位':''),{'text-anchor':'start'});
    for (const [top,left,mid,high,offset,total] of [[30,'虚页号 VPN',vmid,vhigh,voffset,bits?.va],[208,'页框号 PFN',pmid,phigh,poffset,bits?.pa]]) {
      box(picture,x0,top,mid-x0,35,{'data-address-field':left});
      box(picture,mid,top,x1-mid,35,{'data-address-field':'页内偏移',class:'mechanism-box mechanism-offset'});
      const highLabel=mid-x0<95?left.slice(-3):left;
      if (mid-x0>=40) label(picture,high,top+(bits?15:22),highLabel);
      if (x1-mid>=36) label(picture,offset,top+(bits?15:22),x1-mid<55?'偏移':'页内偏移');
      if (bits) {
        if (mid-x0>=40) label(picture,high,top+29,(total-bits.offset)+' 位',{class:'mechanism-small'});
        if (x1-mid>=36) label(picture,offset,top+29,bits.offset+' 位',{class:'mechanism-small'});
      }
    }
    label(picture,tableLeft,98,'页表项',{'text-anchor':'start',class:'mechanism-small'});
    for (const [start,end,name,value] of [[tableLeft,tableMid,'存在位','1'],[tableMid,tableRight,'页框号','PFN']]) {
      box(picture,start,105,end-start,20);
      box(picture,start,125,end-start,22,{class:'mechanism-box mechanism-offset'});
      label(picture,(start+end)/2,119,name,{class:'mechanism-small'});
      label(picture,(start+end)/2,140,value,{class:'mechanism-fields'});
    }
    label(picture,(tableLeft+tableRight)/2,163,'已驻留且权限通过',{class:'mechanism-small'});
    const footer=bits?'VPN '+(bits.va-bits.offset)+' 位 · PFN '+(bits.pa-bits.offset)+' 位 · 偏移 '+bits.offset+' 位':
      '位宽按题设，本图示意字段路径。';
    label(picture,x0,256,footer,{'text-anchor':'start',class:'mechanism-small'});
  }
  function graph() {
    const measure=document.createElement('canvas').getContext('2d');
    measure.font=getComputedStyle(panel).font || '14px system-ui';
    function wrap(value, maxWidth) {
      const lines=[];
      let current='';
      for (const character of value) {
        if (current && measure.measureText(current+character).width>maxWidth) {
          lines.push(current);
          current='';
        }
        current+=character;
      }
      lines.push(current);
      return lines;
    }
    const nodes=[];
    let top=6;
    for (const row of visual.rows) {
      const slot=width/row.length, w=Math.min(slot-12,166);
      const planned=row.map(value=>({value,lines:wrap(value,w-13)}));
      const h=Math.max(46,Math.max(...planned.map(node=>node.lines.length))*16+14);
      planned.forEach((node,index)=>nodes.push({...node,x:slot*(index+.5),y:top+h/2,w,h}));
      top+=h+34;
    }
    const picture=svg(top-28,'对象关系与连线');
    for (const edge of visual.edges) {
      const a=nodes[edge.from], b=nodes[edge.to], dx=b.x-a.x,dy=b.y-a.y;
      // Preserve a connected edge while clipping it to the visible object boundaries.
      const r1=Math.min(dx? (a.w/2+2)/Math.abs(dx):Infinity,dy?(a.h/2+2)/Math.abs(dy):Infinity);
      const r2=Math.min(dx? (b.w/2+2)/Math.abs(dx):Infinity,dy?(b.h/2+2)/Math.abs(dy):Infinity);
      const shift=visual.edges.some(other=>other.from===edge.to&&other.to===edge.from)?9:0;
      const distance=Math.hypot(dx,dy), sx=-dy/distance*shift,sy=dx/distance*shift;
      line(picture,a.x+dx*r1+sx,a.y+dy*r1+sy,b.x-dx*r2+sx,b.y-dy*r2+sy,edge.arrow,{'data-connection':`${edge.from}-${edge.to}`});
      if (edge.label) fitLabel(label(picture,(a.x+b.x)/2+sx+(dx===0?9:0),(a.y+b.y)/2+sy-6,edge.label,{
        class:'mechanism-small mechanism-edge-label',...(dx===0?{'text-anchor':'start'}:{})}));
    }
    for (const [index,node] of nodes.entries()) {
      box(picture,node.x-node.w/2,node.y-node.h/2,node.w,node.h,{rx:4,'data-object':index});
      const text=label(picture,node.x,node.y+5-(node.lines.length-1)*8,undefined,{'aria-label':node.value});
      node.lines.forEach((row,index)=>add(text,'tspan',{x:node.x,dy:index?16:0},row));
    }
  }
  const span = (name,content) => {
    const el=document.createElement('span');
    el.className=name;
    el.textContent=content;
    return el;
  };
  function flow() {
    const row=document.createElement('div');
    row.className='mechanism-flow';
    row.setAttribute('aria-label','过程图');
    visual.items.forEach((label,index)=>{
      const part=document.createElement('div');
      part.className='mechanism-transition';
      if (index) {
        const arrow=add(part,'svg',{width:21,height:13,viewBox:'0 0 21 13','aria-hidden':'true'});
        add(arrow,'path',{d:'M0 6.5 H19 M14 2 L19 6.5 L14 11',class:'mechanism-line'});
      }
      part.append(span('mechanism-stage',label));
      row.append(part);
    });
    panel.append(row);
  }
  function band() {
    const frame=document.createElement('div');
    frame.className='mechanism-band-frame';
    for (const values of visual.rows) {
      const row=document.createElement('div');
      row.className='mechanism-band';
      for (const cell of values) row.append(span('mechanism-cell',cell));
      frame.append(row);
    }
    panel.append(frame);
  }
  function partition() {
    const figure=document.createElement('div');
    figure.className='mechanism-bitfigure';
    figure.setAttribute('aria-label',visual.title);
    const strip=document.createElement('div');
    strip.className='mechanism-band-frame mechanism-partition';
    for (const value of visual.fields) strip.append(span('mechanism-cell',value));
    figure.append(strip,span('mechanism-field-note','字段顺序示意，位宽按题设。'));
    panel.append(figure);
  }
  function fields() {
    const figure=document.createElement('figure');
    figure.className='mechanism-bitfigure';
    figure.setAttribute('aria-label',visual.title+'的字段布局');
    const caption=document.createElement('figcaption');
    const amount=visual.fixed_bits%8===0 ? visual.fixed_bits/8+' B' : visual.fixed_bits+' 位';
    caption.textContent=visual.group+' '+amount;
    figure.append(caption);
    const ruler=document.createElement('div');
    ruler.className='mechanism-ruler';
    ruler.append(span('mechanism-byte','位序'));
    const scale=document.createElement('div');
    scale.className='mechanism-bit-scale';
    const labels=visual.word_bits===32?[0,8,16,24,31]:
      [0,Math.floor(visual.word_bits/2),visual.word_bits-1];
    for (const [index,position] of [...new Set(labels)].entries()) {
      const tick=span('mechanism-tick',String(position));
      tick.style.left=(position/visual.word_bits*100)+'%';
      tick.dataset.edge=index===0?'first':position===visual.word_bits-1?'last':'';
      scale.append(tick);
    }
    ruler.append(scale);
    figure.append(ruler);
    const outer=document.createElement('div');
    outer.className='mechanism-packet';
    const fixed=document.createElement('div');
    fixed.className='mechanism-fixed';
    const brace=add(fixed,'svg',{viewBox:'0 0 12 100',preserveAspectRatio:'none',class:'mechanism-brace','aria-hidden':'true'});
    add(brace,'path',{d:'M1 1 Q7 1 7 10 V42 Q7 50 11 50 Q7 50 7 58 V90 Q7 99 1 99',
      class:'mechanism-line','vector-effect':'non-scaling-stroke'});
    const detail=document.createElement('p');
    detail.className='mechanism-field-detail';
    detail.setAttribute('aria-live','polite');
    const controls=[];
    const tiny=[];
    const flagControls=[];
    function choose(field) {
      for (const [item,control] of controls) control.setAttribute('aria-pressed',String(item.key===field.key));
      for (const [flag,control] of flagControls) control.setAttribute('aria-pressed',String(flag.key===field.key));
      const end=field.start+field.bits-1;
      const range=field.bits===1?String(field.start):field.start+'–'+end;
      detail.textContent=field.label+' · 整体位 '+range+' · '+field.bits+' 位'+
        (visual.tips[field.key]?'。'+visual.tips[field.key]:'');
    }
    for (const row of visual.rows) {
      const line=document.createElement('div');
      line.className='mechanism-bitrow';
      const start=row.fields[0].start;
      const words=row.bits/visual.word_bits;
      const position=words>1 ? start/8+'–'+((start+row.bits)/8-1)+' B' :
        start%8===0 ? start/8+' B' : start+' 位';
      line.append(span('mechanism-byte',position));
      const grid=document.createElement('div');
      grid.className='mechanism-fields-grid';
      grid.style.gridTemplateColumns='repeat('+visual.word_bits+',minmax(0,1fr))';
      if (words>1) {
        grid.style.minHeight=words*1.7+'em';
        grid.dataset.wordCount=words;
      }
      for (const field of row.fields) {
        const button=document.createElement('button');
        button.type='button';
        button.className='mechanism-field';
        button.style.gridColumn='span '+Math.min(field.bits,visual.word_bits);
        button.dataset.field=field.key;
        button.dataset.start=field.start;
        button.dataset.bits=field.bits;
        button.setAttribute('aria-label',field.label+'，'+field.bits+' 位');
        if (visual.highlight.includes(field.key)||visual.flags.some(flag=>visual.highlight.includes(flag.key)
            &&flag.position>=field.start&&flag.position<field.start+field.bits)) button.dataset.focus='';
        if (field.bits<=3) {
          if (!visual.flags.some(flag=>flag.position>=field.start&&flag.position<field.start+field.bits)) {
            tiny.push(field);
          }
          button.append(span('mechanism-field-dot',''));
        } else {
          button.append(span('mechanism-field-label',field.label),span('mechanism-field-bits',field.bits+' 位'));
        }
        button.addEventListener('click',()=>choose(field));
        controls.push([field,button]);
        grid.append(button);
      }
      line.append(grid);
      fixed.append(line);
    }
    outer.append(fixed);
    for (const [name,width] of visual.tail) {
      const row=document.createElement('div');
      row.className='mechanism-packet-tail';
      row.append(span('mechanism-tail-name',name),span('mechanism-tail-width',width));
      outer.append(row);
    }
    figure.append(outer);
    if (visual.flags.length||tiny.length) {
      const legend=document.createElement('div');
      legend.className='mechanism-flag-ruler';
      for (const flag of visual.flags) {
        const cell=document.createElement('button');
        cell.type='button';
        cell.className='mechanism-flag';
        cell.textContent=flag.position%visual.word_bits+' '+flag.label;
        cell.title='整体位 '+flag.position+'，1 位';
        cell.dataset.bitPosition=flag.position;
        cell.dataset.field=flag.key;
        if (visual.highlight.includes(flag.key)) cell.dataset.focus='';
        const flagField={key:flag.key,label:flag.label+' 控制位',bits:1,start:flag.position};
        cell.addEventListener('click',()=>choose(flagField));
        flagControls.push([flagField,cell]);
        legend.append(cell);
      }
      for (const field of tiny) {
        legend.append(span('mechanism-flag',field.label+' '+field.bits+' 位'));
      }
      figure.append(legend);
    }
    if (visual.flags.length) figure.append(span('mechanism-field-note','控制位小格各占 1 位，数字为所在行的位编号。'));
    figure.append(detail);
    const notes=[visual.note,visual.source].filter(Boolean);
    for (const note of notes) figure.append(span('mechanism-field-note',note));
    panel.append(figure);
    const first=[...controls,...flagControls].find(([field])=>visual.highlight.includes(field.key))||controls[0];
    choose(first[0]);
  }
  function table() {
    const grid=document.createElement('table');
    grid.className='mechanism-table';
    grid.dataset.kind=visual.kind;
    grid.setAttribute('aria-label',visual.kind==='lanes'?'按时间对齐的活动':'字段与条目');
    for (const [r,values] of visual.rows.entries()) {
      const row=grid.insertRow();
      for (const [c,value] of values.entries()) {
        const cell=document.createElement(r===0||c===0?'th':'td');
        if (r===0||c===0) cell.scope=r===0?'col':'row';
        if (value==='·') cell.dataset.idle='';
        cell.textContent=value==='·'?'':value;
        row.append(cell);
      }
    }
    panel.append(grid);
  }
  function tree() {
    const root=document.createElement('div');
    root.className='mechanism-tree';
    root.append(span('mechanism-stage',visual.root));
    const branches=document.createElement('ul');
    for (const label of visual.children) {
      const li=document.createElement('li');
      li.textContent=label;
      branches.append(li);
    }
    root.append(branches);
    panel.append(root);
  }
  function communication() {
    const section = document.createElement('div');
    section.className = 'communication-lesson';
    const paragraph = (text, className = '') => {
      const p = document.createElement('p');
      p.textContent = text;
      p.className = className;
      section.append(p);
      return p;
    };
    for (const formula of visual.formulas) paragraph(formula, 'communication-formula');
    paragraph(visual.definitions);
    paragraph(visual.example, 'text-small');
    paragraph(visual.given);
    if (visual.steps) {
      const controls = document.createElement('div');
      controls.className = 'viz-row';
      const detail = document.createElement('p');
      detail.setAttribute('aria-live', 'polite');
      visual.steps.forEach((step, index) => {
        const button = document.createElement('button');
        button.type = 'button';
        button.className = 'btn';
        button.textContent = `${index + 1} ${step.title}`;
        button.addEventListener('click', () => {
          for (const peer of controls.children) peer.setAttribute('aria-pressed', String(peer === button));
          detail.textContent = step.detail;
        });
        button.setAttribute('aria-pressed', String(index === 0));
        controls.append(button);
      });
      detail.textContent = visual.steps[0].detail;
      section.append(controls, detail);
    }
    if (visual.extra_example) {
      const details = document.createElement('details');
      const summary = document.createElement('summary');
      summary.textContent = visual.extra_example.title;
      details.append(summary);
      for (const text of [visual.extra_example.given, ...visual.extra_example.steps]) {
        const p = document.createElement('p');
        p.textContent = text;
        details.append(p);
      }
      section.append(details);
    }
    if (visual.topic === 'differential') {
      const bits = visual.bits;
      let previous = visual.initial_level;
      const differential = bits.map(bit => {
        const first = bit === 0 ? 1 - previous : previous;
        previous = 1 - first;
        return [first, previous];
      });
      const manchester = bits.map(bit => bit === 1 ? [1, 0] : [0, 1]);
      const drawing = document.createElement('div');
      const controls = document.createElement('div');
      controls.className = 'viz-row';
      const detail = document.createElement('p');
      detail.setAttribute('aria-live', 'polite');
      const render = index => {
        drawing.replaceChildren();
        for (const [name, levels] of [['曼彻斯特', manchester], ['差分曼彻斯特', differential]]) {
          const title = document.createElement('p');
          title.textContent = name;
          drawing.append(title);
          const w = Math.max(280, Math.min(690, panel.clientWidth || 620));
          const picture = document.createElementNS(ns, 'svg');
          picture.setAttribute('viewBox', `0 0 ${w} 112`);
          picture.setAttribute('width', w);
          picture.setAttribute('height', 112);
          picture.setAttribute('class', 'mechanism-svg');
          picture.setAttribute('role', 'img');
          picture.setAttribute('aria-label', `${name}的八位波形，选中第${index + 1}位`);
          const left = 22, step = (w - 30) / bits.length;
          add(picture, 'rect', {x:left + index * step,y:10,width:step,height:90,
            fill:'var(--viz-series-1)',opacity:'.12'});
          const y = level => level ? 38 : 76;
          let path = `M ${left - 16} ${y(visual.initial_level)}`;
          levels.forEach((pair, i) => {
            const x = left + i * step;
            add(picture, 'line', {x1:x,y1:25,x2:x,y2:88,stroke:'var(--muted-foreground)','stroke-dasharray':'5 4'});
            add(picture, 'line', {x1:x+step/2,y1:25,x2:x+step/2,y2:88,stroke:'var(--muted-foreground)','stroke-dasharray':'1 4'});
            label(picture, x + step/2, 20, String(bits[i]));
            path += ` H ${x} V ${y(pair[0])} H ${x + step/2} V ${y(pair[1])} H ${x + step}`;
          });
          add(picture, 'line', {x1:left+bits.length*step,y1:25,x2:left+bits.length*step,y2:88,
            stroke:'var(--muted-foreground)','stroke-dasharray':'5 4'});
          add(picture, 'path', {d:path,fill:'none',stroke:'var(--foreground)','stroke-width':2});
          drawing.append(picture);
        }
        const prev = index === 0 ? visual.initial_level : differential[index - 1][1];
        detail.textContent = `第${index + 1}位是 ${bits[index]}。曼彻斯特位中${manchester[index][0] ? '高→低' : '低→高'}。差分曼彻斯特位首${prev === differential[index][0] ? '无跳变' : '有跳变'}，位中跳变用于同步。`;
        for (const [i, button] of [...controls.children].entries()) button.setAttribute('aria-pressed', String(i === index));
      };
      bits.forEach((_, index) => {
        const button = document.createElement('button');
        button.type = 'button';
        button.className = 'btn';
        button.textContent = `第${index + 1}位`;
        button.addEventListener('click', () => render(index));
        controls.append(button);
      });
      section.append(drawing, controls, detail);
      render(0);
    }
    paragraph(visual.note, 'text-small');
    panel.append(section);
  }
  function datapath() {
    const wrapper = document.createElement('div');
    wrapper.className = 'datapath-lesson';
    const controls = document.createElement('div');
    controls.className = 'viz-row';
    const drawing = document.createElement('div');
    const signals = document.createElement('p');
    signals.className = 'datapath-signals';
    const reason = document.createElement('p');
    reason.setAttribute('aria-live', 'polite');
    const objects = document.createElement('div');
    objects.className = 'viz-row';
    const detail = document.createElement('p');
    detail.setAttribute('aria-live', 'polite');
    let stage = 0;
    const buttons = [];
    const objectButtons = [];
    function selectObject(name) {
      detail.textContent = visual.objects[name];
      for (const button of objectButtons) button.setAttribute('aria-pressed', String(button.dataset.object === name));
    }
    function draw() {
      const state = visual.steps[stage];
      drawing.replaceChildren();
      const picture = document.createElementNS(ns, 'svg');
      picture.setAttribute('viewBox', '0 0 680 420');
      picture.setAttribute('class', 'mechanism-svg datapath-svg');
      picture.setAttribute('role', 'img');
      picture.setAttribute('aria-label', `单总线原图结构。${state.reason}`);
      const defs = add(picture, 'defs', {});
      const arrowId = markerId + '-datapath';
      const marker = add(defs, 'marker', {id:arrowId, viewBox:'0 0 8 8',refX:7,refY:4,markerWidth:6,markerHeight:6,orient:'auto'});
      add(marker, 'path', {d:'M0 0 L8 4 L0 8 Z',fill:'context-stroke'});
      function wire(id, path, directed = true) {
        const active = state.active.includes(id);
        add(picture, 'path', {d:path,fill:'none',stroke:active?'var(--viz-series-1)':'var(--border)',
          'stroke-width':active?3:1.5, ...(directed?{'marker-end':`url(#${arrowId})`}:{}), 'data-wire':id,
          'data-active':String(active)});
      }
      function text(x,y,value,small=false) {
        return add(picture,'text',{x,y,'text-anchor':'middle',...(small?{class:'mechanism-small'}:{})},value);
      }
      function component(name,x,y,w,h,value,muted=false) {
        const group=add(picture,'g',{'data-component':name});
        add(group,'rect',{x,y,width:w,height:h,rx:4,fill:muted?'var(--muted)':'var(--background)',
          stroke:'var(--border)','stroke-width':1.5});
        text(x+w/2,y+(value?20: h/2+5),name);
        if(value) text(x+w/2,y+40,value);
      }
      component('主存储器',165,8,350,40,null,true);
      wire('memory','M 340 48 V 72',false);
      add(picture,'path',{d:'M 35 72 H 645',fill:'none',stroke:'var(--border)','stroke-width':2});
      text(90,63,'系统总线',true);
      for(const [name,x] of [['MAR',45],['MDR',205],['PC',365],['IR',525]]) {
        component(name,x,105,85,42,null,true);
        wire('context-'+name,`M ${x+42} 185 V 147`,false);
      }
      wire('mar-memory','M 87 105 V 72',false);
      wire('mdr-memory','M 247 105 V 72',false);
      add(picture,'path',{d:'M 30 185 H 650',fill:'none',stroke:'var(--foreground)','stroke-width':3});
      text(116,176,'内部总线 · 16 位',true);
      text(452,176,`当前驱动 ${state.driver} · 总线 ${state.bus}`,true);
      wire('bus-y','M 90 185 V 245');
      wire('gpr-out','M 577 257 V 185');
      wire('bus-gpr','M 535 185 V 257');
      wire('y-a','M 140 273 H 250 V 289');
      wire('bus-b','M 375 185 V 289');
      wire('alu-z','M 312 328 V 355');
      wire('alu-fr','M 279 328 V 346 H 158 V 366');
      wire('z-out','M 360 382 H 455 V 185');
      component('Y',40,245,100,56,state.y);
      component('GPRs',500,257,125,65,stage>=2?'目标寄存器 ← 8':'操作数来源');
      const alu=add(picture,'g',{'data-component':'ALU'});
      add(alu,'path',{d:'M 217 289 H 287 L 312 301 L 337 289 H 407 L 366 328 H 258 Z',
        fill:'var(--background)',stroke:'var(--border)','stroke-width':1.5});
      text(312,319,'ALU');
      component('Z',265,355,95,56,state.z);
      component('FR',100,366,105,38,null,true);
      text(105,325,'Yin',true);
      text(239,280,'A',true);text(387,280,'B',true);text(327,347,'F',true);
      text(470,333,'Zout',true);text(579,241,'GPRout',true);text(522,241,'GPRin',true);
      add(picture,'path',{d:'M 448 344 L 455 331 L 462 344 Z',fill:'var(--background)',stroke:'var(--foreground)'});
      add(picture,'path',{d:'M 570 225 L 577 212 L 584 225 Z',fill:'var(--background)',stroke:'var(--foreground)'});
      text(653,286,'rs / rd',true);
      drawing.append(picture);
      signals.textContent=state.signals;
      reason.textContent=state.reason;
      buttons.forEach((button,index)=>button.setAttribute('aria-pressed',String(index===stage)));
    }
    visual.steps.forEach((step,index)=>{
      const button=document.createElement('button');
      button.type='button';button.className='btn';button.textContent=`${index+1} ${step.title}`;
      button.addEventListener('click',()=>{stage=index;draw();});
      controls.append(button);buttons.push(button);
    });
    for(const name of ['Y','Z','Zout','ALU','GPRs','FR','BUS']) {
      const button=document.createElement('button');
      button.type='button';button.className='btn';button.textContent=name==='BUS'?'内部总线':name;
      button.dataset.object=name;button.addEventListener('click',()=>selectObject(name));
      objects.append(button);objectButtons.push(button);
    }
    wrapper.append(controls,drawing,signals,reason,objects,detail);
    panel.append(wrapper);
    draw();selectObject('Y');
  }
  const handlers={sequence,address,graph,flow,band,table,lanes:table,tree,fields,partition,communication,datapath};
  handlers[visual.kind]();
  if (visual.kind!=='fields' && visual.source) panel.append(span('mechanism-field-note',visual.source));
}
