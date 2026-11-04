import os,pathlib,subprocess,json,time,threading,http.server,resource,signal
os.umask(0o077)
root=pathlib.Path('/tmp/diagnostic');root.mkdir(exist_ok=True);started=time.time();kind=os.environ['PROBE_KIND']
class Handler(http.server.BaseHTTPRequestHandler):
 def do_GET(self):
  self.send_response(200);self.send_header('Content-Length','2');self.end_headers();self.wfile.write(b'ok');self.wfile.flush()
 def log_message(self,*args):pass
server=http.server.ThreadingHTTPServer(('127.0.0.1',8123),Handler);threading.Thread(target=server.serve_forever,daemon=True).start()
(root/'curl-version.txt').write_text(subprocess.check_output(['curl','--version'],text=True))
(root/'meminfo-start.txt').write_text(pathlib.Path('/proc/meminfo').read_text())
(root/'proc-stat-start.txt').write_text(pathlib.Path('/proc/stat').read_text())
keyfile=root/'tls.keys';env={**os.environ,'SSLKEYLOGFILE':str(keyfile)}
with (root/'tcpdump.stderr').open('w') as errors:
 capture=subprocess.Popen(['tcpdump','-i','any','-U','-s','0','-w',str(root/'capture.pcap'),'tcp port 443 or tcp port 8123'],stderr=errors,stdout=subprocess.DEVNULL)
 try:
  time.sleep(1)
  targets=[('cf-builtin','https://typeahead.waow.tech/cdn-cgi/trace'),('worker-static','https://typeahead.waow.tech/llms.txt'),('search','https://typeahead.waow.tech/xrpc/app.bsky.actor.searchActorsTypeahead?q=jola.dev&limit=19')]
  with (root/'results.jsonl').open('w') as results:
   i=0
   for iteration in range(5):
    for family in ['4','6']:
     for protocol in ['1.1','2']:
      for target,url in targets:
       i+=1;args=['curl','-v','--trace-time','--http'+protocol,'--ipv'+family,'--connect-timeout','3','--max-time','6','-H','X-Typeahead-Probe: eu-causal-20261104','-H','X-Client: bench','-A','typeahead-causal-diagnostic/1.0','-o','/dev/null','-w','%{json}',url]
       before=resource.getrusage(resource.RUSAGE_CHILDREN);begin=time.time_ns();p=subprocess.run(args,env=env,capture_output=True,text=True,timeout=9);end=time.time_ns();after=resource.getrusage(resource.RUSAGE_CHILDREN)
       (root/f'curl-{i:03d}.log').write_text(p.stderr)
       try:d=json.loads(p.stdout)
       except ValueError:d={}
       selected={k:d.get(k) for k in ['time_namelookup','time_connect','time_appconnect','time_starttransfer','time_total','local_ip','local_port','remote_ip','remote_port','http_code','http_version','num_connects','exitcode','errormsg']}
       record={'id':i,'kind':kind,'iteration':iteration,'target':target,'family':family,'protocol':protocol,'started_ns':begin,'finished_ns':end,'elapsed_ms':(end-begin)/1e6,'child_cpu_ms':1000*(after.ru_utime+after.ru_stime-before.ru_utime-before.ru_stime),'returncode':p.returncode,**selected};results.write(json.dumps(record)+'\n');results.flush();time.sleep(.5)
    i+=1;begin=time.time_ns();p=subprocess.run(['curl','-v','--trace-time','--max-time','3','-o','/dev/null','-w','%{json}','http://127.0.0.1:8123/ping'],capture_output=True,text=True,timeout=5);end=time.time_ns();(root/f'curl-{i:03d}.log').write_text(p.stderr);d=json.loads(p.stdout);results.write(json.dumps({'id':i,'kind':kind,'target':'loopback','started_ns':begin,'finished_ns':end,'elapsed_ms':(end-begin)/1e6,**{k:d[k] for k in ['time_connect','time_starttransfer','time_total','local_port','remote_ip','remote_port','http_code']}})+'\n');results.flush()
 finally:
  capture.send_signal(signal.SIGINT);capture.wait(timeout=10);server.shutdown()
(root/'meminfo-end.txt').write_text(pathlib.Path('/proc/meminfo').read_text())
(root/'proc-stat-end.txt').write_text(pathlib.Path('/proc/stat').read_text())
fields=['frame.time_epoch','ip.src','ipv6.src','tcp.srcport','tcp.dstport','tcp.stream','tcp.seq','tcp.ack','tcp.len','tcp.flags','tcp.analysis.retransmission','tcp.analysis.fast_retransmission','tcp.analysis.duplicate_ack','tcp.analysis.ack_rtt','tls.record.content_type','http.request.method','http.request.uri','http.response.code','http2.type','http2.streamid','http2.headers.method','http2.headers.path','http2.headers.status']
args=['tshark','-r',str(root/'capture.pcap'),'-o','tls.keylog_file:'+str(keyfile),'-T','json']
for field in fields:args.extend(['-e',field])
with (root/'packets.json').open('w') as output,(root/'tshark.stderr').open('w') as errors:
 p=subprocess.run(args,stdout=output,stderr=errors,timeout=90)
(root/'summary.json').write_text(json.dumps({'kind':kind,'started_at':started,'finished_at':time.time(),'keylog_present':keyfile.exists() and keyfile.stat().st_size>0,'pcap_bytes':(root/'capture.pcap').stat().st_size,'tshark_exit':p.returncode,'sample_count':i,'scratch_budget_bytes':1073741824,'payloads_retained':False},indent=2))
keyfile.unlink(missing_ok=True);(root/'capture.pcap').unlink()
if p.returncode:raise RuntimeError('tshark extraction failed')
print('TYPEAHEAD_DIAGNOSTIC_COMPLETE',flush=True)
