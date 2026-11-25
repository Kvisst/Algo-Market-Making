"""Four-instrument RITC market maker using LIMIT orders only. Python 3.10+, standard library only.
Run: python strategy.py --live
Use a dedicated Windows RIT practice account, with no other bot/manual orders.
Defaults are experimental, not calibrated or demonstrated profitable.
"""
import argparse
from dataclasses import dataclass
import datetime as dt
import getpass
import json
import math
import os
from pathlib import Path
import signal
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

TICKERS=('SPNG','SMMR','ATMN','WNTR')
STOP=False

def stop_signal(*_):
    global STOP
    STOP=True

class Halt(Exception): pass
class Uncertain(Halt): pass

class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self,*args,**kwargs): return None

class API:
    def __init__(self,base,key,log,gap=.13):
        p=urllib.parse.urlsplit(base)
        if p.scheme not in ('http','https') or not p.hostname or p.username or p.password or p.query or p.fragment:
            raise Halt('Invalid REST base URL')
        self.base=base.rstrip('/'); self.key=key; self.log=log; self.gap=gap; self.next=0.; self.uncertain=False
        self.opener=urllib.request.build_opener(urllib.request.ProxyHandler({}),NoRedirect())

    def event(self,kind,**fields):
        self.log.write(json.dumps(dict(kind=kind,utc=dt.datetime.now(dt.timezone.utc).isoformat(),**fields),allow_nan=False)+'\n')
        self.log.flush()

    def call(self,method,path,**params):
        if method=='POST' and path=='/orders' and params.get('type')!='LIMIT':
            raise Halt('Only LIMIT orders are permitted in this version')
        time.sleep(max(0,self.next-time.monotonic()))
        query=urllib.parse.urlencode(params)
        request=urllib.request.Request(self.base+path+('?' + query if query else ''),
                                       headers={'X-API-key':self.key},method=method)
        started=time.monotonic()
        # Write intent before sending: useful even if the process/connection fails.
        self.event('request',method=method,path=path,params=params,monotonic=started)
        try:
            with self.opener.open(request,timeout=2.) as response:
                data=json.loads(response.read())
            self.event('response',method=method,path=path,data=data,latency_ms=(time.monotonic()-started)*1000)
            return data
        except (urllib.error.URLError,TimeoutError,OSError,ValueError) as error:
            status=getattr(error,'code',None)
            self.event('api_error',method=method,path=path,status=status)
            if method=='POST' and path=='/orders':
                self.uncertain=True
                raise Uncertain('Order outcome uncertain/rejected. No automatic order retry; inspect RIT orders and inventory.') from None
            raise Halt(f'API failure on {method} {path}, HTTP {status}; stopping.') from None
        finally:
            self.next=time.monotonic()+self.gap

    def get(self,path,**params): return self.call('GET',path,**params)

@dataclass(frozen=True)
class Config:
    size:int=100
    stock_cap:int=500
    gross_cap:int=2000
    close_limit:int=0  # 0 = separate CRO limit unknown; never infer it from net.
    limit_buffer:int=100
    loss_limit:float=200.
    reduce_before:int=20
    pause_after:int=4
    final_buffer:int=30
    min_rest:float=2.
    reprice_ticks:int=2
    jump_threshold:float=.15
    cooldown:float=3.
    imbalance_threshold:float=.65
    variance_tau:float=5.
    risk_horizon:float=2.
    volatility_weight:float=.5
    taker_fee_reserve:float=.1  # heuristic fraction, not estimated probability
    min_edge_ticks:float=.25
    max_spread_ticks:int=20
    max_book_age:float=2.
    cleanup_seconds:float=10.


def number(x):
    v=float(x)
    if not math.isfinite(v):raise Halt('Nonfinite API value')
    return v


def positions(rows):
    result={}
    for row in rows:
        q=number(row['position'])
        if q!=int(q):raise Halt('Fractional positions unsupported')
        result[row['ticker']]=int(q)
    if set(result)!=set(TICKERS):raise Halt('Expected exactly four case securities')
    return result


def remaining(o):
    value=number(o['quantity'])-number(o.get('quantity_filled',0))
    if value<0 or value!=int(value):raise Halt('Invalid order quantity')
    return int(value)


def capacities(rows,c):
    group=[r for r in rows if r.get('name')=='LIMIT-STOCK']
    if len(group)!=1:raise Halt('Expected one LIMIT-STOCK group')
    gross=min(c.gross_cap,int(number(group[0]['gross_limit']))-c.limit_buffer)
    net=min(c.gross_cap,int(number(group[0]['net_limit']))-c.limit_buffer)
    if c.close_limit:gross=min(gross,c.close_limit-c.limit_buffer)
    if min(gross,net)<=0:raise Halt('No safe capacity under current limits')
    return gross,net


def envelope(p,orders):
    buy=dict.fromkeys(TICKERS,0);sell=buy.copy()
    for o in orders:
        if o['ticker'] not in p or o['action'] not in ('BUY','SELL'):raise Halt('Unexpected order')
        (buy if o['action']=='BUY' else sell)[o['ticker']]+=remaining(o)
    lo={k:p[k]-sell[k]for k in TICKERS};hi={k:p[k]+buy[k]for k in TICKERS}
    return lo,hi


def safe(p,orders,gross,net,cap):
    lo,hi=envelope(p,orders)
    return (sum(max(abs(lo[k]),abs(hi[k]))for k in TICKERS)<=gross
            and sum(lo.values())>=-net and sum(hi.values())<=net
            and all(max(abs(lo[k]),abs(hi[k]))<=cap for k in TICKERS))


def safe_reduction(p,orders,candidate,gross,net,cap):
    """Reduction must not worsen an existing envelope breach on any metric."""
    before=envelope(p,orders);after=envelope(p,orders+[candidate])
    def metrics(bounds):
        lo,hi=bounds
        return [sum(max(abs(lo[k]),abs(hi[k]))for k in TICKERS),
                max(abs(sum(lo.values())),abs(sum(hi.values()))),
                *[max(abs(lo[k]),abs(hi[k]))for k in TICKERS]]
    return all(a<=max(limit,b)for a,b,limit in zip(metrics(after),metrics(before),[gross,net,*[cap]*4]))


def depth(raw,own_ids=()):
    """Aggregate remaining quantities; exclude our orders from the signal."""
    sides=[]
    for side in ('bids','asks'):
        levels={}
        for o in raw.get(side,[]):
            if o['order_id'] in own_ids:continue
            qty=remaining(o);price=number(o['price'])
            if qty and price>0:levels[price]=levels.get(price,0)+qty
        sides.append(levels)
    if not all(sides):return None
    bid,ask=max(sides[0]),min(sides[1])
    if not bid<ask:return None
    qb,qa=sides[0][bid],sides[1][ask]
    b3=sum(sides[0][x]for x in sorted(sides[0],reverse=True)[:3])
    a3=sum(sides[1][x]for x in sorted(sides[1])[:3])
    return bid,ask,(qb-qa)/(qb+qa),(b3-a3)/(b3+a3)


@dataclass
class Observation:
    time:float
    mid:float
    variance:float=0.
    paused_until:float=0.


def update_observation(previous,now,mid,c):
    if previous is None:return Observation(now,mid)
    dt=max(.001,now-previous.time);change=mid-previous.mid
    v=previous.variance+-math.expm1(-dt/c.variance_tau)*(change**2/dt-previous.variance)
    paused=previous.paused_until
    if abs(change)>=c.jump_threshold:
        paused=now+c.cooldown
        # A news jump is not used as an estimate of ordinary quoting volatility.
        v=previous.variance
    return Observation(now,mid,max(0,v),paused)


def targets(k,sec,p,book,obs,mode,c,now):
    """Return side -> (price, quantity, estimated CAD/share edge).

    Edge is a heuristic score, NOT a fill-conditioned forecast or guaranteed P&L.
    Reducing orders bypass the profitability/pressure filter to control inventory.
    """
    if book is None:return {}
    bid,ask,imb,imb3=book;q=p[k];tick=10**(-int(sec['quoted_decimals']))
    max_qty=int(number(sec['max_trade_size']));min_qty=max(1,int(number(sec.get('min_trade_size',0))))
    if mode=='reduce':
        qty=min(abs(q),max_qty)
        if qty<min_qty:return {}
        return {('SELL'if q>0 else 'BUY'):(ask if q>0 else bid,qty,None)}
    if now<obs.paused_until or ask-bid>c.max_spread_ticks*tick:return {}
    rebate=number(sec['limit_order_rebate']);fee=number(sec['trading_fee'])
    # Rebates are positive cash inflows; negative rebates are maker charges.
    adverse_buffer=(.5*tick+c.volatility_weight*math.sqrt(obs.variance*c.risk_horizon)
                    +c.taker_fee_reserve*max(0.,fee))
    alpha=tick*(.75*imb+.25*imb3)
    fair=(bid+ask)/2+alpha-2*tick*q/c.stock_cap
    half=max(tick/2,adverse_buffer+c.min_edge_ticks*tick-rebate)
    buy=min(bid,math.floor((fair-half)/tick+1e-8)*tick)
    sell=max(ask,math.ceil((fair+half)/tick-1e-8)*tick)
    result={}
    for side,price in (('BUY',buy),('SELL',sell)):
        reducing=(side=='SELL'and q>0)or(side=='BUY'and q<0)
        if reducing:
            # Prioritise closing; do not move the closing quote farther from best.
            price=ask if side=='SELL'else bid
        elif (side=='BUY'and imb < -c.imbalance_threshold)or(side=='SELL'and imb>c.imbalance_threshold):continue
        # Scale only the inventory-increasing side; never enlarge size for rebates.
        if reducing:qty=min(c.size,abs(q),max_qty)
        else:
            room=c.stock_cap-abs(q)
            qty=min(max_qty,c.size,max(0,room))
            if q and room<c.stock_cap/2:qty=min(qty,max(min_qty,c.size//2))
        if qty<min_qty or price<=0:continue
        edge=(fair-price if side=='BUY'else price-fair)+rebate-adverse_buffer
        if not reducing and edge<c.min_edge_ticks*tick-1e-8:continue
        result[side]=(round(price,8),int(qty),edge)
    return result


class Bot:
    def __init__(self,api,c):
        self.api=api;self.c=c;self.known={};self.obs={};self.started=False
        self.initial_pnl=0.;self.stop_loss=False;self.seen_active=False
        self.last_case=None;self.rotation=0;self.shutdown=False

    def case(self):
        x=self.api.get('/case')
        if self.last_case and (x['period']!=self.last_case['period']or int(x['tick'])<int(self.last_case['tick'])):
            raise Halt('Case reset: restart separately for another heat')
        self.last_case=x
        return x

    def mode(self,case):
        tick=int(case['tick']);end=int(case['ticks_per_period'])
        return ('reduce' if self.shutdown or self.stop_loss or tick>=end-self.c.final_buffer
                or tick%60>=60-self.c.reduce_before or tick%60<self.c.pause_after else 'quote')

    def snapshot(self):
        # Orders first: fills between these reads are conservatively double-counted.
        orders=self.api.get('/orders',status='OPEN')
        if not isinstance(orders,list):raise Halt('Invalid open orders response')
        for o in orders:
            if int(o['order_id'])not in self.known:raise Halt('External/unknown open order: dedicated account required')
        rows=self.api.get('/securities');p=positions(rows)
        g,n=capacities(self.api.get('/limits'),self.c)
        self.api.event('snapshot',securities=rows,open_orders=orders,gross_cap=g,net_cap=n)
        # Completed/cancelled orders: retain cumulative fills and VWAP in the log.
        present={int(o['order_id'])for o in orders}
        for oid in list(self.known):
            if oid not in present:
                status=self.api.get('/orders/'+str(oid))
                if status.get('status')=='OPEN':raise Halt('Inconsistent order snapshot; halt before replacing')
                self.api.event('order_final',order=status);del self.known[oid]
        return rows,p,orders,g,n

    def cancel_ids(self,ids):
        ids=set(ids)
        if not ids:return
        answer=self.api.call('POST','/commands/cancel',ids=','.join(str(x)for x in sorted(ids)))
        if isinstance(answer,dict)and answer.get('success')is False:raise Halt('Cancellation rejected')
        for _ in range(4):
            orders=self.api.get('/orders',status='OPEN')
            if not ids.intersection(int(o['order_id'])for o in orders):return
            time.sleep(.15)
        raise Halt('Cancellation not confirmed; no replacement sent')

    def cancel_all(self):
        # Account-wide cancellation reserved for shutdown and exceptional risk.
        self.api.call('POST','/commands/cancel',all=1)
        for _ in range(4):
            if not self.api.get('/orders',status='OPEN'):return
            time.sleep(.15)
        raise Halt('Could not confirm account-wide cancellation')

    def submit(self,k,side,price,qty):
        if side not in ('BUY','SELL')or qty<=0 or int(qty)!=qty or number(price)<=0:raise Halt('Invalid LIMIT')
        reply=self.api.call('POST','/orders',ticker=k,type='LIMIT',quantity=qty,action=side,price=price)
        if not isinstance(reply,dict)or 'order_id'not in reply:
            self.api.uncertain=True;raise Uncertain('Missing acknowledgement; no retry')
        oid=int(reply['order_id'])
        self.known[oid]=dict(time=time.monotonic(),ticker=k,action=side)
        # Reserve full size against the pre-submission position, even immediate fills.
        return dict(order_id=oid,ticker=k,action=side,price=price,quantity=qty,quantity_filled=0)

    def get_book(self,k):
        raw=self.api.get('/securities/book',ticker=k,limit=100)
        now=time.monotonic();book=depth(raw,self.known)
        self.api.event('book',ticker=k,data=raw)
        if book:
            self.obs[k]=update_observation(self.obs.get(k),now,(book[0]+book[1])/2,self.c)
        return book,now

    def startup(self):
        x=self.case()
        if 'market making'not in str(x.get('name','')).lower()or int(x['total_periods'])!=1 or int(x['ticks_per_period'])!=300:
            raise Halt('Expected the 300-second, single-period Market Making case')
        if self.api.get('/orders',status='OPEN'):raise Halt('Start with no open orders')
        rows=self.api.get('/securities');p=positions(rows)
        if any(p.values()):raise Halt('Start with zero positions')
        for sec in rows:
            groups=sec.get('limits',[])
            if len(groups)!=1 or groups[0].get('name')!='LIMIT-STOCK'or number(groups[0].get('units',0))!=1:
                raise Halt('Unsupported limit mapping')
            if number(sec.get('execution_delay_ms',0))!=0:raise Halt('Configured execution delay must be zero')
            if int(sec.get('api_orders_per_second',0))<8:raise Halt('Order rate must support at least 8/sec')
            if not int(sec.get('min_trade_size',0))<=self.c.size<=int(sec['max_trade_size']):raise Halt('Invalid quote size')
            number(sec['limit_order_rebate']);number(sec['trading_fee'])
            print(sec['ticker'],'maker rebate',sec['limit_order_rebate'],'taker fee',sec['trading_fee'])
        capacities(self.api.get('/limits'),self.c)
        self.initial_pnl=sum(number(s.get('realized',0))+number(s.get('unrealized',0))for s in rows)
        self.started=True;self.api.event('start',config=self.c.__dict__)

    def maintain(self,k,rows,p,orders,g,n,case,forced=False):
        sec=next(s for s in rows if s['ticker']==k)
        book,observed=self.get_book(k);mode='reduce'if forced else self.mode(case)
        desired=targets(k,sec,p,book,self.obs.get(k),mode,self.c,time.monotonic())if book else {}
        existing=[o for o in orders if o['ticker']==k]
        # Keep queue position unless repricing is material or the order is unsafe.
        cancels=[];seen=set();tick=10**(-int(sec['quoted_decimals']))
        for o in existing:
            oid=int(o['order_id']);side=o['action'];d=desired.get(side)
            if side in seen:cancels.append(oid);continue
            seen.add(side)
            age=time.monotonic()-self.known[oid]['time']
            if d is None:cancels.append(oid);continue
            price,qty,_=d
            crossing=book and (o['price']>=book[1]if side=='BUY'else o['price']<=book[0])
            wrong_reduction=mode=='reduce'and ((side=='BUY'and p[k]>=0)or(side=='SELL'and p[k]<=0))
            too_large=remaining(o)>qty
            material=abs(number(o['price'])-price)>=self.c.reprice_ticks*tick-1e-8
            reducing=(side=='SELL'and p[k]>0)or(side=='BUY'and p[k]<0)
            existing_edge=(d[2]+(price-number(o['price'])if side=='BUY'else number(o['price'])-price))if d[2]is not None else None
            uneconomic=not reducing and existing_edge is not None and existing_edge<self.c.min_edge_ticks*tick-1e-8
            if crossing or wrong_reduction or too_large or uneconomic or (material and age>=self.c.min_rest):cancels.append(oid)
        if cancels:
            self.cancel_ids(cancels)
            rows,p,orders,g,n=self.snapshot()
            sec=next(s for s in rows if s['ticker']==k)
            book,observed=self.get_book(k)
            case=self.case();mode='reduce'if forced else self.mode(case)
            desired=targets(k,sec,p,book,self.obs.get(k),mode,self.c,time.monotonic())if book else {}
        # Prioritise the side reducing actual inventory.
        sides=('SELL','BUY')if p[k]>0 else ('BUY','SELL')
        for side in sides:
            if STOP and not self.shutdown:return rows,p,orders,g,n
            if any(o['ticker']==k and o['action']==side for o in orders):continue
            d=desired.get(side)
            if d is None:continue
            fresh=self.case()
            if fresh.get('status')!='ACTIVE':break
            current_mode='reduce'if forced else self.mode(fresh)
            if current_mode!=mode:break  # next cycle cancels/adjusts immediately
            if time.monotonic()-observed>self.c.max_book_age:break
            price,qty,edge=d
            candidate=dict(ticker=k,action=side,price=price,quantity=qty,quantity_filled=0)
            if current_mode=='reduce':
                # Only one outstanding reducing order; never exceed current inventory.
                if (side=='BUY'and p[k]>=0)or(side=='SELL'and p[k]<=0)or qty>abs(p[k]):continue
                minimum=max(1,int(sec.get('min_trade_size',0)))
                while qty>=minimum and not safe_reduction(p,orders,dict(candidate,quantity=qty),g,n,self.c.stock_cap):qty-=1
                if qty<minimum:continue
            else:
                while qty>=max(1,int(sec.get('min_trade_size',0)))and not safe(p,orders+[dict(candidate,quantity=qty)],g,n,self.c.stock_cap):
                    qty-=1
                if qty<max(1,int(sec.get('min_trade_size',0))):continue
            self.api.event('decision',ticker=k,side=side,price=price,quantity=qty,edge_score=edge,mode=current_mode)
            orders.append(self.submit(k,side,price,qty))
        return rows,p,orders,g,n

    def cycle(self):
        case=self.case()
        if case.get('status')!='ACTIVE':
            if self.seen_active:return False
            time.sleep(.3);return True
        self.seen_active=True
        if int(case['tick'])>=int(case['ticks_per_period']):return False
        rows,p,orders,g,n=self.snapshot()
        pnl=sum(number(s.get('realized',0))+number(s.get('unrealized',0))for s in rows)-self.initial_pnl
        if pnl<=-self.c.loss_limit:self.stop_loss=True
        forced=not safe(p,orders,g,n,self.c.stock_cap)
        if forced:
            self.cancel_all();rows,p,orders,g,n=self.snapshot()
        offset=self.rotation%4;self.rotation+=1
        keys=list(TICKERS[offset:]+TICKERS[:offset])
        # Inventory first, then rotation: all four retain equal access when flat.
        keys.sort(key=lambda k:abs(p[k]),reverse=True)
        for k in keys:
            if STOP and not self.shutdown:break
            # Reconcile fills and limits once per stock before managing its two sides.
            rows,p,orders,g,n=self.snapshot()
            if not safe(p,orders,g,n,self.c.stock_cap):
                self.cancel_all();rows,p,orders,g,n=self.snapshot();forced=True
            fresh=self.case()
            if fresh.get('status')!='ACTIVE':return False
            if int(fresh['tick'])>=int(fresh['ticks_per_period']):return False
            rows,p,orders,g,n=self.maintain(k,rows,p,orders,g,n,fresh,forced)
        print(f"tick={case['tick']} mode={self.mode(case)} pnl={pnl:+.2f} positions={p}")
        return True

    def run(self,seconds):
        self.startup();deadline=time.monotonic()+seconds
        while not STOP and time.monotonic()<deadline:
            if not self.cycle():break
            time.sleep(.1)

    def cleanup(self):
        if not self.started:return
        self.shutdown=True
        try:
            self.cancel_all()
            if self.api.uncertain:
                print('Uncertain order: cancellation attempted; inspect positions. No further submissions.');return
            deadline=time.monotonic()+self.c.cleanup_seconds
            while time.monotonic()<deadline:
                case=self.case()
                if case.get('status')!='ACTIVE'or int(case['tick'])>=int(case['ticks_per_period']):break
                rows,p,orders,g,n=self.snapshot()
                if not any(p.values()):break
                for k in sorted(TICKERS,key=lambda k:abs(p[k]),reverse=True):
                    if time.monotonic()>=deadline:break
                    rows,p,orders,g,n=self.snapshot()
                    self.maintain(k,rows,p,orders,g,n,case,forced=True)
                time.sleep(.2)
        except (Halt,KeyError,ValueError,TypeError)as e:print('Close-out attempt interrupted:',e)
        finally:
            try:
                self.cancel_all()
                rows=self.api.get('/securities');p=positions(rows)
                self.api.event('shutdown',positions=p)
                print('Remaining positions:',p)
                if any(p.values()):print('Passive close-out incomplete. Inspect RIT inventory.')
            except (Halt,KeyError,ValueError,TypeError):print('Cleanup NOT confirmed. Inspect RIT orders and positions.')


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--live',action='store_true')
    parser.add_argument('--base-url',default='http://localhost:9999/v1')
    parser.add_argument('--size',type=int,default=100)
    parser.add_argument('--stock-cap',type=int,default=500)
    parser.add_argument('--gross-cap',type=int,default=2000)
    parser.add_argument('--close-limit',type=int,default=0,help='Known separate CRO gross limit; 0 means unknown')
    parser.add_argument('--loss-limit',type=float,default=200)
    parser.add_argument('--seconds',type=float,default=420)
    a=parser.parse_args()
    if not a.live:parser.error('Use --live to enable orders in the RIT simulation')
    if not 1<=a.size<=a.stock_cap<=500 or not a.stock_cap<=a.gross_cap<=2000:parser.error('Require size <= stock-cap <= 500 and stock-cap <= gross-cap <= 2000')
    if a.close_limit<0 or (a.close_limit and a.close_limit<=100):parser.error('close-limit must be 0 or greater than 100')
    if not all(math.isfinite(x)and x>0 for x in (a.seconds,a.loss_limit)):parser.error('Invalid duration/loss threshold')
    key=os.environ.get('RIT_API_KEY')or getpass.getpass('RIT API key (hidden): ')
    if not key.strip():parser.error('API key required')
    signal.signal(signal.SIGINT,stop_signal);signal.signal(signal.SIGTERM,stop_signal)
    Path('live_logs').mkdir(exist_ok=True)
    path=Path('live_logs')/('final_'+dt.datetime.now().strftime('%Y%m%d_%H%M%S')+'_'+uuid.uuid4().hex[:6]+'.jsonl')
    with path.open('x',encoding='utf-8')as log:
        api=API(a.base_url,key,log);bot=Bot(api,Config(size=a.size,stock_cap=a.stock_cap,gross_cap=a.gross_cap,close_limit=a.close_limit,loss_limit=a.loss_limit))
        print('Log:',path)
        try:bot.run(a.seconds)
        except (Halt,KeyError,ValueError,TypeError)as e:print('Stopped:',e)
        finally:bot.cleanup()


# BEGIN RITC_API_RECOVERY_V1
class OrderRejected(Halt):
    def __init__(self, status, message, delay=1.):
        self.status, self.message, self.delay = status, message, delay
        super().__init__(f"HTTP {status}: {message}")

class ReadUnavailable(Halt):
    pass

def _safe_api_error(api, error):
    try:
        raw = error.read(4096).decode('utf-8', errors='replace')
        raw = raw.replace(api.key, '[REDACTED]') if api.key else raw
        payload = json.loads(raw)
        if isinstance(payload, dict):
            message = str(payload.get('message', payload.get('error', payload)))
            delay = payload.get('wait', payload.get('retry_after', 1.))
        else:
            message, delay = str(payload), 1.
    except (ValueError, TypeError, OSError):
        message, delay = 'API returned an HTTP error (no readable JSON message)', 1.
    try:
        header_delay = float(error.headers.get('Retry-After', 0))
    except (ValueError, TypeError, AttributeError):
        header_delay = 0.
    try:
        delay = float(delay)
    except (ValueError, TypeError):
        delay = 1.
    if not math.isfinite(delay): delay = 1.
    if not math.isfinite(header_delay): header_delay = 0.
    return message[:600], max(.5, min(30., max(delay, header_delay)))


def _api_call_recovered(self, method, path, **params):
    if method == 'POST' and path == '/orders' and params.get('type') != 'LIMIT':
        raise Halt('Only LIMIT orders are permitted')
    time.sleep(max(0., self.next-time.monotonic()))
    query = urllib.parse.urlencode(params)
    request = urllib.request.Request(self.base+path+('?' + query if query else ''),
                                    headers={'X-API-key': self.key}, method=method)
    started = time.monotonic()
    self.event('request', method=method, path=path, params=params, monotonic=started)
    try:
        with self.opener.open(request, timeout=2.) as response:
            data = json.loads(response.read())
        self.event('response', method=method, path=path, data=data,
                   latency_ms=(time.monotonic()-started)*1000)
        return data
    except urllib.error.HTTPError as error:
        status = error.code
        message, delay = _safe_api_error(self, error)
        self.event('api_error', method=method, path=path, status=status, message=message)
        print(f'API {status} {method} {path}: {message}')
        if status in (401, 403):
            raise Halt('Authentication/access denied; check API key and permissions') from None
        if status in (400, 404, 405, 409, 422, 429) and method == 'POST' and path == '/orders':
            # These are explicit rejections, not evidence of an accepted order.
            self.next = max(self.next, time.monotonic()+delay)
            raise OrderRejected(status, message, delay) from None
        if status == 429:
            self.next = max(self.next, time.monotonic()+delay)
            raise ReadUnavailable('Rate limit: waiting before reconciliation') from None
        if status >= 500 and method == 'GET':
            raise ReadUnavailable(f'Temporary HTTP {status} on {path}') from None
        if method == 'POST' and path == '/orders':
            self.uncertain = True
            raise Uncertain('Order outcome uncertain; reconcile history before resuming') from None
        raise Halt(f'API failure {status} on {method} {path}: {message}') from None
    except (urllib.error.URLError, TimeoutError, OSError, ValueError):
        self.event('api_error', method=method, path=path, status=None,
                   message='Connection timeout/failure or invalid JSON')
        if method == 'POST' and path == '/orders':
            self.uncertain = True
            raise Uncertain('Order response lost; do not resend before reconciliation') from None
        if method == 'GET':
            raise ReadUnavailable(f'Temporary read failure on {path}') from None
        raise Halt('Cancellation outcome unclear; inspect open orders') from None
    finally:
        self.next = max(self.next, time.monotonic()+self.gap)


_original_submit = Bot.submit

def _submit_recovered(self, k, side, price, qty):
    # Record the pre-request order IDs. A new matching ID proves acceptance,
    # even if the order was filled before the missing acknowledgement arrived.
    history = self.api.get('/orders')
    if not isinstance(history, list): raise Halt('Invalid order-history response')
    before = {int(o['order_id']) for o in history}
    self.api.pending = dict(ticker=k, action=side, price=price, quantity=qty,
                            before=before, time=time.monotonic(),
                            period=(self.last_case or {}).get('period'),
                            tick=(self.last_case or {}).get('tick', 0))
    try:
        result = _original_submit(self, k, side, price, qty)
    except OrderRejected:
        self.api.pending = None
        raise
    else:
        self.api.pending = None
        return result


def _reconcile_pending(self):
    pending = getattr(self.api, 'pending', None)
    if not pending: raise Halt('Uncertain order without reconciliation metadata; inspect RIT')
    history = self.api.get('/orders')
    if not isinstance(history, list): raise ReadUnavailable('Order history unavailable')
    matches = []
    for o in history:
        if (int(o['order_id']) not in pending['before']
            and o.get('ticker') == pending['ticker']
            and o.get('action') == pending['action']
            and o.get('type') == 'LIMIT'
            and o.get('period') == pending['period']
            and number(o.get('tick', -1)) >= number(pending['tick'])
            and abs(number(o['quantity'])-pending['quantity']) < 1e-8
            and abs(number(o['price'])-pending['price']) < 1e-8):
            matches.append(o)
    if len(matches) == 1:
        o = matches[0]; oid = int(o['order_id'])
        self.known[oid] = dict(time=pending['time'], ticker=pending['ticker'], action=pending['action'])
        self.api.event('order_recovered', order=o)
        self.api.pending = None; self.api.uncertain = False
        print(f'Recovered accepted order {oid}; resuming after fresh reconciliation.')
        return True
    # Absence from a polled/truncated history does NOT prove rejection.
    self.api.event('reconciliation_wait', matching_orders=len(matches))
    print('Order acceptance still unconfirmed: monitoring history, no new orders sent.')
    return False


def _run_recovered(self, seconds):
    self.startup()
    deadline = time.monotonic()+seconds
    self.api.pending = None
    rejected = 0
    while not STOP and time.monotonic() < deadline:
        try:
            if self.api.uncertain:
                case = self.case()
                if self.seen_active and case.get('status') != 'ACTIVE': break
                if int(case['tick']) >= int(case['ticks_per_period'])-1: break
                if not _reconcile_pending(self):
                    time.sleep(1.); continue
            if not self.cycle(): break
            rejected = 0
            time.sleep(.1)
        except OrderRejected as error:
            rejected += 1
            self.api.event('order_rejected_continue', status=error.status,
                           message=error.message, consecutive=rejected)
            print('Order rejected; refreshing limits, positions and prices. Bot remains active.')
            # Rebuild the decision on the next cycle; never replay the old POST.
            time.sleep(min(10., max(error.delay, .5*min(rejected, 10))))
        except ReadUnavailable as error:
            print(str(error), 'Monitoring continues; no blind order retry.')
            time.sleep(1.)
        except Uncertain:
            print('Lost/ambiguous acknowledgement; checking history, not duplicating the order.')
            time.sleep(.5)

API.call = _api_call_recovered
Bot.submit = _submit_recovered
Bot.run = _run_recovered
# END RITC_API_RECOVERY_V1

# BEGIN RITC_CONTINUOUS_V1
_previous_startup = Bot.startup

def _continuous_startup(self):
    from dataclasses import replace
    self.c = replace(self.c, cooldown=0., reduce_before=0, pause_after=0, final_buffer=0)
    _previous_startup(self)
    print('CONTINUOUS: four stocks, LIMIT only, no scheduled minute/final/shock pauses.')
    print('Runs across heats until Ctrl+C. Position limits and loss protection remain active.')


def _continuous_mode(self, case):
    return 'reduce' if self.shutdown or self.stop_loss else 'quote'


def _continuous_case(self):
    case = self.api.get('/case')
    old = self.last_case
    reset = old and (case['period'] != old['period'] or int(case['tick']) < int(old['tick']))
    if reset:
        if ('market making' not in str(case.get('name','')).lower()
            or int(case['total_periods']) != 1 or int(case['ticks_per_period']) != 300):
            raise Halt('Unexpected case after reset; refusing automatic restart')
        self.cancel_all()
        rows = self.api.get('/securities')
        current = positions(rows)
        if any(current.values()):
            raise Halt('New heat has nonzero inventory; inspect account before restart')
        self.known.clear(); self.obs.clear(); self.stop_loss = False; self.seen_active = False
        self.api.pending = None; self.api.uncertain = False
        self.initial_pnl = sum(number(s.get('realized',0))+number(s.get('unrealized',0)) for s in rows)
        self.api.event('new_heat', case=case, positions=current)
        print('New heat detected: zero inventory confirmed, trading automatically resumes when ACTIVE.')
    self.last_case = case
    return case


def _run_continuous(self, seconds=None):
    # Deliberately no wall-clock stop: the user's request is continuous operation.
    self.startup(); self.api.pending = None
    rejected = 0; waiting = False
    while not STOP:
        try:
            case = self.case()
            if case.get('status') != 'ACTIVE' or int(case['tick']) >= int(case['ticks_per_period']):
                if not waiting:
                    self.cancel_all()
                    print('Market stopped: waiting for the next active heat; program remains running.')
                    waiting = True
                time.sleep(.5); continue
            waiting = False
            if self.api.uncertain and not _reconcile_pending(self):
                time.sleep(1.); continue
            self.cycle()
            rejected = 0
            time.sleep(.1)
        except OrderRejected as error:
            rejected += 1
            self.api.event('order_rejected_continue', status=error.status, message=error.message)
            print('Rejected order; refreshing the market. Program remains running.')
            time.sleep(min(10., max(error.delay, .5*min(rejected,10))))
        except ReadUnavailable as error:
            print(str(error), 'Reconnecting without resubmitting orders.')
            time.sleep(1.)
        except Uncertain:
            print('Checking order acceptance before new submissions; no duplicate POST.')
            time.sleep(.5)

Bot.startup = _continuous_startup
Bot.mode = _continuous_mode
Bot.case = _continuous_case
Bot.run = _run_continuous
# END RITC_CONTINUOUS_V1

if __name__=='__main__':main()
