"""Generate the operator dashboard; no credentials in its JSON."""
import json
from pathlib import Path
P='typeahead-prometheus'
F='typeahead-fly'
panels=[]

def panel(title,expr,x,y,w=6,h=4,kind='stat',unit='short',ds=P,description='',thresholds=None,legend=''):
    defaults={'unit':unit,'noValue':'NO DATA','color':{'mode':'thresholds'},'thresholds':{'mode':'absolute','steps':thresholds or [{'color':'green','value':None}]}}
    if kind=='timeseries':
        defaults['color']={'mode':'palette-classic'}
        defaults['custom']={'drawStyle':'line','lineWidth':2,'fillOpacity':8,'showPoints':'never','spanNulls':False,'axisPlacement':'auto'}
    item={'id':len(panels)+1,'title':title,'description':description,'type':kind,'gridPos':{'x':x,'y':y,'w':w,'h':h},'datasource':{'type':'prometheus','uid':ds},'targets':[{'refId':'A','expr':expr,'legendFormat':legend,'range':kind=='timeseries','instant':kind=='stat'}],'fieldConfig':{'defaults':defaults,'overrides':[]},'options':{'reduceOptions':{'calcs':['lastNotNull'],'fields':'','values':False},'colorMode':'value','graphMode':'area','textMode':'auto','legend':{'displayMode':'list','placement':'bottom','showLegend':True},'tooltip':{'mode':'multi','sort':'desc'}}}
    panels.append(item)
    return item

def row(title,y):
    panels.append({'id':len(panels)+1,'type':'row','title':title,'collapsed':False,'gridPos':{'x':0,'y':y,'w':24,'h':1},'panels':[]})

bad=[{'color':'red','value':None},{'color':'green','value':1}]
row('Can people search—and are results fresh?',0)
a=panel('Search + freshness', 'min(typeahead_freshness_ok * typeahead_search_probe_ok * typeahead_search_ready) * min(typeahead_endpoint_up)',0,1,thresholds=bad,description='Live serving-path probe plus snapshot/overlay freshness and all four endpoint checks. NO DATA is not healthy.')
a['fieldConfig']['defaults']['mappings']=[{'type':'value','options':{'0':{'text':'CHECK','color':'red'},'1':{'text':'HEALTHY','color':'green'}}}]
panel('Overlay lag','typeahead_overlay_lag_seconds',6,1,unit='s',thresholds=[{'color':'green','value':None},{'color':'orange','value':600},{'color':'red','value':1800}],description='Turso → search overlay. Polls every five minutes. Warning 10m; stale 30m.')
panel('Snapshot age','typeahead_snapshot_age_seconds',12,1,unit='s',thresholds=[{'color':'green','value':None},{'color':'orange','value':259200},{'color':'red','value':345600}],description='Age of the attached artifact. Warning 3 days; stale 4 days. This checks the deployed output, not just whether Prefect completed.')
panel('Search disk available','fly_instance_filesystem_blocks_avail{app="typeahead-search",mount="/data"} * fly_instance_filesystem_block_size{app="typeahead-search",mount="/data"}',18,1,unit='bytes',ds=F,thresholds=[{'color':'red','value':None},{'color':'orange','value':14000000000},{'color':'green','value':21000000000}],description='Reserve room to promote another ~14 GB snapshot. Warning below 1.5× artifact size. Limits must change if artifact grows.')
panel('Overlay lag over time','typeahead_overlay_lag_seconds',0,5,12,7,'timeseries','s',description='A healthy five-minute sawtooth should keep resetting.')
panel('Search response time · Fly P95','histogram_quantile(0.95, sum by (le) (rate(fly_app_http_response_time_seconds_bucket{app="typeahead-search",status="2xx"}[5m])))',12,5,12,7,'timeseries','s',F,description='Fly app HTTP latency over five minutes, including health/status requests. Not Logfire engine-only P95, and not Cloudflare cache-hit latency.')
row('Fly search + ingester',12)
panel('Requests reaching Fly','sum by (app) (rate(fly_app_http_responses_count{app=~"typeahead-search|typeahead-ingester"}[5m]))',0,13,8,7,'timeseries','reqps',F,legend='{{app}}',description='Cloudflare cache hits do not reach Fly. Includes probes and status requests.')
panel('Fly 5xx responses','sum by (app) (increase(fly_app_http_responses_count{app=~"typeahead-search|typeahead-ingester",status="5xx"}[5m])) or on(app) (0 * sum by(app) (increase(fly_app_http_responses_count{app=~"typeahead-search|typeahead-ingester"}[5m])))',8,13,8,7,'timeseries','short',F,legend='{{app}}',description='5xx per rolling five minutes. Zero is filled only when other HTTP counter observations exist for that app; total telemetry loss remains NO DATA.')
panel('Memory pressure','100 * (1 - fly_instance_memory_mem_available{app=~"typeahead-search|typeahead-ingester"} / fly_instance_memory_mem_total{app=~"typeahead-search|typeahead-ingester"})',16,13,8,7,'timeseries','percent',F,legend='{{app}}',description='Uses MemAvailable, so reclaimable page cache is counted as available.')
panel('Machine uptime','fly_instance_uptime_seconds{app=~"typeahead-search|typeahead-ingester"}',0,20,8,7,'timeseries','s',F,legend='{{app}}',description='A reset identifies a machine restart, not necessarily a process-only restart.')
panel('CPU throttling','sum by(app) (rate(fly_instance_cpu_throttle{app=~"typeahead-search|typeahead-ingester"}[5m])) / 100',8,20,8,7,'timeseries','s',F,legend='{{app}}',description='Seconds throttled per second, from Fly’s centisecond counter. Counter resets are handled by rate().')
panel('Search disk available over time','fly_instance_filesystem_blocks_avail{app="typeahead-search",mount="/data"} * fly_instance_filesystem_block_size{app="typeahead-search",mount="/data"}',16,20,8,7,'timeseries','bytes',F)
row('Ingest signals · identity queue is not the whole firehose',27)
panel('Identity queue','typeahead_identity_queued',0,28,6,4,description='Identity-resolution queue only. Does not measure the main repository-event backlog.',thresholds=[{'color':'green','value':None},{'color':'orange','value':100},{'color':'red','value':1000}])
panel('Identity drops · last hour','increase(typeahead_identity_dropped_total[1h])',6,28,6,4,thresholds=[{'color':'green','value':None},{'color':'red','value':1}],description='Counter-reset aware. Repository-event drops are a separate Logfire signal.')
panel('Identity events processed','rate(typeahead_identity_processed_total[5m])',12,28,12,7,'timeseries','ops',legend='processed / sec')
panel('Overlay actors','typeahead_overlay_actors',0,32,12,6,'timeseries',description='Recent actor rows above the attached snapshot; should compact after promotion.')
row('Home builds + monitoring health',38)
panel('Heavypad reachable','up{job="heavypad"}',0,39,6,4,thresholds=bad,description='Existing node exporter over Tailscale.')
panel('Heavypad disk available','node_filesystem_avail_bytes{job="heavypad",mountpoint="/"}',6,39,6,4,unit='bytes')
panel('Heavypad RAM available','node_memory_MemAvailable_bytes{job="heavypad"}',12,39,6,4,unit='bytes')
panel('Probe data age','max(time() - typeahead_probe_timestamp_seconds)',18,39,6,4,unit='s',thresholds=[{'color':'green','value':None},{'color':'orange','value':120},{'color':'red','value':180}],description='Age of the oldest successfully parsed endpoint response. Check individual endpoint status below too.')
panel('Endpoint checks','typeahead_endpoint_up',0,43,12,6,'timeseries',legend='{{source}}',description='1 = HTTP 200 + valid JSON. Failed reads discard prior values.')
panel('Heavypad load','node_load1{job="heavypad"}',12,43,12,6,'timeseries',legend='1m load')
panels.append({'id':len(panels)+1,'type':'text','title':'When something is wrong','gridPos':{'x':0,'y':49,'w':24,'h':5},'options':{'mode':'markdown','content':'**Search fails:** check Fly resources, then search traces. **Overlay lag grows:** inspect projection and ingest writes. **Snapshot ages:** inspect the Prefect build and R2 promotion. **Disk falls:** leave room for the next snapshot.\n\n[Logfire · traces and ingest drops](https://logfire-us.pydantic.dev/waow/typeahead) · [Prefect · builds and enrichment](https://prefect-server.waow.tech) · [Fly managed metrics](https://fly-metrics.net) · [Typeahead stats](https://typeahead.waow.tech/stats)\n\n**Coverage:** endpoint probes run every minute. New probe history starts with this dashboard; Prometheus retains 7 days, Fly about 15 days. Identity counters do not prove the full firehose is caught up. Main ingest backlog/drop events, per-deployment Prefect outcomes, and Worker-specific error/cache rates still require the linked tools. No notification rules have been enabled.'}})
dashboard={'uid':'typeahead-health','title':'Typeahead · system health','tags':['typeahead','operations'],'timezone':'browser','schemaVersion':39,'version':0,'refresh':'1m','time':{'from':'now-6h','to':'now'},'panels':panels,'links':[{'title':'Logfire','url':'https://logfire-us.pydantic.dev/waow/typeahead','type':'link','targetBlank':True},{'title':'Prefect','url':'https://prefect-server.waow.tech','type':'link','targetBlank':True}]}
Path(__file__).with_name('dashboard.json').write_text(json.dumps(dashboard,indent=2)+'\n')
