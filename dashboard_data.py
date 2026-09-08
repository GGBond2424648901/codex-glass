"""Shared native-view projections. Usage facts are never mutated by UI filters."""
import csv
import hashlib
from datetime import datetime, timedelta
from widget_data import compact, window_data

COLORS = ['#08c6e7','#994cff','#ff836a','#159cff','#8794b5','#21cdb1']
KNOWN_COLORS = {'gpt-6-astra':COLORS[0],'gpt-5.6-sol':COLORS[1], 'gpt-5.6-terra':COLORS[2], 'gpt-5.6-luna':COLORS[3], 'unknown':COLORS[4],'unknown-model':COLORS[4]}

def color_for(name):
    return KNOWN_COLORS.get(name,COLORS[int(hashlib.sha256(name.encode()).hexdigest()[:4],16)%len(COLORS)])

def token_structure(row):
    inputs=max(0,row.get('input_tokens',0)); cached=min(inputs,max(0,row.get('cached_input_tokens',0)))
    return [inputs-cached,cached,max(0,row.get('output_tokens',0))]

def scope_bounds(scope,now=None):
    now=now or datetime.now()
    start=now.replace(hour=0,minute=0,second=0,microsecond=0) if scope=='today' else now-timedelta(hours=5)
    if scope=='this_week':start=now.replace(hour=0,minute=0,second=0,microsecond=0)-timedelta(days=now.weekday())
    return ('' if scope=='all' else start.isoformat(sep=' ',timespec='seconds'), now.isoformat(sep=' ',timespec='seconds'))

def number(value):
    s=compact(value)
    return s[:-1].rstrip('0').rstrip('.')+s[-1] if s and s[-1] in 'KMBT' and '.' in s else s

def cost_text(row,unpriced=False):
    return '未计价' if unpriced else f'${row.get("estimated_cost_usd",0):,.2f}'

def model_rows(data,scope,query='',selected=None,workspace=False):
    _, models=window_data(data,scope)
    unknown={r['model'] for r in data.get('pricing',{}).get('unpriced_models',[])}
    if workspace:
        models=data.get('by_cwd',{}) if scope=='all' else data.get('windows',{}).get(scope,{}).get('by_cwd',{})
    rows=[]
    for name,stats in models.items():
        if query.casefold() not in name.casefold() or (selected is not None and name not in selected):continue
        rows.append(dict(stats,name=name,color=color_for(name),unpriced=name in unknown,
                         display_name='未定价模型' if name in ('unknown','unknown-model') else name))
    return sorted(rows,key=lambda r:r.get('total_tokens',0),reverse=True)

def coverage(data,scope):
    rows=model_rows(data,scope);total=sum(r.get('total_tokens',0) for r in rows)
    unknown=sum(r.get('total_tokens',0) for r in rows if r['unpriced'])
    return total,unknown,100*(total-unknown)/total if total else 100.0

def export_events(path,events):
    """Export only the visible selected page; guard spreadsheet formula cells."""
    def safe(v):
        s=str(v)
        return "'"+s if s.startswith(('=','+','-','@','\t','\r')) else s
    with open(path,'w',encoding='utf-8-sig',newline='') as stream:
        writer=csv.writer(stream);writer.writerow(['时间','模型','工作区','输入','缓存','输出','推理','Token','预估费用USD'])
        for row in events:
            t=row.get('tokens',{});writer.writerow([safe(row.get(k,'')) for k in ('timestamp','model','cwd')]+[t.get(k,0) for k in ('input','cached_input','output','reasoning_output','total')]+['未计价' if row.get('pricing_source')=='unpriced' else row.get('cost_usd',{}).get('total',0)])
