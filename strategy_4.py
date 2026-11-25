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
    tickers:tuple=TICKERS
    size:int=100
    stock_cap:int=500
    gross_cap:int=2000
    loss_limit:float=200.
    refresh:float=1.
    pause_before:int=20
    pause_after:int=4
    final_buffer:int=30
    imbalance_threshold:float=.65
    jump_threshold:float=.15
    cooldown:float=3.


def number(x):
    v=float(x)
    if not math.isfinite(v): raise Halt('Nonfinite API value')
    return v


def quantities(securities):
    result={s['ticker']:int(number(s['position'])) for s in securities}
    if any(number(s['position'])!=result[s['ticker']] for s in securities):raise Halt('Fractional positions unsupported')
    return result


def limits(rows,c):
    stocks=[x for x in rows if x.get('name')=='LIMIT-STOCK']
    if len(stocks)!=1: raise Halt('Expected one LIMIT-STOCK group; inspect case configuration')
    x=stocks[0]
    gross=min(c.gross_cap,int(number(x['gross_limit']))-100)
    net=min(c.gross_cap,int(number(x['net_limit']))-100)
    if min(gross,net)<=0:raise Halt('No safe capacity under current limits')
    return gross,net


def allowed(positions,orders,ticker,side,qty,gross_cap,net_cap,stock_cap):
    """Worst-case envelope for every subset/partial fill of known open orders."""
    buy={k:0 for k in positions};sell=buy.copy()
    for o in orders+[dict(ticker=ticker,action=side,quantity=qty,quantity_filled=0)]:
        k=o['ticker']
        if k not in positions:return False
        rem=max(0,int(number(o['quantity'])-number(o.get('quantity_filled',0))))
        (buy if o['action']=='BUY' else sell)[k]+=rem
    lo={k:q-sell[k] for k,q in positions.items()}
    hi={k:q+buy[k] for k,q in positions.items()}
    return (sum(max(abs(lo[k]),abs(hi[k])) for k in positions)<=gross_cap
            and sum(hi.values())<=net_cap and sum(lo.values())>=-net_cap
            and all(max(abs(lo[k]),abs(hi[k]))<=stock_cap for k in TICKERS))


def book_info(raw):
    sides=[]
    for name in ('bids','asks'):
        levels={}
        for o in raw.get(name,[]):
            rem=max(0,number(o['quantity'])-number(o.get('quantity_filled',0)))
            p=number(o['price'])
            if rem>0: levels[p]=levels.get(p,0)+rem
        sides.append(levels)
    if not all(sides):return None
    bid,ask=max(sides[0]),min(sides[1])
    if not 0<bid<ask:return None
    qb,qa=sides[0][bid],sides[1][ask]
    return bid,ask,(qb-qa)/(qb+qa)


def price_quotes(bid,ask,imbalance,position,cap,tick):
    # One-tick imbalance shift; up to two ticks of inventory skew.
    shift=tick*imbalance-2*tick*position/cap
    b=min(bid,math.floor((bid+shift)/tick+1e-8)*tick)
    a=max(ask,math.ceil((ask+shift)/tick-1e-8)*tick)
    return round(b,8),round(a,8)


class Bot:
    def __init__(self,api,c):
        self.api=api;self.c=c;self.previous={};self.cool_until={};self.started=False
        self.initial_pnl=0.;self.last_print=0.;self.tick_seen=None;self.period_seen=None;self.tracked=set();self.reduce_offset=0

    def state(self):
        rows=self.api.get('/securities')
        if not isinstance(rows,list):raise Halt('Invalid securities response')
        return rows,quantities(rows)

    def open_orders(self):
        rows=self.api.get('/orders',status='OPEN')
        if not isinstance(rows,list):raise Halt('Invalid open-order response')
        return rows

    def case(self):
        case=self.api.get('/case')
        tick=int(case['tick']);period=case['period']
        if self.tick_seen is not None and (period!=self.period_seen or tick<self.tick_seen):
            raise Halt('Case reset/period changed. Restart the bot for the next heat.')
        self.tick_seen=tick;self.period_seen=period
        return case

    def quiet(self,case):
        tick=int(case['tick']);duration=int(case['ticks_per_period'])
        return tick%60>=60-self.c.pause_before or tick%60<self.c.pause_after or tick>=duration-self.c.final_buffer

    def cancel(self):
        # Account-wide cancellation is intentional: exclusive account required.
        answer=self.api.call('POST','/commands/cancel',all=1)
        if isinstance(answer,dict) and answer.get('success') is False:raise Halt('Cancellation rejected')
        for _ in range(5):
            if not self.open_orders():
                for oid in list(self.tracked):
                    status=self.api.get('/orders/'+str(oid))
                    self.api.event('order_reconciled',order=status)
                    self.tracked.remove(oid)
                return
            time.sleep(.2)
        raise Halt('Could not confirm cancellation; no replacement/flatten orders sent')

    def order(self,ticker,side,qty,price):
        if side not in ('BUY','SELL') or qty<=0 or int(qty)!=qty or number(price)<=0:
            raise Halt('Invalid limit order')
        params=dict(ticker=ticker,type='LIMIT',quantity=int(qty),action=side,price=price)
        reply=self.api.call('POST','/orders',**params)
        if not isinstance(reply,dict) or 'order_id' not in reply:
            self.api.uncertain=True
            raise Uncertain('Missing order acknowledgement; inspect RIT before restarting')
        self.tracked.add(int(reply['order_id']))
        return reply

    def flatten(self,seconds=5.):
        """Bounded passive close-out attempt; no execution/flatness guarantee.

        One reducing order per stock per pass. Cancel and confirm before retrying;
        a partial fill never turns into an order larger than remaining inventory.
        All orders left unfilled at the end of the attempt are cancelled.
        """
        deadline=time.monotonic()+seconds
        first=True
        try:
            while first or time.monotonic()<deadline:
                first=False
                self.cancel()
                case=self.case()
                if case.get('status')!='ACTIVE' or int(case['tick'])>=int(case['ticks_per_period']):return False
                _,p=self.state()
                if not any(p.values()):return True
                offset=self.reduce_offset%len(self.c.tickers)
                reduction_tickers=self.c.tickers[offset:]+self.c.tickers[:offset]
                self.reduce_offset+=1
                for k in reduction_tickers:
                    if time.monotonic()>=deadline:break
                    # One side only; outstanding orders were cancelled above.
                    rows,current=self.state();q=current[k]
                    if not q:continue
                    raw=self.api.get('/securities/book',ticker=k,limit=100)
                    info=book_info(raw)
                    self.api.event('reduction_book',ticker=k,book=raw)
                    if info is None:continue
                    bid,ask,_=info
                    fresh=self.case()
                    if fresh.get('status')!='ACTIVE' or int(fresh['tick'])>=int(fresh['ticks_per_period']):return False
                    sec=next(x for x in rows if x['ticker']==k)
                    qty=min(abs(q),self.c.size,int(sec['max_trade_size']))
                    if qty<int(sec.get('min_trade_size',0)):continue
                    # Sell long inventory at the ask; cover short at the bid.
                    # No deliberate spread crossing, even for risk reduction.
                    self.order(k,'SELL' if q>0 else 'BUY',qty,ask if q>0 else bid)
                time.sleep(min(1.,max(0.,deadline-time.monotonic())))
            return False
        finally:
            self.cancel()
            _,remaining=self.state()
            self.api.event('passive_close_attempt',positions=remaining)

    def startup(self):
        case=self.case()
        if 'market making' not in str(case.get('name','')).lower():raise Halt('This is not the expected Market Making case')
        rows,p=self.state()
        if not set(TICKERS).issubset(p):raise Halt('Expected SPNG, SMMR, ATMN, WNTR')
        if any(p.values()) or self.open_orders():raise Halt('Start with zero positions and no open orders; use a dedicated account')
        if self.c.tickers!=TICKERS:raise Halt('This version requires all four instruments')
        for s in rows:
            if s['ticker'] not in TICKERS:continue
            groups=s.get('limits',[])
            if len(groups)!=1 or groups[0].get('name')!='LIMIT-STOCK' or number(groups[0].get('units',0))!=1:
                raise Halt('Unsupported position-limit mapping')
            if number(s.get('execution_delay_ms',0))!=0:raise Halt('This version requires zero configured execution delay')
            if int(s.get('api_orders_per_second',0))<8:raise Halt('Order-rate setting too low for this version')
            if self.c.size>number(s['max_trade_size']):raise Halt('Quote size exceeds security maximum')
            if number(s.get('min_trade_size',0))>self.c.size:raise Halt('Quote size below security minimum')
            if number(s['limit_order_rebate'])<0:raise Halt('Unexpected passive fee')
            print(s['ticker'],'passive rebate',s['limit_order_rebate'],'market fee',s['trading_fee'])
        limits(self.api.get('/limits'),self.c)
        self.initial_pnl=sum(number(s.get('realized',0))+number(s.get('unrealized',0)) for s in rows)
        self.started=True
        self.api.event('strategy_start',config=self.c.__dict__)
        print('LIVE LIMIT-ONLY:',','.join(self.c.tickers),'size',self.c.size,'stock cap',self.c.stock_cap,'gross cap',self.c.gross_cap)

    def run(self,seconds):
        self.startup();end=time.monotonic()+seconds;active_seen=False
        while not STOP and time.monotonic()<end:
            case=self.case();tick=int(case['tick']);duration=int(case['ticks_per_period'])
            if case.get('status')!='ACTIVE':
                self.cancel()
                if active_seen:break
                time.sleep(.5);continue
            active_seen=True
            if tick>=duration-self.c.final_buffer:
                self.flatten(seconds=12.);break
            if self.quiet(case):
                self.flatten(seconds=5.)
                time.sleep(.3);continue
            # Rebuild only after cancellation is confirmed. Deliberate simple baseline;
            # repeated replacement sacrifices queue priority.
            self.cancel()
            rows,p=self.state()
            pnl=sum(number(s.get('realized',0))+number(s.get('unrealized',0)) for s in rows)-self.initial_pnl
            self.api.event('account_snapshot',tick=tick,securities=rows,pnl_change=pnl)
            if pnl<=-self.c.loss_limit:
                print('Loss threshold reached; attempting passive reduction.');self.flatten(seconds=12.);break
            g,n=limits(self.api.get('/limits'),self.c)
            if sum(abs(q)for q in p.values())>g or abs(sum(p.values()))>n or any(abs(p[k])>self.c.stock_cap for k in TICKERS):
                self.flatten();continue
            offset=tick%len(self.c.tickers)
            order_tickers=self.c.tickers[offset:]+self.c.tickers[:offset]
            for k in order_tickers:
                if STOP:break
                raw=self.api.get('/securities/book',ticker=k,limit=100)
                info=book_info(raw)
                self.api.event('book_snapshot',ticker=k,tick_hint=tick,book=raw)
                if info is None:continue
                bid,ask,imb=info;mid=(bid+ask)/2;now=time.monotonic()
                if k in self.previous and abs(mid-self.previous[k])>=self.c.jump_threshold:
                    self.cool_until[k]=now+self.c.cooldown
                self.previous[k]=mid
                if now<self.cool_until.get(k,0):continue
                sec=next(s for s in rows if s['ticker']==k)
                tick_size=10**(-int(sec['quoted_decimals']))
                if ask-bid>20*tick_size:continue  # abnormally wide/uncertain book
                buy_price,sell_price=price_quotes(bid,ask,imb,p[k],self.c.stock_cap,tick_size)
                for side,price in (('BUY',buy_price),('SELL',sell_price)):
                    if STOP:break
                    # Reconcile position and remaining orders, including partial fills.
                    # Read orders first, then positions: fills in between are
                    # conservatively double-counted rather than omitted.
                    orders=self.open_orders();_,current=self.state()
                    if any(o.get('ticker') not in TICKERS for o in orders):raise Halt('External order detected')
                    g,n=limits(self.api.get('/limits'),self.c)
                    # Refresh the book after reconciliation, then check the clock.
                    latest=book_info(self.api.get('/securities/book',ticker=k,limit=100))
                    if latest is None:continue
                    fresh_bid,fresh_ask,fresh_imb=latest
                    if abs((fresh_bid+fresh_ask)/2-mid)>=self.c.jump_threshold:
                        self.cool_until[k]=time.monotonic()+self.c.cooldown
                        break
                    imb=fresh_imb
                    prices=price_quotes(fresh_bid,fresh_ask,imb,current[k],self.c.stock_cap,tick_size)
                    price=prices[0 if side=='BUY' else 1]
                    fresh=self.case()
                    if fresh.get('status')!='ACTIVE' or self.quiet(fresh):break
                    adverse=(side=='BUY' and imb < -self.c.imbalance_threshold) or (side=='SELL' and imb>self.c.imbalance_threshold)
                    if adverse:continue
                    if side=='BUY' and current[k]>=self.c.stock_cap:continue
                    if side=='SELL' and current[k]<=-self.c.stock_cap:continue
                    if not allowed(current,orders,k,side,self.c.size,g,n,self.c.stock_cap):continue
                    if price<=0:continue
                    # Cached quote is passive against observed book; moving prices can
                    # still make a limit order marketable before it reaches the engine.
                    self.api.event('quote',ticker=k,action=side,price=price,quantity=self.c.size,imbalance=imb)
                    self.order(k,side,self.c.size,price)
            if time.monotonic()-self.last_print>=5:
                print(f'tick={tick:3d} PnL change={pnl:+.2f} positions={p}')
                self.last_print=time.monotonic()
            time.sleep(self.c.refresh)

    def cleanup(self):
        if not self.started:return
        print('Stopping: cancelling quotes and attempting passive inventory reduction...')
        try:
            self.cancel()
            if self.api.uncertain:
                print('Uncertain submission: cancellation attempted; inspect positions manually. No additional orders sent.')
                return
            flat=self.flatten(seconds=10.)
            _,p=self.state()
            self.api.event('shutdown',positions=p)
            print('Remaining positions:',p)
            if any(p.values()):print('ATTENTION: passive orders did not close all inventory; inspect RIT positions.')
        except (Halt,KeyboardInterrupt,KeyError,TypeError,ValueError) as error:
            print('Cleanup could not be confirmed:',str(error),'Inspect RIT open orders and positions now.')


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--live',action='store_true',help='Enable actual orders in your RIT simulation')
    parser.add_argument('--base-url',default='http://localhost:9999/v1')
    parser.add_argument('--size',type=int,default=100)
    parser.add_argument('--stock-cap',type=int,default=500)
    parser.add_argument('--gross-cap',type=int,default=2000)
    parser.add_argument('--loss-limit',type=float,default=200)
    parser.add_argument('--seconds',type=float,default=420)
    args=parser.parse_args()
    if not args.live:parser.error('To trade in the simulation, run with --live')
    if not 1<=args.size<=args.stock_cap<=500 or not args.stock_cap<=args.gross_cap<=2000:
        parser.error('Practice limits: 1 <= size <= stock-cap <= 500; stock-cap <= gross-cap <= 2000')
    if not math.isfinite(args.seconds) or args.seconds<=0 or not math.isfinite(args.loss_limit) or args.loss_limit<=0:
        parser.error('Seconds and loss limit must be finite and positive')
    key=os.environ.get('RIT_API_KEY')or getpass.getpass('RIT API key (hidden): ')
    if not key.strip():parser.error('API key required')
    for sig in (signal.SIGINT,signal.SIGTERM):signal.signal(sig,stop_signal)
    folder=Path('live_logs');folder.mkdir(exist_ok=True)
    filename=folder/('live_'+dt.datetime.now().strftime('%Y%m%d_%H%M%S')+'_'+uuid.uuid4().hex[:6]+'.jsonl')
    with filename.open('x',encoding='utf-8')as log:
        api=API(args.base_url,key,log)
        bot=Bot(api,Config(size=args.size,stock_cap=args.stock_cap,gross_cap=args.gross_cap,loss_limit=args.loss_limit))
        print('Log:',filename)
        try:bot.run(args.seconds)
        except (Halt,KeyError,TypeError,ValueError)as error:print('Stopped:',str(error))
        finally:bot.cleanup()

if __name__=='__main__':main()
