"""Bounded live DeepSeek prompt-token reconciliation; never writes credentials."""
from __future__ import annotations
import hashlib,json,os,sys,time,urllib.request
from pathlib import Path
from opspilot.investigation.context import estimate_tokens
from opspilot.tools.tokens import count_tokens
from opspilot.tools.registry import canonical

ENDPOINT='https://api.deepseek.com/chat/completions'
MODEL='deepseek-flash'

def key_from_env():
    p=os.environ.get('M0_ENV_FILE')
    if not p: raise SystemExit('M0_ENV_FILE required')
    for line in Path(p).read_text().splitlines():
        if line.strip().startswith('DEEPSEEK_API_KEY='):
            v=line.split('=',1)[1].strip()
            return v[1:-1] if len(v)>=2 and v[0]==v[-1] and v[0] in "'\"" else v
    raise SystemExit('DEEPSEEK_API_KEY missing')

def content_count(messages,tools):
    # Same canonical per-message units used by the old estimator, but vendored tokenizer.
    n=sum(count_tokens(canonical(dict(m))) for m in messages)
    tool_n=count_tokens(canonical([dict(t) for t in tools])) if tools else 0
    return n,tool_n

def make_case(base_messages,tools,target,with_tools):
    msgs=list(base_messages[:2])
    # Keep the seed small so each requested size is distinct; it is still
    # copied verbatim from the rebuilt repository request.
    corpus=canonical(base_messages[:2])[:12000]
    # Use repository-shaped JSON/text, repeated only to reach a bounded target.
    filler=corpus
    lo,hi=1, max(2,target*8//max(1,count_tokens(filler)))
    while count_tokens(filler*hi)<target and hi<200000: hi*=2
    while lo<hi:
        mid=(lo+hi)//2
        if count_tokens(filler*mid)<target: lo=mid+1
        else: hi=mid
    msgs.append({'role':'user','content':filler*lo})
    # trim by binary search on repetitions if overshoot is large
    while len(msgs)>3 and count_tokens(canonical(msgs))>target*12//10:
        msgs[-1]['content']=msgs[-1]['content'][:int(len(msgs[-1]['content'])*0.9)]
    return msgs, (tools if with_tools else None)

def call(key,messages,tools):
    body={'model':MODEL,'messages':messages,'max_tokens':1}
    if tools: body['tools']=tools
    req=urllib.request.Request(ENDPOINT,data=json.dumps(body,ensure_ascii=False).encode(),headers={'Authorization':'Bearer '+key,'Content-Type':'application/json'})
    with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(req,timeout=180) as r:
        return json.loads(r.read())

def main():
    root=Path(__file__).parent
    base=json.loads((root/'rebuilt-20261001'/'normal-2'/'messages.json').read_text())
    tools=json.loads((root/'rebuilt-20261001'/'normal-2'/'tools.json').read_text())
    targets=[1000,10000,100000,300000,600000,900000]
    tool_flags=[False,True,False,True,False,True]
    key=key_from_env(); rows=[]
    for i,(target,wt) in enumerate(zip(targets,tool_flags),1):
        msgs,ts=make_case(base,tools,target,wt)
        old=estimate_tokens(msgs,ts)
        mc,tc=content_count(msgs,ts)
        started=time.time(); resp=call(key,msgs,ts); elapsed=round(time.time()-started,2)
        usage=resp.get('usage') or {}
        row={'case':f'live-{i:02d}','target_tokens':target,'with_tools':wt,'message_count':len(msgs),'request_bytes':len(json.dumps({'model':MODEL,'messages':msgs,'max_tokens':1,**({'tools':ts} if ts else {})},ensure_ascii=False).encode()),'request_sha256':hashlib.sha256(json.dumps({'model':MODEL,'messages':msgs,'max_tokens':1,**({'tools':ts} if ts else {})},ensure_ascii=False).encode()).hexdigest(),'old_estimate_tokens':old,'tokenizer_message_tokens':mc,'tokenizer_tools_tokens':tc,'tokenizer_content_tokens':mc+tc,'prompt_tokens':usage.get('prompt_tokens'),'completion_tokens':usage.get('completion_tokens'),'elapsed_s':elapsed}
        rows.append(row); print(json.dumps(row),flush=True)
    old=[]
    if (root/'live-results-initial.json').exists():
        old=json.loads((root/'live-results-initial.json').read_text())
    (root/'live-results.json').write_text(json.dumps(old+rows,indent=2)+'\n')
if __name__=='__main__': main()
