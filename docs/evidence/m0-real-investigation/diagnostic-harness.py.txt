import asyncio,json,os,time,sys
from pathlib import Path
import psycopg
from scripts.m0 import live
from scripts.m0.config import load_config
from scripts.m0.postgres_lab import verify_server
p=Path('tmp/m0-real-investigation'); n=sys.argv[1]
def save(name,obj):
 fd=os.open(p/name,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
 with os.fdopen(fd,'w') as f: json.dump(obj,f,indent=2,default=str)
c=load_config(Path('/Users/shenghuikevin/dev/AI/production-ops-agent/.env'))
a=live.read_contract((p/('approval-'+n+'.json')).resolve())
c.values['OPSPILOT_EXPERIMENT_DEADLINE_UTC']=a['deadline']; c.values['OPSPILOT_EXPERIMENT_BUDGET_CNY']='2.00'
live.validate(a,c); verify_server()
original=live.Wire.request
async def observed(self,slot,*args,**kwargs):
 t=time.monotonic();r=await original(self,slot,*args,**kwargs)
 if slot=='model-2':
  content=r.get('choices',[{}])[0].get('message',{}).get('content')
  s={'content_is_string':type(content)is str,'length':len(content) if type(content)is str else None,'request_seconds':time.monotonic()-t}
  if type(content)is str:
   s.update(starts_json_fence=content.startswith('```json\n'),ends_fence=content.rstrip().endswith('```'))
   try: value=json.loads(content);s['json_valid']=True
   except ValueError: value=None;s['json_valid']=False
   s['strict_contract_match']=value=={'target':'m0-target-a','evidence_id':'m0-evidence-a'}
   s['fenced_inner_strict_match']=False
   if s['starts_json_fence'] and s['ends_fence']:
    try:s['fenced_inner_strict_match']=json.loads(content.rstrip()[8:-3])=={'target':'m0-target-a','evidence_id':'m0-evidence-a'}
    except ValueError:pass
   # Only persist exact known synthetic content or its JSON Markdown wrapper, never arbitrary text.
   if s['strict_contract_match'] or s['fenced_inner_strict_match']:s['known_fixture_final_content']=content
  save('final-diagnostic-'+n+'.json',s)
 return r
live.Wire.request=observed
save('started-'+n+'.json',{'started':live.utcnow(),'experiment_id':a['experiment_id'],'run_id':a['run_id'],'code_sha256':live.code_digest(),'wrapper_sha256':live.digest(Path(__file__).read_bytes())})
t=time.monotonic();result=asyncio.run(live.execute(a,c,live.LiveLedger(a['database_dsn'])))
with psycopg.connect(a['database_dsn']) as db:
 row=db.execute('SELECT row_to_json(t) FROM m0_live_once t WHERE experiment_id=%s',(a['experiment_id'],)).fetchone()[0]
 diag=db.execute('SELECT row_to_json(t) FROM m0_live_diagnostics t WHERE experiment_id=%s',(a['experiment_id'],)).fetchone()[0]
save('result-'+n+'.json',{'result':result,'elapsed_seconds':time.monotonic()-t,'row':row,'diagnostics':diag,'ended':live.utcnow()})
print(json.dumps(result));print('elapsed_seconds',round(time.monotonic()-t,3))
