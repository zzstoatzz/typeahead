import { BSKY_MOD_DID, hasExplicitSlur, shouldHide, isDomainBlocked, resolveHidden } from '../../../../src/moderation';
import { createHash } from 'node:crypto';

const now_epoch = 1800000000;
let now_millis = now_epoch * 1000;
Date.now = () => now_millis;
const source = await Bun.file(new URL('../../../../src/moderation.ts', import.meta.url)).text();
const cases: any[] = [];
const result = (f: () => any) => { try { return {ok: true, value: f()}; } catch { return {ok: false}; } };
function add(id: string, handle: string, labels: any[] = [], override: any = null, domains: string[] = [], expiry_unsupported = false) {
  const snapshot = {schema_version:1,source_observed_at:now_epoch,complete:true,
    overrides:override ? [{did:'did:fixture:test',action:override}] : [], blocked_domains:domains};
  const blocked = isDomainBlocked(handle,new Set(domains.map(d=>d.toLowerCase())));
  cases.push({id,now_epoch,now_millis,actor:{did:'did:fixture:test',handle,quality_score:1,version:1,labels,hidden:true},snapshot,expiry_unsupported,
    expected:{slur:result(()=>hasExplicitSlur(handle)),label_hide:result(()=>shouldHide(labels)),domain:result(()=>blocked),
      hidden:result(()=>!!resolveHidden({handle,labels,override,domainBlocked:blocked}))}});
}
const seeds = ['chink','coons','fag','faggot','kike','nigger','nigga','tranny','Ćhink','f4g','n1gger','chínk'];
const boundaries = ['', '-', '.', '_', '/', ' ', 'é', '東', 'a', '1', '💩', '\u0301'];
for (const seed of seeds) {
  for (let i=0;i<boundaries.length;i++) for (let j=0;j<boundaries.length;j++) {
    add(`boundary:${seed}:${i}:${j}`,boundaries[i]+seed+boundaries[j]);
  }
  for (const separator of ['.','-','_']) add(`separators:${seed}:${separator}`,[...seed].join(separator)+'.test');
  add(`uppercase:${seed}`,seed.toUpperCase());
}
const truth = [false,true,0,1,'','false',[],{},null];
for (const val of ['!hide','!takedown','!suspend','spam','!no-unauthenticated','sexual','other']) {
  for (const src of [BSKY_MOD_DID,'did:fixture:other']) {
    for (let n=0;n<truth.length;n++) add(`label:${val}:${src}:${n}`,'ordinary.test',[{src,val,neg:truth[n]}]);
  }
}
const expiries: [any,boolean][] = [
  [undefined,false],[null,false],['',false],[0,false],[false,false],[1,false],[true,false],
  [now_epoch*1000,false],[now_epoch*1000+1,false],[now_epoch*1000-1,false],
  ['2026-01-01',false],['2040-01-01',false],['2027-01-15T08:00:00Z',false],
  ['2027-01-15T08:00:00.0001Z',false],['2027-01-15T08:00:00.001Z',false],
  ['2027-01-15T09:00:00+01:00',false],['1969-12-31T23:59:59.999999Z',false],
  ['2027-01-15t08:00:00z',false],['0000-01-01',false],
  ['2026',true],['2026-02',true],['2026-02-30',true],['2026-02-30T01:00:00Z',true],
  ['2026-01-01T00:00:00',true],['not-a-date',true],['0',true],[[],true],[{},true],
  ['2026-12-31T23:59:60Z',true],['+010000-01-01T00:00:00Z',true],
];
for (let i=0;i<expiries.length;i++) {
  const [exp,unsupported]=expiries[i];
  add(`expiry:${i}`,'ordinary.test',[{src:BSKY_MOD_DID,val:'!hide',exp}],null,[],unsupported);
  add(`expiry-show:${i}`,'ordinary.test',[{src:BSKY_MOD_DID,val:'!hide',exp}],'show',[],unsupported);
}
for (const handle of ['blocked.test','x.blocked.test','X.BLOCKED.TEST','evil-blocked.test','blocked.test.evil',
                       'İ.test','ΟΣ.test','οσ.test','ος.test','Σ.test','💩.test','end.']) {
  for (const domain of ['blocked.test','İ.TEST','ΟΣ.TEST','Σ.TEST','']) {
    for (const override of [null,'show','hide']) add(`domain:${handle}:${domain}:${override}`,handle,[],override,[domain]);
  }
}
for (const override of [null,'show','hide']) {
  for (const handle of ['ordinary.test','nigger.test','x.blocked.test']) {
    add(`priority:${handle}:${override}`,handle,[{src:BSKY_MOD_DID,val:'spam'}],override,['blocked.test']);
  }
}
for (const label of [null,1,true,'text']) add(`malformed:${JSON.stringify(label)}`,'ordinary.test',[label]);
add('null-after-active-label','ordinary.test',[{src:BSKY_MOD_DID,val:'spam'},null]);
add('null-after-active-label-show','ordinary.test',[{src:BSKY_MOD_DID,val:'spam'},null],'show');
add('null-before-active-label','ordinary.test',[null,{src:BSKY_MOD_DID,val:'spam'}]);
add('null-label-show','ordinary.test',[null],'show');
add('foreign-valid-expiry','ordinary.test',[{src:'did:fixture:foreign',val:'spam',exp:'2040-01-01T00:00:00Z'}]);
add('foreign-unsupported-expiry','ordinary.test',[{src:'did:fixture:foreign',val:'spam',exp:'2026'}],null,[],true);
add('json-expiry-conversion-throws','ordinary.test',[{src:BSKY_MOD_DID,val:'spam',exp:{toString:1}}],null,[],true);
add('RFC-normalized-hour24','ordinary.test',[{src:BSKY_MOD_DID,val:'spam',exp:'2026-01-01T24:00:00Z'}],null,[],true);
add('RFC-offset-no-colon','ordinary.test',[{src:BSKY_MOD_DID,val:'spam',exp:'2026-01-01T00:00:00+0000'}],null,[],true);
add('RFC-minute-only','ordinary.test',[{src:BSKY_MOD_DID,val:'spam',exp:'2026-01-01T00:00Z'}],null,[],true);
for (const offset of [1,499,500,501,999]) {
  now_millis=now_epoch*1000+offset;
  add(`fractional-now:${offset}`,'ordinary.test',[{src:BSKY_MOD_DID,val:'spam',exp:'2027-01-15T08:00:00.500Z'}]);
}
now_millis=now_epoch*1000;
const output = {schema:1,canonical_source_sha256:createHash('sha256').update(source).digest('hex'),now_epoch,cases};
await Bun.write(new URL('./cases.json',import.meta.url),JSON.stringify(output)+'\n');
console.log(JSON.stringify({cases:cases.length,unsupported_expiry_cases:cases.filter(c=>c.expiry_unsupported).length}));
