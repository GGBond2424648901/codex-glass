"""Approved design illustrative data, exclusively for visual regression tests."""
from datetime import datetime,timedelta
from tests.glass_fixtures import telemetry

def dashboard_fixture():
    data=telemetry();names=['gpt-6-astra','gpt-5.6-sol','gpt-5.6-terra','gpt-5.6-luna']
    rows=[]
    for name,inputs,cache,output,reasoning,cost,calls in [('gpt-6-astra',350000000,340000000,4300000,1100000,498.57,10000),('gpt-5.6-sol',347690000,340550000,1220000,79580,189.27,4500),('gpt-5.6-terra',4200000,3600000,550000,120000,2.22,77),('gpt-5.6-luna',10000,0,0,0,.01,1)]:
        rows.append(dict(model=name,input_tokens=inputs,cached_input_tokens=cache,output_tokens=output,reasoning_output_tokens=reasoning,total_tokens=inputs+output,estimated_cost_usd=cost,calls=calls))
    data['windows']['today']={'by_model':rows,'total':{k:sum(r[k] for r in rows) for k in rows[0] if k!='model'}}
    data['source']={'files':217,'imported_sources':1,'imported_usage_events':533293,'imported_rate_limit_snapshots':549409}
    data['rate_limits']['limits'][0]['used_percent']=28
    # Use actual five-minute buckets in fixtures as well as production.
    now=datetime.now().replace(second=0,microsecond=0);now=now.replace(minute=now.minute//5*5)
    shape=[40,65,80,120,110,140,160,190,125,110,150,170,225,180,205,260,275,300,220,175,155,130,165,180,150,135,170,165,190,200,235,260,215,230,200,180,190,210,190,200,210,230,260,280,310,300,330,310,345,330,360,310,300,270,290,300,310,320,330,340,239.9]
    data['charts']['last_5_hours']['values']=[v*1000 for v in shape];data['charts']['last_5_hours']['labels']=[(now-timedelta(minutes=(60-i)*5)).isoformat() for i in range(61)]
    data['charts']['today']={**data['charts']['last_5_hours'],'values':[v*1000 for v in shape[-7:]],'labels':data['charts']['last_5_hours']['labels'][-7:]}
    context={'index_path':'fixture-only.sqlite3','sources':[{'source_id':'PC-2026-09-08','first_imported_at':now.timestamp(),'source_files':217,'source_usage_events':533293,'source_rate_limit_snapshots':549409}]}
    events=[{'timestamp':(now-timedelta(minutes=i*4)).isoformat(sep=' '),'model':names[i%4],'cwd':f'工作区 {i%3+1:02d}','tokens':{'input':(i+1)*26000,'cached_input':i*24000,'output':1450,'total':(i+1)*26000+1450},'cost_usd':{'total':.26*(i+1)},'pricing_source':'direct'} for i in range(7)]
    return data,context,events
