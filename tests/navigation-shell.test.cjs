/* Run with Node: node tests/navigation-shell.test.cjs. Simulated DOM, not browser visual validation. */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const source = fs.readFileSync(path.join(__dirname, '../static/js/member-nav.js'), 'utf8');
function fixture(hasSidebar) {
  let doc;
  class Element {
    constructor(name) { this.name=name; this.children=[]; this.inert=false; this.attrs={}; this.events={}; this.hidden=false; this.inert=false; this.open=false; const classes=new Set(); this.classList={add:x=>classes.add(x),remove:x=>classes.delete(x),contains:x=>classes.has(x),toggle:(x,on)=>on?classes.add(x):classes.delete(x)}; }
    append(...items) { items.forEach(x=>{x.parent=this;this.children.push(x);}); }
    contains(x) { return this===x || this.children.some(c=>c.contains(x)); }
    setAttribute(k,v) { this.attrs[k]=v; }
    getAttribute(k) { return this.attrs[k] || null; }
    removeAttribute(k) { delete this.attrs[k]; }
    addEventListener(k,f) { (this.events[k] ||= []).push(f); }
    emit(k,e={}) { (this.events[k] || []).forEach(f=>f({target:this,preventDefault(){this.prevented=true;},...e})); }
    focus() { assert.equal(this.closest('[hidden], [inert]'), null, 'focus target must be available: '+this.name); doc.activeElement=this; }
    closest(s) { if(s==='a[href]') return this.attrs.href?this:(this.parent?this.parent.closest(s):null); return this.hidden||this.inert?this:(this.parent?this.parent.closest(s):null); }
    getClientRects() { return this.closest('[hidden], [inert]')?[]:[{}]; }
    getBoundingClientRect() { return {top:0}; }
    scrollIntoView() { this.scrolled=true; }
    querySelector(s) { if(s==='[data-nav-close]')return close; if(s==='.app-nav')return hasSidebar?nav:null; if(s==='[aria-current="page"]'||s==='.is-current')return first; if(s==='summary')return summary; return null; }
    querySelectorAll() { return [close,first,last]; }
  }
  const body=new Element('body'), html=new Element('html'), header=new Element('header'), toggle=new Element('toggle'), publicMenu=new Element('public-menu'), shell=new Element('shell'), side=new Element('sidebar'), nav=new Element('nav'), close=new Element('close'), first=new Element('current-link'), last=new Element('last-link'), main=new Element('main'), overlay=new Element('overlay'), footer=new Element('footer'), profile=new Element('profile'), summary=new Element('summary');
  const panel=hasSidebar?side:publicMenu;
  nav.id='member-menu'; nav.attrs['aria-label']='Member menu'; publicMenu.id='site-menu'; publicMenu.attrs['aria-label']='Main navigation'; first.attrs.href='/dashboard/'; first.attrs['aria-current']='page'; last.attrs.href='/accounts/'; close.hidden=true; overlay.hidden=true; footer.inert=true;
  html.append(body); body.append(header,overlay,shell,footer); header.append(toggle,publicMenu,profile); profile.append(summary); shell.append(...(hasSidebar?[side,main]:[main])); if(hasSidebar) {side.append(close,nav);nav.append(first,last);} else publicMenu.append(close,first,last);
  const queries={'.site-menu-toggle':toggle,'.app-nav__toggle':null,'.app-sidebar':hasSidebar?side:null,'[data-nav-overlay]':overlay,'.profile-menu':profile,'.tabs':null};
  doc={body,documentElement:html,activeElement:body,events:{},querySelector:s=>queries[s]||null,getElementById:id=>id==='site-menu'?publicMenu:null,addEventListener(k,f){(this.events[k] ||= []).push(f);}};
  const media={matches:true,addEventListener(k,f){this.change=f;}}, windowEvents={};
  const win={matchMedia:()=>media,addEventListener(k,f){windowEvents[k]=f;},setTimeout:f=>f()};
  vm.runInNewContext(source,{document:doc,window:win});
  function key(key,shiftKey=false) {let e={key,shiftKey,preventDefault(){this.prevented=true;}};(doc.events.keydown||[]).forEach(f=>f(e));return e;}
  return {doc,media,windowEvents,body,toggle,panel,close,first,last,main,overlay,footer,profile,summary,key,change(wide){media.matches=!wide;media.change();}};
}
for(const sidebar of [true,false]) {
  const f=fixture(sidebar);
  assert.equal(f.toggle.hidden,false); assert.equal(f.panel.classList.contains('is-collapsed'),true); assert.equal(f.overlay.hidden,true);
  f.toggle.emit('click'); assert.equal(f.toggle.getAttribute('aria-expanded'),'true'); assert.equal(f.doc.activeElement,f.first); assert.notEqual(f.main.closest('[hidden], [inert]'),null); assert.equal(f.overlay.hidden,false); assert.equal(f.panel.getAttribute('aria-modal'),'true');
  f.last.focus(); assert.equal(f.key('Tab').prevented,true); assert.equal(f.doc.activeElement,f.close);
  assert.equal(f.key('Tab',true).prevented,true); assert.equal(f.doc.activeElement,f.last);
  f.key('Escape'); assert.equal(f.doc.activeElement,f.toggle); assert.equal(f.main.inert,false); assert.equal(f.footer.inert,true); assert.equal(f.overlay.hidden,true);
  f.toggle.emit('click'); f.overlay.emit('click'); assert.equal(f.toggle.getAttribute('aria-expanded'),'false');
  f.toggle.emit('click'); f.close.emit('click'); assert.equal(f.doc.activeElement,f.toggle);
  f.toggle.emit('click'); f.panel.emit('click',{target:f.first}); assert.equal(f.overlay.hidden,true);
  f.toggle.emit('click'); f.close.focus(); f.change(true); assert.equal(f.main.inert,false); assert.equal(f.toggle.hidden,true); assert.equal(f.doc.activeElement,f.first); assert.equal(f.panel.classList.contains('is-collapsed'),false);
  f.change(false); assert.equal(f.doc.activeElement,f.toggle); assert.equal(f.panel.classList.contains('is-collapsed'),true);
  f.profile.open=true; f.summary.focus(); f.key('Escape'); assert.equal(f.profile.open,false); assert.equal(f.doc.activeElement,f.summary);
  f.profile.open=true; f.doc.events.click.forEach(fn=>fn({target:f.main})); assert.equal(f.profile.open,false);
  f.toggle.emit('click'); f.windowEvents.pagehide(); assert.equal(f.main.inert,false); f.windowEvents.pageshow(); assert.equal(f.overlay.hidden,true);
  console.log((sidebar?'Portal':'Public')+' drawer: focus wrap, Escape, backdrop, close/link dismissal, breakpoints, profile and history cleanup passed');
}
