import data from "./data.json";
import history from "./events.json";
function need<T>(value:T|null|undefined):T { if(value==null) throw new Error("Missing replay element"); return value; }
const chart=need(document.querySelector<SVGSVGElement>("#chart"));
const slider=need(document.querySelector<HTMLInputElement>("#timeline"));
const play=need(document.querySelector<HTMLButtonElement>("#play"));
const clock=need(document.querySelector<HTMLElement>("#clock"));
const count=need(document.querySelector<HTMLElement>("#search-count"));
const speed=need(document.querySelector<HTMLElement>("#speed"));
const empty=need(document.querySelector<HTMLElement>("#empty"));
const barTip=need(document.querySelector<HTMLElement>('#bar-tip'));
const rangeLabel=need(document.querySelector<HTMLElement>('#date-range'));
const samplingNote=need(document.querySelector<HTMLElement>('#sampling-note'));
const windowSize=need(document.querySelector<HTMLSelectElement>('#window-size'));
const windowRange=need(document.querySelector<HTMLSelectElement>('#window-range'));
const fmt=(n:number)=>n.toLocaleString("en-US");
const time=(n:number)=>new Date(n).toISOString().slice(11,16);
const dateTime=(n:number)=>`Sep ${new Date(n).getUTCDate()}, ${time(n)}`;
const origin=Date.parse(history.origin);
const totals=[0];
for(const event of history.events)totals.push(need(totals.at(-1))+need(event[3]));
function searchesThrough(at:number){const offset=(at-origin)*1000;let lo=0,hi=history.events.length;while(lo<hi){const mid=Math.floor((lo+hi)/2);if(need(need(history.events[mid])[0])<offset)lo=mid+1;else hi=mid;}return need(totals[lo]);}
let windowStart=data.start,windowEnd=data.end;
let position=1000,playing=false,scrubbing=false,lastFrame=0,frame=0,lastPulse=0;
const searches=data.searches.map(r=>({...r,end:Date.parse(r.window)+300000}));
function node<K extends keyof SVGElementTagNameMap>(tag:K, attrs:Record<string,string|number>={}) {const el=document.createElementNS("http://www.w3.org/2000/svg",tag);for(const [k,v]of Object.entries(attrs))el.setAttribute(k,String(v));return el;}
function label(x:number,y:number,value:string,cls="axis",anchor="start") {const el=node("text",{x,y,class:cls,"text-anchor":anchor});el.textContent=value;return el;}
const group=node("g");chart.append(group);
const reducedMotion=matchMedia('(prefers-reduced-motion: reduce)');
const shelf=need(document.querySelector<HTMLElement>('#portraits'));
const detail=need(document.querySelector<HTMLElement>('#portrait-detail'));
let snapshots:HTMLDivElement[]=[];
function snapshotTimes(){return [1,3,5].map(i=>windowStart+(windowEnd-windowStart)*i/6);}
function buildPortraits(){
 shelf.replaceChildren(); detail.textContent='';
 snapshots=snapshotTimes().map(at=>{
  const snapshot=need(data.snapshots.find(s=>s.time===at));
  const row=document.createElement('div');row.className='snapshot';
  for(const p of snapshot.people){
   const button=document.createElement('button');button.className='portrait';button.style.setProperty('--size',`${32+18*Math.sqrt(Math.min(p.votes,1200)/1200)}px`);button.setAttribute('aria-label',`${p.name}, ${p.votes} votes at ${time(at)} UTC`);
   const img=document.createElement('img');img.src=p.avatar;img.alt='';const votes=document.createElement('small');votes.textContent=fmt(p.votes);button.append(img,votes);
   const describe=()=>{detail.textContent=`${p.name} · ${fmt(p.votes)} votes · ${dateTime(at)} UTC`;};const clear=()=>{detail.textContent='';};button.addEventListener('pointerenter',describe);button.addEventListener('pointerleave',clear);button.addEventListener('focus',describe);button.addEventListener('blur',clear);row.append(button);
  }
  const cell=document.createElement('div');cell.className='snapshot-cell';cell.append(row);shelf.append(cell);return row;
 });
}
let lastBucket=-1,lastWidth=0;
function updateNumber(el:HTMLElement,value:string){if(el.textContent===value)return;el.textContent=value;if(!reducedMotion.matches){el.getAnimations().forEach(a=>a.cancel());el.animate([{transform:'translateY(7px) scale(.85)',opacity:.35},{transform:'translateY(-4px) scale(1.12)',opacity:1,offset:.55},{transform:'none',opacity:1}],{duration:440,easing:'cubic-bezier(.2,.8,.3,1)'});}}
function render(){
 const at=windowStart+(windowEnd-windowStart)*position/1000;
 const width=Math.max(280,chart.clientWidth),left=8,right=8,plotW=width-left-right,baseline=213;
 chart.setAttribute('viewBox',`0 0 ${width} 245`);
 const x=(t:number)=>left+(t-windowStart)/(windowEnd-windowStart)*plotW;
 const rows=searches.filter(r=>r.end>windowStart&&r.end<=windowEnd),bw=plotW/rows.length;
 const estimated=at>data.sampledFrom;
 const running=(estimated?'≈':'')+fmt(searchesThrough(at)-searchesThrough(windowStart));
 if(count.textContent!==running){count.textContent=running;const now=performance.now();if(playing&&!scrubbing&&!reducedMotion.matches&&now-lastPulse>600){lastPulse=now;count.animate([{transform:'scale(1)'},{transform:'scale(1.06)',offset:.35},{transform:'scale(1)'}],{duration:240});}}
 clock.textContent=new Date(at).toISOString().slice(11,19);slider.value=String(Math.round(position));slider.setAttribute('aria-valuetext',`${dateTime(at)} UTC`);
 const oldCursor=group.querySelector('#cursor');oldCursor?.setAttribute('transform',`translate(${x(at)},0)`);
 const bucket=Math.floor(at/300000);
 if(bucket===lastBucket&&width===lastWidth)return;
 lastBucket=bucket;lastWidth=width;group.replaceChildren();barTip.hidden=true;
 const volumeMax=Math.max(2000,...rows.map(r=>r.searches));
 const latencyMax=Math.ceil(Math.max(20,...rows.map(r=>r.p95_ms))/20)*20;
 const ly=(ms:number)=>94-ms/latencyMax*62;
 const percentileLink=node('a',{href:'https://cloudprober.org/docs/how-to/percentiles/',target:'_blank',rel:'noopener noreferrer','aria-label':'P95: learn about latency percentiles'});percentileLink.append(label(left,14,'P95','latency-label'));group.append(percentileLink,label(width-right,14,`${latencyMax} ms`,'latency-label','end'));
 group.append(node('line',{x1:left,x2:width-right,y1:94,y2:94,class:'latency-baseline'}),label(width-right,106,'0 ms','latency-zero','end')); 
 group.append(label(left,124,'Searches / 5 min','annotation'));
 group.append(node('line',{x1:left,x2:width-right,y1:baseline,y2:baseline,class:'baseline'}));
 const path=rows.map((r,i)=>`${i?'L':'M'}${x(r.end)-bw/2},${ly(r.p95_ms)}`).join(' ');
 group.append(node('path',{d:path,fill:'none',stroke:'#0b5fd0','stroke-width':2,'stroke-linejoin':'round','stroke-linecap':'round',class:'latency-trace',opacity:.2}));
 const elapsedPath=rows.filter(r=>r.end<=at).map((r,i)=>`${i?'L':'M'}${x(r.end)-bw/2},${ly(r.p95_ms)}`).join(' ');group.append(node('path',{d:elapsedPath,fill:'none',stroke:'#0b5fd0','stroke-width':2,'stroke-linejoin':'round','stroke-linecap':'round',class:'latency-trace'}));
 for(const row of rows){
  const bx=x(row.end)-bw,h=row.searches/volumeMax*72,eh=row.empty_results/volumeMax*72;
  const current=row.end<=at&&row.end>at-300000,g=node('g',{class:current?'current-bucket':''});
  const gap=bw<5?.5:2;
  const bars=node('g',{opacity:row.end<=at?1:.15,class:current?'new-bar':''});
  bars.append(node('rect',{x:bx+gap,y:baseline-h,width:Math.max(.5,bw-2*gap),height:h,fill:current?'#14795f':'#44aa99'}),node('rect',{x:bx+gap,y:baseline-h,width:Math.max(.5,bw-2*gap),height:eh,fill:'#e5a04b'}));g.append(bars);
  const description=`${dateTime(row.end-300000)}–${time(row.end)} UTC: ${row.sampled?'about ':''}${fmt(row.searches)} searches, ${fmt(row.empty_results)} zero matches; P95 ${row.p95_ms.toFixed(1)} ms`;
  const title=node('title');title.textContent=description;g.append(title);g.setAttribute('tabindex','0');g.setAttribute('role','button');g.setAttribute('aria-label',description);
  g.append(node('rect',{x:bx,y:26,width:bw,height:baseline-26,fill:'transparent'}));
  const show=()=>{barTip.replaceChildren();const heading=document.createElement('strong');heading.textContent=`${dateTime(row.end-300000)}–${time(row.end)} UTC`;const counts=document.createElement('p');counts.textContent=`${row.sampled?'≈ ':''}${fmt(row.searches)} engine searches`;const matches=document.createElement('p');matches.className='tip-matches';matches.textContent=`${row.sampled?'≈ ':''}${fmt(row.searches-row.empty_results)} returned matches`;const zeros=document.createElement('p');zeros.className='tip-zeros';zeros.textContent=`${row.sampled?'≈ ':''}${fmt(row.empty_results)} zero matches (${(100*row.empty_results/row.searches).toFixed(1)}%)`;const latency=document.createElement('p');latency.textContent=`P95 ${row.p95_ms.toFixed(1)} ms · median ${row.p50_ms.toFixed(1)} ms`;barTip.append(heading,counts,matches,zeros,latency);barTip.hidden=false;};
  g.addEventListener('pointerenter',show);g.addEventListener('focus',show);g.addEventListener('click',show);g.addEventListener('pointerleave',()=>{barTip.hidden=true;});g.addEventListener('blur',()=>{barTip.hidden=true;});g.addEventListener('keydown',e=>{if(e.key==='Escape')barTip.hidden=true;});group.append(g);
 }
 if(data.announcement>=windowStart&&data.announcement<windowEnd){const ax=x(data.announcement);group.append(node('line',{x1:ax,x2:ax,y1:133,y2:baseline,stroke:'#969b9f','stroke-dasharray':'2 4'}),label(ax+6,139,'Voting opens','annotation'));}
 snapshotTimes().forEach((t,i)=>{group.append(label(x(t),baseline+23,time(t),'axis','middle'));const row=need(snapshots[i]),visible=at>=t;if(visible&&!row.classList.contains('shown'))row.classList.add('arrive');if(!visible)row.classList.remove('arrive');row.classList.toggle('shown',visible);});
 const cursor=node('g',{id:'cursor',transform:`translate(${x(at)},0)`});cursor.append(node('line',{x1:0,x2:0,y1:26,y2:baseline,stroke:'#14795f','stroke-width':1.5}),node('rect',{x:-3,y:baseline-3,width:6,height:6,fill:'#14795f'}));group.append(cursor);
 const latest=rows.filter(r=>r.end<=at).at(-1);
 updateNumber(speed,latest?latest.p95_ms.toFixed(1)+' ms':'—');updateNumber(empty,latest?(latest.sampled?'≈':'')+fmt(latest.empty_results):'—');
}
function buttonState(){play.innerHTML=playing?'Ⅱ <span>Pause</span>':'▶ <span>Replay</span>';play.setAttribute('aria-label',playing?'Pause replay':'Replay selected window');}
function loop(t:number){if(!playing)return;const dt=lastFrame?t-lastFrame:0;lastFrame=t;if(!scrubbing){position=Math.min(1000,position+dt/35000*1000);render();}if(position>=1000){playing=false;buttonState();return;}frame=requestAnimationFrame(loop);}
function toggle(){playing=!playing;if(playing){if(position>=1000)position=0;lastFrame=0;frame=requestAnimationFrame(loop);}else cancelAnimationFrame(frame);buttonState();render();}
function setWindow(start:number,hours:number){
 const duration=hours*3600000;windowStart=Math.max(data.start,Math.min(data.end-duration,start));windowEnd=windowStart+duration;position=1000;playing=false;cancelAnimationFrame(frame);buttonState();lastBucket=-1;
 rangeLabel.textContent=`${dateTime(windowStart)} – ${dateTime(windowEnd)} UTC`;
 samplingNote.textContent=windowEnd>data.sampledFrom?'≈ Counts estimated after 20:08 UTC · 1 in 10 searches recorded':'';samplingNote.hidden=!samplingNote.textContent;
 windowSize.value=String(hours);windowRange.hidden=hours===12;rangeLabel.hidden=hours!==12;
 windowRange.replaceChildren();
 for(let start=data.start;start+duration<=data.end;start+=3600000){const option=document.createElement('option');option.value=String(start);option.textContent=`${dateTime(start)} – ${dateTime(start+duration)} UTC`;windowRange.append(option);}
 windowRange.value=String(windowStart);
 buildPortraits();render();
}
windowSize.addEventListener('change',()=>{const hours=Number(windowSize.value),at=windowStart+(windowEnd-windowStart)*position/1000;setWindow(Math.floor((at-hours*3600000)/3600000)*3600000,hours);});
windowRange.addEventListener('change',()=>setWindow(Number(windowRange.value),Number(windowSize.value)));
play.addEventListener('click',toggle);
slider.addEventListener('input',()=>{position=Number(slider.value);lastFrame=performance.now();render();});slider.addEventListener('pointerdown',()=>{scrubbing=true;});
const release=()=>{scrubbing=false;lastFrame=performance.now();};window.addEventListener('pointerup',release);window.addEventListener('pointercancel',release);slider.addEventListener('blur',release);
const seekKeys=new Set(['ArrowLeft','ArrowRight','ArrowUp','ArrowDown','Home','End','PageUp','PageDown']);slider.addEventListener('keydown',e=>{if(seekKeys.has(e.key))scrubbing=true;});slider.addEventListener('keyup',release);
document.addEventListener('keydown',e=>{if(e.code!=='Space'||e.repeat||e.target instanceof HTMLInputElement||e.target instanceof HTMLButtonElement||e.target instanceof HTMLSelectElement)return;e.preventDefault();toggle();});document.addEventListener('visibilitychange',()=>{if(document.hidden&&playing){playing=false;cancelAnimationFrame(frame);buttonState();}});
new ResizeObserver(render).observe(chart);setWindow(data.start,12);
