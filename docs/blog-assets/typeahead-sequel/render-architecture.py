from pathlib import Path
from html import escape
out=Path(__file__).resolve().parent
bg='#f7f9fc'; ink='#152940'; muted='#52647a'; blue='#245cba'; teal='#08766c'; purple='#7450a2'
s=['<svg xmlns="http://www.w3.org/2000/svg" width="1440" height="1080" viewBox="0 0 1440 1080" role="img" aria-labelledby="title desc"><title id="title">Typeahead: prepare the search, then serve it</title><desc id="desc">Clients query a Cloudflare Worker which asks a Zig search service on Fly. The search service holds an R2 snapshot and a live overlay, with no full actor replica. Relay events go through a Zig ingester and the Worker into Turso. Prefect jobs on heavypad build snapshots and enrich actors. Turso changes feed the overlay. Request counters still write to Turso.</desc>','<defs>']
for n,c in [('request',blue),('live',teal),('batch',purple),('quiet',muted)]: s.append(f'<marker id="{n}" markerWidth="9" markerHeight="9" refX="8" refY="4" orient="auto"><path d="M0 0 L8 4 L0 8" fill="{c}"/></marker>')
s += ['</defs>',f'<rect width="1440" height="1080" fill="{bg}"/>','<style>text{font-family:Helvetica,Arial,sans-serif;fill:#152940}.small{font-size:17px;fill:#52647a}.body{font-size:20px}.name{font-size:28px;font-weight:700}.label{font-size:18px;font-weight:600}</style>']
def t(x,y,txt,cl='body',color=None): s.append(f'<text x="{x}" y="{y}" class="{cl}"'+(f' style="fill:{color}"' if color else '')+'>'+escape(txt)+'</text>')
def box(x,y,w,h,title,where,lines,color=ink):
 s.append(f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="9" fill="white" stroke="{color}" stroke-width="1.5"/>'); t(x+22,y+33,where,'small'); t(x+22,y+70,title,'name')
 for i,l in enumerate(lines):t(x+22,y+103+i*26,l,'body')
def path(d,n='live',dash=False):
 c={'request':blue,'live':teal,'batch':purple,'quiet':muted}[n];s.append(f'<path d="{d}" fill="none" stroke="{c}" stroke-width="2.5" marker-end="url(#{n})"'+(' stroke-dasharray="8 6"' if dash else '')+'/>')
t(48,64,'typeahead, after the replica', 'name');t(48,102,'Prepare the expensive work offline. Answer each keystroke with a small lookup and merge.','body')
t(48,157,'Serving', 'label',blue)
box(48,190,220,180,'Clients','Atproto apps',['bsky38 · pckt','race38 · others'])
box(348,190,285,210,'Edge Worker','Cloudflare',['XRPC + 60 s cache','Rate limits; ingest API','Counters → Turso'])
box(773,190,590,210,'Search service','Fly / ewr     Zig · 4 shared CPUs · 2 GB',[''],blue)
s.append('<rect x="795" y="280" width="252" height="93" rx="5" fill="#eef3fc"/>');t(813,311,'Read-only snapshot','label',blue);t(813,342,'Precomputed top-N keys','small')
s.append('<rect x="1065" y="280" width="276" height="93" rx="5" fill="#eaf5f2"/>');t(1083,311,'Live overlay','label',teal);t(1083,342,'Additions + suppressions','small')
path('M268 250 H348','request');t(286,232,'q=','small',blue)
path('M633 250 H773','request');t(660,229,'/search','small',blue)
t(795,426,'No full actor replica.','small');t(795,452,'60 GB volume; bounded candidate set.','small')
t(48,490,'Continuous discovery & freshness', 'label',teal)
box(48,523,220,158,'Relay','bsky.network',['subscribeRepos'],teal)
box(348,523,285,158,'Ingester','Fly / ewr     Zig',['1 shared CPU · 256 MB','Stateless batches'],teal)
box(773,523,300,158,'Turso','Durable source',['Actors + tombstones','Moderation + profiles'],teal)
path('M268 602 H348');t(279,580,'events','small',teal)
path('M490 523 V400');t(501,473,'ingest','small',teal)
path('M633 346 H700 V602 H773');t(654,499,'writes','small',teal)
path('M1073 602 H1200 V373');t(1214,504,'5-minute','small',teal);t(1214,531,'delta replay','small',teal)
t(48,745,'Offline builds & enrichment', 'label',purple)
box(48,796,220,172,'Sources','Appviews + PLC',['Bluesky / Blacksky','PDS fallback; PLC'],purple)
box(348,796,425,172,'Prefect jobs','Home / heavypad',['Zig snapshot builder on home disk','Identity hourly · profiles daily','PLC weekly'],purple)
box(933,796,300,172,'R2 artifacts','Cloudflare',['index.db + manifest','Versioned snapshots'],purple)
path('M268 880 H348','batch')
path('M830 681 V722 H600 V796','batch');t(627,712,'export','small',purple)
path('M720 796 V753 H985 V681','batch');t(811,778,'enrich','small',purple)
path('M773 874 H933','batch');t(796,853,'publish','small',purple)
path('M1233 874 H1392 V272 H1363','batch');t(1260,746,'verify, then','small',purple);t(1260,773,'promote','small',purple)
t(48,1020,'Failure boundaries','label');t(242,1020,'Late build → keep prior snapshot. Engine down → uncached empty results.','body')
t(48,1053,'Logfire observes both Fly services; Workers Logs covers the edge. Operator access to heavypad uses Tailscale.','small')
s.append('</svg>');(out/'architecture.svg').write_text('\n'.join(s))
