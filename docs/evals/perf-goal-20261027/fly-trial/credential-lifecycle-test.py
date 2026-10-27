import json,pathlib,subprocess,uuid,shlex
name='typeahead-perf-cred-test-'+uuid.uuid4().hex[:8]
root=pathlib.Path('/run')/name
root.mkdir(mode=0o700)
(root/'token').write_text('dummy-fixture')
(root/'token').chmod(0o600)
check="import pathlib; assert pathlib.Path("+repr(str(root/'token'))+").read_text()=='dummy-fixture'"
command=['/usr/bin/python3','-c',check]
args=['systemd-run','--unit',name,'--wait','--collect','--property=User=root','--property=ReadWritePaths='+str(root),'--property=ProtectSystem=strict','--property=NoNewPrivileges=yes','--property=CapabilityBoundingSet=','--property=ExecStopPost='+shlex.join(['/usr/bin/python3','-c',check+'; pathlib.Path('+repr(str(root/'token'))+').unlink()']),*command]
result=subprocess.run(args,capture_output=True,text=True,timeout=30)
assert result.returncode==0,result.stderr
assert not (root/'token').exists(),'runtime credential source not retired'
root.rmdir()
print(json.dumps({'exec_start_and_stop_post_read_passed':True,'runtime_source_removed':True,'credential_kind':'nonsecret dummy fixture'}))
