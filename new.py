"""Standalone SIMPLE-4: continuous four-stock LIMIT market maker. Python 3.10+, standard library only.
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
    def __init__(self,base,key,log,gap=.15):
        p=urllib.parse.urlsplit(base)
        if p.scheme not in ('http','https') or not p.hostname or p.username or p.password or p.query or p.fragment:
            raise Halt('Invalid REST base URL')
        self.base=base.rstrip('/'); self.key=key; self.log=log; self.gap=gap; self.next=0.; self.uncertain=False
        self.opener=urllib.request.build_opener(urllib.request.ProxyHandler({}),NoRedirect())

    def event(self,kind,**fields):
        self.log.write(json.dumps(dict(kind=kind,utc=dt.datetime.now(dt.timezone.utc).isoformat(),**fields),allow_nan=False)+'\n')
        self.log.flush()

    def call(self,method,path,**params):
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
                                    headers={'X-API-key': self.key}, method=method,
                                    data=b'' if method == 'POST' else None)
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



API.call = _api_call_recovered

SIZE=100
STOCK_CAP=500
GROSS_CAP=2000
LOSS_LIMIT=200.


def num(x):
    v=float(x)
    if not math.isfinite(v):raise Halt('Invalid numeric API value')
    return v


def rem(o):return max(0,int(num(o['quantity'])-num(o.get('quantity_filled',0))))


def safe(p,orders,gross,net):
    lo=p.copy();hi=p.copy()
    for o in orders:
        k=o['ticker']
        if k not in p:raise Halt('Unexpected order ticker')
        if o['action']=='BUY':hi[k]+=rem(o)
        elif o['action']=='SELL':lo[k]-=rem(o)
        else:raise Halt('Unexpected order side')
    return (sum(max(abs(lo[k]),abs(hi[k]))for k in p)<=gross
            and sum(lo.values())>=-net and sum(hi.values())<=net
            and all(max(abs(lo[k]),abs(hi[k]))<=STOCK_CAP for k in p))


def reduction_safe(p,orders,candidate,gross,net):
    def metrics(seq):
        lo=p.copy();hi=p.copy()
        for o in seq:
            if o['action']=='BUY':hi[o['ticker']]+=rem(o)
            else:lo[o['ticker']]-=rem(o)
        return [sum(max(abs(lo[k]),abs(hi[k]))for k in p),max(abs(sum(lo.values())),abs(sum(hi.values()))),*[max(abs(lo[k]),abs(hi[k]))for k in p]]
    return all(a<=max(b,limit)for a,b,limit in zip(metrics(orders+[candidate]),metrics(orders),[gross,net,*[STOCK_CAP]*4]))


def best_book(raw,own_ids):
    levels=[]
    for side in ('bids','asks'):
        prices=[num(o['price'])for o in raw.get(side,[])if rem(o)>0 and int(o['order_id'])not in own_ids]
        if not prices:return None
        levels.append(max(prices)if side=='bids'else min(prices))
    return tuple(levels)if 0<levels[0]<levels[1]else None


class Bot:
    def __init__(self,api,size=100):
        self.api=api;self.size=size;self.known={};self.pending=None;self.last=None
        self.started=False;self.loss=False;self.initial=0.;self.messages={};self.rotation=0

    def say(self,key,message):
        if self.messages.get(key)!=message:print(message);self.messages[key]=message

    def cancel(self,ids=None):
        if ids is not None and not ids:return
        params={'all':1}if ids is None else {'ids':','.join(map(str,sorted(ids)))}
        self.api.call('POST','/commands/cancel',**params)
        for _ in range(4):
            opened=self.api.get('/orders',status='OPEN')
            wanted={int(o['order_id'])for o in opened}if ids is None else set(ids).intersection(int(o['order_id'])for o in opened)
            if not wanted:return
            time.sleep(.2)
        raise Halt('Cancellation not confirmed; inspect RIT')

    def securities(self):
        rows=self.api.get('/securities')
        if not isinstance(rows,list):raise Halt('Invalid securities response')
        p={s['ticker']:int(num(s['position']))for s in rows}
        if set(p)!=set(TICKERS):raise Halt('Expected exactly SPNG SMMR ATMN WNTR')
        if any(num(s['position'])!=p[s['ticker']]for s in rows):raise Halt('Fractional position unsupported')
        return {s['ticker']:s for s in rows},p

    def snapshot(self):
        # Orders before positions: fills between reads are double-counted conservatively.
        opened=self.api.get('/orders',status='OPEN')
        if any(int(o['order_id'])not in self.known for o in opened):raise Halt('Unknown open order: do not run another bot/manual orders')
        sec,p=self.securities()
        groups=self.api.get('/limits')
        group=[x for x in groups if x.get('name')=='LIMIT-STOCK']
        if len(group)!=1:raise Halt('Expected LIMIT-STOCK')
        g=min(GROSS_CAP,int(num(group[0]['gross_limit']))-100)
        n=min(GROSS_CAP,int(num(group[0]['net_limit']))-100)
        if min(g,n)<=0:raise Halt('No capacity under current limits')
        for oid in list(self.known):
            if not any(int(o['order_id'])==oid for o in opened):
                final=self.api.get('/orders/'+str(oid))
                if final.get('status')=='OPEN':raise ReadUnavailable('Order snapshot inconsistent; waiting')
                self.api.event('order_final',data=final);del self.known[oid]
        pnl=sum(num(s.get('realized',0))+num(s.get('unrealized',0))for s in sec.values())-self.initial
        if pnl<=-LOSS_LIMIT:self.loss=True
        self.api.event('account',positions=p,pnl=pnl,gross_cap=g,net_cap=n,reduce_only=self.loss)
        return sec,p,opened,g,n

    def case(self):
        c=self.api.get('/case')
        if self.last and (c['period']!=self.last['period']or int(c['tick'])<int(self.last['tick'])):
            self.cancel();sec,p=self.securities()
            if any(p.values()):raise Halt('Reset with inventory: inspect RIT')
            self.known.clear();self.pending=None;self.api.uncertain=False;self.loss=False;self.messages.clear()
            self.initial=sum(num(s.get('realized',0))+num(s.get('unrealized',0))for s in sec.values())
            print('NEW HEAT: positions zero, automatically resuming when ACTIVE')
        self.last=c;return c

    def startup(self):
        c=self.case()
        if int(c['total_periods'])!=1 or int(c['ticks_per_period'])!=300:raise Halt('Expected single 300-second case')
        if self.api.get('/orders',status='OPEN'):raise Halt('Cancel existing orders before startup')
        sec,p=self.securities()
        if any(p.values()):raise Halt('Start with zero positions')
        for k,s in sec.items():
            mapping=s.get('limits',[])
            if len(mapping)!=1 or mapping[0].get('name')!='LIMIT-STOCK'or num(mapping[0].get('units',0))!=1:raise Halt('Unsupported limit mapping')
            if num(s.get('execution_delay_ms',0))!=0:raise Halt('Nonzero configured execution delay unsupported')
            print(k,'rebate=',s['limit_order_rebate'],'fee=',s['trading_fee'],'min_qty=',s.get('min_trade_size',0),'max_qty=',s['max_trade_size'])
        self.initial=sum(num(s.get('realized',0))+num(s.get('unrealized',0))for s in sec.values())
        self.started=True
        print('SIMPLE-4 LIVE: best bid/ask, LIMIT only, no signal or minute pauses. Ctrl+C to stop.')

    def send(self,k,side,price,qty):
        history=self.api.get('/orders')
        self.pending=dict(before={int(o['order_id'])for o in history},ticker=k,action=side,quantity=qty,price=price,period=self.last['period'],tick=self.last['tick'])
        try:
            response=self.api.call('POST','/orders',ticker=k,type='LIMIT',action=side,quantity=int(qty),price=price)
        except OrderRejected:
            self.pending=None;raise
        if not isinstance(response,dict)or 'order_id'not in response:
            self.api.event('invalid_order_ack',data=response);self.api.uncertain=True
            raise Uncertain('Order acknowledgement missing id')
        oid=int(response['order_id']);self.known[oid]=time.monotonic();self.pending=None
        print(f'ACCEPTED {k} {side} {qty} @ {price:.2f} id={oid} status={response.get("status")}')
        return dict(order_id=oid,ticker=k,action=side,quantity=qty,quantity_filled=0,price=price)

    def reconcile(self):
        if not self.pending:raise Halt('Uncertain order without metadata')
        p=self.pending;matches=[]
        for o in self.api.get('/orders'):
            if (int(o['order_id'])not in p['before']and o.get('ticker')==p['ticker']and o.get('action')==p['action']and o.get('type')=='LIMIT'
                and o.get('period')==p['period']and num(o.get('tick',-1))>=p['tick']and num(o['quantity'])==p['quantity']and abs(num(o['price'])-p['price'])<1e-8):matches.append(o)
        if len(matches)!=1:
            self.say('uncertain','WAIT: order outcome unconfirmed; no duplicate order sent');return False
        oid=int(matches[0]['order_id']);self.known[oid]=time.monotonic();self.api.uncertain=False;self.pending=None
        print('RECOVERED order',oid);return True

    def manage(self,k,retries=0):
        sec,p,opened,g,n=self.snapshot()
        # Invalidate unsafe outstanding orders before considering replacements.
        if not safe(p,opened,g,n):
            self.cancel();sec,p,opened,g,n=self.snapshot()
        reduce_only=self.loss or not safe(p,opened,g,n)
        raw=self.api.get('/securities/book',ticker=k,limit=100)
        book=best_book(raw,self.known)
        self.api.event('book',ticker=k,data=raw)
        if not book:self.say(k,'SKIP '+k+': empty/locked external order book');return
        bid,ask=book;s=sec[k];q=p[k];tick=10**(-int(s['quoted_decimals']))
        rebate=num(s['limit_order_rebate']);pair_edge=ask-bid+2*rebate
        wanted={}
        for side,price in (('BUY',bid),('SELL',ask)):
            reducing=(side=='SELL'and q>0)or(side=='BUY'and q<0)
            if reduce_only and not reducing:continue
            if not reducing and pair_edge<=0:
                self.say((k,side),f'SKIP {k} {side}: spread + two rebates = {pair_edge:.4f} <= 0');continue
            qty=min(self.size,int(s['max_trade_size']))
            if reducing:qty=min(qty,abs(q))
            minimum=max(1,int(s.get('min_trade_size',0)))
            if qty<minimum:
                self.say((k,side),f'SKIP {k} {side}: qty {qty} below API minimum {minimum}');continue
            wanted[side]=(round(price,int(s['quoted_decimals'])),qty)
        cancels=[];seen=set()
        for o in [o for o in opened if o['ticker']==k]:
            side=o['action'];d=wanted.get(side);oid=int(o['order_id'])
            if side in seen or not d:cancels.append(oid);continue
            seen.add(side);price,qty=d
            crossing=(num(o['price'])>=ask if side=='BUY'else num(o['price'])<=bid)
            moved=abs(num(o['price'])-price)>=2*tick-1e-8 and time.monotonic()-self.known[oid]>=2
            if crossing or rem(o)>qty or moved:cancels.append(oid)
        if cancels:
            self.cancel(cancels)
            # A fresh invocation recomputes sizes if fills happened during cancellation.
            if retries>=2:return
            return self.manage(k,retries+1)
        sides=('SELL','BUY')if q>0 else ('BUY','SELL')
        for side in sides:
            if STOP:return
            if side not in wanted:continue
            if any(o['ticker']==k and o['action']==side for o in opened):continue
            # Reconcile again immediately before a new submission.
            sec,p,opened,g,n=self.snapshot()
            if any(o['ticker']==k and o['action']==side for o in opened):continue
            raw=self.api.get('/securities/book',ticker=k,limit=100);book=best_book(raw,self.known)
            if not book:continue
            bid,ask=book;price=bid if side=='BUY'else ask;q=p[k];s=sec[k]
            reducing=(side=='SELL'and q>0)or(side=='BUY'and q<0)
            if (self.loss or not safe(p,opened,g,n))and not reducing:continue
            if not reducing and ask-bid+2*num(s['limit_order_rebate'])<=0:continue
            qty=min(self.size,int(s['max_trade_size']),abs(q)if reducing else STOCK_CAP)
            minimum=max(1,int(s.get('min_trade_size',0)))
            candidate=dict(ticker=k,action=side,quantity=qty,quantity_filled=0,price=price)
            check=(lambda amount:reduction_safe(p,opened,dict(candidate,quantity=amount),g,n))if reducing else (lambda amount:safe(p,opened+[dict(candidate,quantity=amount)],g,n))
            while qty>=minimum and not check(qty):qty-=1
            if qty<minimum:
                self.say((k,side),f'SKIP {k} {side}: position/open-order capacity exhausted');continue
            c=self.case()
            if c.get('status')!='ACTIVE'or int(c['tick'])>=int(c['ticks_per_period']):return
            opened.append(self.send(k,side,round(price,int(s['quoted_decimals'])),qty))

    def run(self):
        self.startup();waiting=False
        while not STOP:
            try:
                c=self.case()
                if c.get('status')!='ACTIVE'or int(c['tick'])>=int(c['ticks_per_period']):
                    if not waiting:self.cancel();print('WAIT: market stopped; waiting for ACTIVE');waiting=True
                    time.sleep(.5);continue
                waiting=False
                if self.api.uncertain and not self.reconcile():time.sleep(1);continue
                offset=self.rotation%4;self.rotation+=1
                for k in TICKERS[offset:]+TICKERS[:offset]:
                    if STOP:break
                    self.manage(k)
                time.sleep(.2)
            except OrderRejected as e:
                print('REJECTED:',str(e),'-- refreshing market, bot stays running')
                time.sleep(max(1,e.delay))
            except ReadUnavailable as e:
                print('READ WAIT:',str(e));time.sleep(1)
            except Uncertain:
                print('WAIT: checking order acceptance in history');time.sleep(.5)

    def cleanup(self):
        if not self.started:return
        try:
            self.cancel();_,p=self.securities();print('STOP: open orders cancelled. Positions:',p)
            print('No automatic close-out on Ctrl+C. Remaining positions require attention.')
            self.api.event('shutdown',positions=p)
        except (Halt,KeyError,ValueError,TypeError):print('Cleanup NOT confirmed: inspect RIT orders/positions')


def main():
    parser=argparse.ArgumentParser(description='SIMPLE-4: standalone continuous LIMIT market maker')
    parser.add_argument('--live',action='store_true');parser.add_argument('--size',type=int,default=100)
    parser.add_argument('--base-url',default='http://localhost:9999/v1')
    a=parser.parse_args()
    if not a.live:parser.error('Run: python strategy.py --live')
    if not 1<=a.size<=500:parser.error('Size must be 1..500')
    key=os.environ.get('RIT_API_KEY')or getpass.getpass('RIT API key (hidden): ')
    if not key.strip():parser.error('Key required')
    signal.signal(signal.SIGINT,stop_signal);signal.signal(signal.SIGTERM,stop_signal)
    Path('live_logs').mkdir(exist_ok=True)
    filename=Path('live_logs')/('simple4_'+dt.datetime.now().strftime('%Y%m%d_%H%M%S')+'_'+uuid.uuid4().hex[:6]+'.jsonl')
    with filename.open('x',encoding='utf-8')as log:
        bot=Bot(API(a.base_url,key,log),a.size)
        print('VERSION: SIMPLE-4 standalone | log:',filename)
        try:bot.run()
        except (Halt,KeyError,ValueError,TypeError)as e:print('STOP:',str(e))
        finally:bot.cleanup()

if __name__=='__main__':main()
