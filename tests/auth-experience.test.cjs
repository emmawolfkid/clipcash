/* Node-only simulated-DOM checks; no browser or database access. */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
function control(props={}) {
  return Object.assign({events:{},dataset:{},attrs:{},hidden:false,disabled:false,
    addEventListener(k,f){this.events[k]=f;},
    setAttribute(k,v){this.attrs[k]=v;}, removeAttribute(k){delete this.attrs[k];}
  }, props);
}
const groups = {email:{hidden:false},phone:{hidden:false},username:{hidden:false}};
const ids = {};
for (const [method,id] of Object.entries({email:'id_email',phone:'id_phone_number',username:'id_username'})) {
  ids[id]=control({value:'preserved-'+method,closest:()=>groups[method]});
}
const radios=['email','phone','username'].map(value=>control({value,checked:value==='email'}));
const toggles=['id_password1','id_password2'].map(id=>{
  ids[id]=control({type:'password',value:'preserve-secret'});
  return control({hidden:true,dataset:{passwordTarget:id,show:'Show password',hide:'Hide password',fieldLabel:id}});
});
const submit=control();
const form=control({querySelectorAll:s=>s==='button[type="submit"]'?[submit]:submit.attrs['aria-disabled']==='true'?[submit]:[]});
ids['auth-submit-status']=control({hidden:true});
const shell={querySelectorAll:s=>s==='input[name="contact_method"]'?radios:s==='[data-password-target]'?toggles:s==='form'?[form]:[],querySelector:()=>radios.find(r=>r.checked)};
const events={};
vm.runInNewContext(fs.readFileSync(__dirname+'/../static/js/auth.js','utf8'),{
  document:{querySelector:()=>shell,getElementById:id=>ids[id]},
  window:{addEventListener:(k,f)=>events[k]=f}
});
for(const method of ['phone','username','email']) {
  radios.forEach(r=>r.checked=r.value===method);
  radios.find(r=>r.checked).events.change();
  for(const key of Object.keys(groups)) assert.equal(groups[key].hidden,key!==method);
}
for(const toggle of toggles) {
  assert.equal(toggle.hidden,false);
  toggle.events.click(); assert.equal(ids[toggle.dataset.passwordTarget].type,'text');
  assert.equal(toggle.attrs['aria-pressed'],'true');
  toggle.events.click(); assert.equal(ids[toggle.dataset.passwordTarget].type,'password');
}
let first={preventDefault(){throw new Error('first submission must proceed');}};
form.events.submit(first); assert.equal(form.attrs['aria-busy'],'true');
assert.equal(submit.disabled,false);
let prevented=false;form.events.submit({preventDefault(){prevented=true;}});assert.equal(prevented,true);
toggles[0].events.click();events.pageshow();
assert.equal(ids.id_password1.type,'password');assert.equal(ids.id_password1.value,'preserve-secret');
assert.equal(ids.id_phone_number.value,'preserved-phone');assert.equal(form.dataset.submitting,undefined);
assert.equal(submit.attrs['aria-disabled'],undefined);assert.equal(ids['auth-submit-status'].hidden,true);
console.log('Auth controls: all identifier choices, show/hide, duplicate-submit guard and history reset passed');
