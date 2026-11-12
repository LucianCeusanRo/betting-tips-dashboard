"""Read-only local dashboard over live_monitor/out/latest.json."""
from __future__ import annotations
import json, re, time, unicodedata
from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any
from fastapi import FastAPI, Query
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

HERE=Path(__file__).resolve().parent; PROJECT=HERE.parent
SNAPSHOT=PROJECT/'live_monitor'/'out'/'latest.json'; HISTORY=SNAPSHOT.parent/'snapshots.jsonl'
CACHE=PROJECT.parent/'.cs2_cache'; LIVE_CACHE=CACHE/'live_data.json'; RANK_CACHE=CACHE/'hltv_apify_rankings.json'
BANK=100.0; KELLY=.25; MAX_STAKE=2.0; MIN_EDGE=2.0
app=FastAPI(title='Supreme Punter — Live Monitor',docs_url='/docs')
app.mount('/static',StaticFiles(directory=HERE/'static'),name='static')
_last=None; _ctx_cache={}; _history_cache=(None,{})

def read_json(path,default):
    try:return json.loads(path.read_text(encoding='utf-8-sig'))
    except Exception:return default

def snapshot():
    global _last
    if not SNAPSHOT.exists(): return {'ts':None,'sources':{},'matches':[],'errors':{},'dashboard_message':'Nu există date. Pornește: python -m live_monitor.monitor --interval 30'}
    for _ in range(4):
        try:
            d=json.loads(SNAPSHOT.read_text(encoding='utf-8-sig')); d.setdefault('matches',[]); d.setdefault('sources',{}); _last=d; return d
        except (OSError,json.JSONDecodeError): time.sleep(.04)
    if _last:
        d=dict(_last); d['dashboard_message']='Se afișează ultimul snapshot valid (monitorul scrie acum).'; return d
    return {'ts':None,'sources':{},'matches':[],'errors':{'dashboard':'Snapshot temporar indisponibil'}}

def norm(v):
    s=unicodedata.normalize('NFKD',str(v or '')); s=''.join(c for c in s if not unicodedata.combining(c)).lower()
    return re.sub(r'[^a-z0-9]+','',s)

def selkey(name,teams):
    n=norm(name)
    if n in ('1','home','gazde'): return 's0'
    if n in ('2','away','oaspeti'): return 's1'
    if n in ('x','draw','egal'): return 'draw'
    scores=[]
    for i,t in enumerate(teams[:2]):
        nt=norm(t)
        if n==nt:return f's{i}'
        if n and nt and (n in nt or nt in n): scores.append((min(len(n),len(nt))/max(len(n),len(nt)),i))
        else: scores.append((SequenceMatcher(None,n,nt).ratio(),i))
    score,i=max(scores,default=(0,0)); return f's{i}' if score>=.55 else None

def market(book):
    ms=book.get('markets') or {}
    if isinstance(ms,list): ms={str(m.get('key',i)):m for i,m in enumerate(ms)}
    for k in ('match_winner','1x2','map_winner_1','map_winner'):
        if isinstance(ms.get(k),dict) and ms[k].get('selections'): return k,ms[k]
    for k,m in ms.items():
        if isinstance(m,dict) and m.get('selections') and 'winner' in str(k).lower(): return str(k),m
    legacy=book.get('match_winner') or {}
    if legacy:return 'match_winner',{'name':'Câștigător meci','selections':[{'name':n,'price':v} for n,v in legacy.items()]}

def quotes(book,teams):
    got=market(book)
    if not got:return None
    k,m=got; q={}
    for s in m.get('selections',[]):
        try:o=float(s.get('price'))
        except (TypeError,ValueError):continue
        key=selkey(s.get('name',''),teams)
        if key and o>1 and str(s.get('status','active')).lower() in ('active','open',''):q[key]={'price':o,'name':s.get('name')}
    return (k,m.get('name') or 'Câștigător meci',q) if q else None

def display(key,teams):return teams[0] if key=='s0' else teams[1] if key=='s1' else 'Egal'

def no_vig(q,key):
    inv={k:1/v['price'] for k,v in q.items()}; total=sum(inv.values()); return inv.get(key,0)/total if total else 0

def stake(p,o):
    b=o-1
    if b<=0:return 0
    raw=min(max((p*b-(1-p))/b,0)*KELLY*BANK,MAX_STAKE)
    return round(raw*2)/2 if raw>=.25 else 0

def parse_dt(v):
    try:
        d=datetime.fromisoformat(str(v).replace('Z','+00:00'))
        return d if d.tzinfo else d.replace(tzinfo=timezone(timedelta(hours=3)))
    except Exception:return None

def history_index(current):
    global _history_cache
    stamp=(HISTORY.stat().st_mtime_ns,HISTORY.stat().st_size) if HISTORY.exists() else None
    if _history_cache[0]==stamp:return _history_cache[1]
    events={}
    if HISTORY.exists():
        try:
            # Bounded tail: enough for recent schedule without loading an unlimited log.
            with HISTORY.open('rb') as f:
                size=f.seek(0,2); f.seek(max(0,size-6_000_000)); raw=f.read().decode('utf-8','ignore')
            for line in raw.splitlines()[1 if size>6_000_000 else 0:]:
                try:d=json.loads(line)
                except Exception:continue
                for m in d.get('matches',[]):
                    start=parse_dt(m.get('start') or d.get('ts')); mid=str(m.get('id') or (m.get('teams'),start))
                    if start:
                        for team in m.get('teams',[]): events[(norm(team),mid)]=start
        except Exception:pass
    for m in current.get('matches',[]):
        start=parse_dt(m.get('start') or current.get('ts')); mid=str(m.get('id'))
        if start:
            for team in m.get('teams',[]):events[(norm(team),mid)]=start
    idx={}
    for (team,mid),dt in events.items():idx.setdefault(team,[]).append((mid,dt))
    _history_cache=(stamp,idx); return idx

def fatigue(team,current):
    now=parse_dt(current.get('ts')) or datetime.now(timezone.utc); starts=history_index(current).get(norm(team),[])
    recent=sorted({(mid,dt) for mid,dt in starts if now-timedelta(days=3)<=dt<=now+timedelta(hours=8)},key=lambda x:x[1])
    late=sum(1 for _,d in recent if d.astimezone(timezone(timedelta(hours=3))).hour>=22 or d.astimezone(timezone(timedelta(hours=3))).hour<6)
    back=any((b[1]-a[1]).total_seconds()<6*3600 for a,b in zip(recent,recent[1:]))
    n=len(recent); level='ridicat' if n>=4 or back else 'mediu' if n>=3 or late else 'scăzut'
    if level=='ridicat': label=f'Oboseală posibilă · {n} meciuri/3 zile'+(' · back-to-back' if back else '')
    elif level=='mediu':label=f'Program încărcat · {n} meciuri/3 zile'+(' · oră târzie' if late else '')
    else:label=f'Fără semnal major · {n} meci(uri)/3 zile'
    return {'level':level,'label':label,'matches_3d':n,'late_matches':late,'back_to_back':back,'basis':'istoric snapshots local'}

def load_context():
    global _ctx_cache
    stamps=tuple((p.stat().st_mtime_ns if p.exists() else 0) for p in (LIVE_CACHE,RANK_CACHE))
    if _ctx_cache.get('_stamps')==stamps:return _ctx_cache
    live=read_json(LIVE_CACHE,{}).get('teams',{}); ranks=read_json(RANK_CACHE,{}).get('teams',[])
    _ctx_cache={'_stamps':stamps,'live':live,'ranks':ranks}; return _ctx_cache

def best_team(team):
    c=load_context(); target=norm(team); best=None; score=0
    def richness(data):
        roster=data.get('roster') or []
        return sum(1 for x in roster if x.get('name','').lower()!='nodata' and x.get('kd') is not None)+sum(v is not None and v!='N/A' for v in (data.get('rank'),data.get('rating'),data.get('form')))
    for name,data in c['live'].items():
        n=norm(name); s=1 if n==target else SequenceMatcher(None,target,n).ratio()+(.12 if target in n or n in target else 0)
        if s>score or (s==score and (best is None or richness(data)>richness(best[1]))):score=s;best=(name,data)
    rankbest=None; rs=0
    for r in c['ranks']:
        n=norm(r.get('name')); s=1 if n==target else SequenceMatcher(None,target,n).ratio()+(.12 if target in n or n in target else 0)
        if s>rs:rs=s;rankbest=r
    data=dict(best[1]) if best and score>=.72 else {}
    # Duplicate aliases in the existing cache can carry complementary fields.
    for name,extra in c['live'].items():
        if norm(name)==target:
            for field in ('rank','rating','form'):
                if data.get(field) in (None,'', 'N/A',999) and extra.get(field) not in (None,'','N/A',999): data[field]=extra[field]
    if rankbest and rs>=.72:data.setdefault('rank',rankbest.get('rank'));data.setdefault('roster',[{'name':x} for x in rankbest.get('players',[])])
    roster=data.get('roster') or []; kds=[]
    for x in roster:
        try:
            if str(x.get('name','')).lower()!='nodata' and x.get('kd') is not None:kds.append(float(x['kd']))
        except Exception:pass
    rank=data.get('rank'); form=data.get('form'); quality=data.get('data_quality','cached') if data else 'indisponibil'
    rating=data.get('rating')
    if quality=='estimated' and rank in (None,999) and form in (None,'N/A',''): rating=None
    faceit_ok=len(kds)>=3 and (max(kds)-min(kds)>.02 or any(x.get('utility_damage_per_round') is not None for x in roster))
    return {'available':bool(data),'matched_name':best[0] if best and score>=.72 else (rankbest.get('name') if rankbest and rs>=.72 else None),'hltv_rank':rank if rank not in (None,999) else None,'hltv_rating':rating,'hltv_form':form if form not in (None,'N/A','') else None,'faceit_rating':round(sum(kds)/len(kds),2) if faceit_ok else None,'faceit_metric':'K/D mediu roster (cache Faceit)' if faceit_ok else None,'data_quality':quality}

def context_for(team,current):
    d=best_team(team);d['team']=team;d['fatigue']=fatigue(team,current);return d

def tips(current):
    out=[]
    for m in current.get('matches',[]):
        teams=[str(x) for x in m.get('teams',[])];books=m.get('books') or {}
        if len(teams)<2:continue
        parsed={b:quotes(books[b],teams) for b in ('betano','superbet') if isinstance(books.get(b),dict)}
        parsed={k:v for k,v in parsed.items() if v}
        if len(parsed)<2:continue
        bk,bn,bq=parsed['betano'];sk,sn,sq=parsed['superbet']
        if bk!=sk and {bk,sk}!={'match_winner','1x2'}:continue
        for key in set(bq)&set(sq):
            pair=sorted([('betano',bq[key]),('superbet',sq[key])],key=lambda x:x[1]['price'],reverse=True)
            best,bo=pair[0];ref,ro=pair[1];edge=(bo['price']/ro['price']-1)*100
            if edge<MIN_EDGE:continue
            rq=bq if ref=='betano' else sq;p=no_vig(rq,key);ev=(p*bo['price']-1)*100
            if ev<=0:continue
            selected=display(key,teams);ctx=context_for(selected,current); penalty={'ridicat':2.0,'mediu':.75}.get(ctx['fatigue']['level'],0)
            out.append({'match_id':m.get('id'),'match':' vs '.join(teams),'sport':str(m.get('sport','')).upper(),'league':m.get('league'),'score':m.get('score'),'market':bn if best=='betano' else sn,'selection':selected,'book':best.capitalize(),'book_key':best,'book_url':books[best].get('url'),'odds':round(bo['price'],2),'reference_book':ref.capitalize(),'reference_odds':round(ro['price'],2),'estimated_probability_pct':round(p*100,2),'edge_pct':round(edge,2),'ev_pct':round(ev,2),'stake_lei':stake(p,bo['price']),'profitability':'Potențial ridicat' if ev>=8 else 'Potențial bun' if ev>=4 else 'Avantaj moderat','context':ctx,'ranking_score':round(ev-penalty,2),'disclaimer':'Estimare, nu garanție. Pariul se plasează numai manual.'})
    return sorted(out,key=lambda x:(x['ranking_score'],x['edge_pct']),reverse=True)

@app.get('/')
def index():return FileResponse(HERE/'static'/'index.html')
@app.get('/api/live')
def live(refresh:bool=Query(False)):
    d=dict(snapshot());d['dashboard']={'snapshot_path':str(SNAPSHOT),'match_count':len(d.get('matches',[])),'read_only':True,'refreshed':refresh};return JSONResponse(d)
@app.get('/api/tips')
def api_tips():
    d=snapshot();t=tips(d);return JSONResponse({'ts':d.get('ts'),'tips':t,'count':len(t),'bankroll_lei':BANK,'kelly_fraction':'1/4','max_stake_lei':MAX_STAKE,'minimum_line_edge_pct':MIN_EDGE,'method':'Line shopping; probabilitate fără marjă din cealaltă casă; ordonare penalizată ușor pentru oboseală.','disclaimer':'Estimări, nu garanții. Nicio plasare automată.','message':d.get('dashboard_message')})
@app.get('/api/context')
def api_context():
    d=snapshot(); names=[]
    for m in d.get('matches',[]):
        if str(m.get('sport','')).lower()=='cs2':names.extend(m.get('teams',[]))
    seen=set();rows=[]
    for n in names:
        if norm(n) not in seen:seen.add(norm(n));rows.append(context_for(str(n),d))
    return JSONResponse({'ts':d.get('ts'),'teams':rows,'sources':{'hltv':'cache hltv_engine + clasament Apify (read-only)','faceit':'agregat roster din cache hltv_engine/FaceitProvider','fatigue':'snapshots.jsonl deduplicat, fereastră 3 zile'},'news':{'status':'coming_soon','message':'Știri verificate: în curând. Nu folosim headline-uri neverificate în recomandări.'}})
