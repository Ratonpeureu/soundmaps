const $=(s,r=document)=>r.querySelector(s),$$=(s,r=document)=>[...r.querySelectorAll(s)];
const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const fd=(s,full=true)=>s?new Date(s).toLocaleString('fr-FR',full?{dateStyle:'short',timeStyle:'short'}:{dateStyle:'short'}):'—';
const pct=x=>x==null?'—':Math.round(x*100)+' %';
const clone=o=>JSON.parse(JSON.stringify(o));
const slug=s=>String(s||'').normalize('NFD').replace(/[\u0300-\u036f]/g,'').toLowerCase().replace(/[^a-z0-9]+/g,'_').replace(/^_+|_+$/g,'');
const COL=['#0e9f9a','#6d5df6','#f59e0b','#ef4444','#22c55e','#3b82f6','#ec4899','#64748b'];
const kpi=(l,v,s='')=>`<div class="kpi"><span>${esc(l)}</span><b>${esc(v)}</b>${s?`<small>${esc(s)}</small>`:''}</div>`;
function toast(m,bad){const t=document.createElement('div');t.className='toast'+(bad?' bad':'');t.textContent=m;document.body.append(t);setTimeout(()=>t.remove(),4500)}
function modal(html){const o=document.createElement('div');o.className='ov';o.innerHTML=`<div class="md">${html}</div>`;o.addEventListener('mousedown',e=>{if(e.target===o)o.remove()});o.addEventListener('click',e=>{if(e.target.closest('[data-close]'))o.remove()});document.body.append(o);return o}
const ERR={invalid_credentials:'Identifiants incorrects.',not_authenticated:'Connexion requise.',insufficient_role:'Droits insuffisants.',account_temporarily_locked:'Compte bloqué quelques minutes (trop d\'essais).',rate_limited:'Trop de requêtes, patientez un instant.',payload_too_large:'Fichier trop volumineux.',study_not_deletable:'Seul un brouillon sans réponse peut être supprimé.',end_date_required_in_future:'Indiquez une date de clôture dans le futur avant d\'activer.',sponsor_inactive:'L\'initiateur est inactif.',asset_in_use:'Ce média est utilisé dans une étude ou un initiateur.',sponsor_has_studies:'Cet initiateur a des études.',ai_not_configured:'L\'IA n\'est pas configurée sur le serveur.',ai_unavailable:'L\'IA est indisponible, réessayez.',ai_upstream_overloaded:'L\'IA est surchargée, réessayez.',ai_invalid_response:'Réponse IA invalide, réessayez.',not_a_lottery:'Cette étude n\'est pas un tirage au sort.',already_drawn:'Le tirage a déjà eu lieu.',no_profiles_for_slot:'Une rubrique n\'a aucun profil : générez-en d\'abord.',invalid_pair:'Paire de questions incompatible.'};
function errMsg(d,st){const t=d?.detail;if(typeof t==='string')return ERR[t]||t;if(Array.isArray(t))return t.map(e=>(e.loc||[]).slice(1).join('.')+' : '+e.msg).join(' ; ');if(t?.code)return (ERR[t.code]||t.code)+(t.errors?' : '+t.errors.join(' ; '):'');return 'Erreur '+st}
const A={base:localStorage.getItem('ss_api')||'https://soundmaps.rhmanager.site/api/v1',access:sessionStorage.getItem('ss_at'),refresh:sessionStorage.getItem('ss_rt'),me:null};
let _rf=null;
const saveTokens=(a,r)=>{A.access=a;A.refresh=r;sessionStorage.setItem('ss_at',a);sessionStorage.setItem('ss_rt',r)};
function logout(){A.access=A.refresh=null;sessionStorage.clear();$('#shell').hidden=true;$('#login').hidden=false}
function refreshTokens(){if(!A.refresh)return Promise.resolve(false);return _rf||=fetch(A.base+'/auth/refresh',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({refresh_token:A.refresh})}).then(async r=>{if(!r.ok)return false;const d=await r.json();saveTokens(d.access_token,d.refresh_token);return true}).catch(()=>false).finally(()=>{_rf=null})}
async function raw(path,{method='GET',body,query,form}={},retry=true){
 const url=new URL(A.base+path);for(const[k,v]of Object.entries(query||{})){if(Array.isArray(v))v.forEach(x=>url.searchParams.append(k,x));else if(v!==''&&v!=null)url.searchParams.set(k,v)}
 let r;try{r=await fetch(url,{method,headers:{...(body?{'Content-Type':'application/json'}:{}),...(A.access?{Authorization:'Bearer '+A.access}:{})},body:form||(body?JSON.stringify(body):undefined)})}catch{throw new Error('Serveur injoignable (réseau ou CORS)')}
 if(r.status===401&&retry&&await refreshTokens())return raw(path,{method,body,query,form},false);
 if(r.status===401){logout();throw new Error('Session expirée')}return r}
async function api(path,o){const r=await raw(path,o);const d=await r.json().catch(()=>null);if(!r.ok)throw new Error(errMsg(d,r.status));return d}
async function download(path,query,name){const r=await raw(path,{query});if(!r.ok)throw new Error('Erreur '+r.status);const u=URL.createObjectURL(await r.blob());const a=document.createElement('a');a.href=u;a.download=name;a.click();setTimeout(()=>URL.revokeObjectURL(u),5000)}
function boot({page,roles,ready}){
 const nav=[['index.html','Mesures','mesures'],['surveys.html','Enquêtes','surveys'],['studies.html','Études','studies'],['invitations.html','Invitations','invit']];
 document.body.insertAdjacentHTML('afterbegin',`<div id="login" class="lg-wrap" hidden><div class="card lg-card"><div class="brand"><i></i>SunuSound</div><div><h2>Administration</h2><div class="sub">Connectez-vous avec votre compte.</div></div><label class="f">E-mail<input id="l-email" type="email" autocomplete="username"></label><label class="f">Mot de passe<input id="l-pass" type="password" autocomplete="current-password"></label><details><summary>Paramètres de connexion</summary><label class="f">Adresse du serveur<input id="l-api"></label></details><button class="btn" id="l-go">Se connecter</button><div id="l-err" class="err"></div></div></div>
 <div id="shell" hidden><header class="top"><div class="brand"><i></i>SunuSound</div><nav>${nav.map(n=>`<a href="${n[0]}" class="${n[2]===page?'on':''}">${n[1]}</a>`).join('')}</nav><span class="sub" id="who"></span><button class="btn sec sm" id="logout">Déconnexion</button></header><main class="page" id="main"></main></div>`);
 $('#l-api').value=A.base;
 const start=async()=>{try{A.me=await api('/users/me')}catch{return}
  if(!roles.includes(A.me.role)){logout();$('#l-err').textContent="Ce compte n'a pas accès à cette page.";return}
  $('#who').textContent=A.me.pseudonym+' · '+(A.me.role==='tenant_admin'?'Administrateur':'Analyste');$('#login').hidden=true;$('#shell').hidden=false;
  try{await ready(A.me)}catch(e){toast(e.message,1)}};
 $('#l-go').onclick=async()=>{$('#l-err').textContent='';A.base=$('#l-api').value.trim().replace(/\/+$/,'');localStorage.setItem('ss_api',A.base);
  try{const r=await fetch(A.base+'/auth/login',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({email:$('#l-email').value.trim(),password:$('#l-pass').value})});const d=await r.json().catch(()=>({}));if(!r.ok)throw new Error(errMsg(d,r.status));saveTokens(d.access_token,d.refresh_token);await start()}catch(e){$('#l-err').textContent=e.message}};
 $('#l-pass').addEventListener('keydown',e=>e.key==='Enter'&&$('#l-go').click());
 $('#logout').onclick=async()=>{try{await api('/auth/logout',{method:'POST',body:{refresh_token:A.refresh}})}catch{}logout()};
 if(A.access)start();else $('#login').hidden=false}
// ---------- graphiques (SVG / CSS, sans dépendance)
const Ch={
 bars(items,{total}={}){if(!items.length)return'<div class="empty">Aucune donnée</div>';const mx=Math.max(1,...items.map(i=>i.v)),t=total??items.reduce((a,i)=>a+i.v,0);
  return `<div class="bars">${items.map((i,k)=>`<div class="br"><span class="bl" title="${esc(i.l)}">${esc(i.l)}</span><div class="bt"><i style="width:${i.v/mx*100}%;background:${COL[k%8]}"></i></div><span class="bv">${i.v}${t?' · '+Math.round(i.v/t*100)+' %':''}</span></div>`).join('')}</div>`},
 donut(items){const t=items.reduce((a,i)=>a+i.v,0);if(!t)return'<div class="empty">Aucune donnée</div>';let off=0;const R=15.9155;
  const seg=items.map((i,k)=>{const p=i.v/t*100,s=`<circle r="${R}" cx="21" cy="21" fill="none" stroke="${COL[k%8]}" stroke-width="6" stroke-dasharray="${p} ${100-p}" stroke-dashoffset="${25-off}"/>`;off+=p;return s}).join('');
  return `<div class="donut"><svg viewBox="0 0 42 42"><circle r="${R}" cx="21" cy="21" fill="none" stroke="var(--bd)" stroke-width="6"/>${seg}<text x="21" y="22.6" text-anchor="middle" font-size="6" fill="currentColor" font-weight="700">${t}</text></svg><div class="dleg">${items.map((i,k)=>`<div><i style="background:${COL[k%8]}"></i>${esc(i.l)} <b>${Math.round(i.v/t*100)} %</b></div>`).join('')}</div></div>`},
 cols(vals,labels){const mx=Math.max(1,...vals);return `<div class="cols">${vals.map((v,i)=>`<div class="cl" title="${esc(labels[i])} : ${v}"><b>${v}</b><div class="cb"><i style="height:${v/mx*100}%"></i></div><span>${esc(labels[i])}</span></div>`).join('')}</div>`},
 line(series,labels){const all=series.flatMap(s=>s.v.filter(v=>v!=null));if(!all.length)return'<div class="empty">Aucune donnée</div>';const W=560,H=170,P=30,mx=Math.max(...all,1),n=Math.max(...series.map(s=>s.v.length));
  const X=i=>P+(n<2?0:i*(W-P-10)/(n-1)),Y=v=>H-24-(v/mx)*(H-40);
  const lines=series.map((s,k)=>`<polyline fill="none" stroke="${COL[k]}" stroke-width="2.5" points="${s.v.map((v,i)=>v==null?'':X(i)+','+Y(v)).filter(Boolean).join(' ')}"/>`).join('');
  const lg=series.length>1?`<div class="dleg h">${series.map((s,k)=>`<div><i style="background:${COL[k]}"></i>${esc(s.name)}</div>`).join('')}</div>`:'';
  return `<div class="line">${lg}<svg viewBox="0 0 ${W} ${H}"><line x1="${P}" y1="${H-24}" x2="${W-6}" y2="${H-24}" stroke="var(--bd)"/><text x="2" y="14" font-size="10" fill="var(--mu)">${mx}</text>${lines}<text x="${P}" y="${H-6}" font-size="10" fill="var(--mu)">${esc(labels[0]??'')}</text><text x="${W-6}" y="${H-6}" font-size="10" text-anchor="end" fill="var(--mu)">${esc(labels[labels.length-1]??'')}</text></svg></div>`},
 stack(parts){const t=parts.reduce((a,p)=>a+p.v,0)||1;return `<div class="stk">${parts.map((p,k)=>p.v?`<i style="width:${p.v/t*100}%;background:${p.c||COL[k%8]}" title="${esc(p.l)} ${p.v}"></i>`:'').join('')}</div>`},
 dual(rows,an,bn){if(!rows.length)return'<div class="empty">Aucune donnée</div>';const mx=Math.max(1,...rows.flatMap(r=>[r.a||0,r.b||0]));
  return `<div class="dleg h"><div><i style="background:${COL[0]}"></i>${esc(an)}</div><div><i style="background:${COL[1]}"></i>${esc(bn)}</div></div><div class="bars">${rows.map(r=>`<div class="br2"><span class="bl" title="${esc(r.l)}">${esc(r.l)}</span><div><div class="bt"><i style="width:${(r.a||0)/mx*100}%;background:${COL[0]}"></i></div><div class="bt"><i style="width:${(r.b||0)/mx*100}%;background:${COL[1]}"></i></div></div><span class="bv">${Math.round(r.a||0)} % · ${Math.round(r.b||0)} %</span></div>`).join('')}</div>`}
};