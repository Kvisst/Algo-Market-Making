"""Read-only Windows RIT REST recorder. Python 3.10+, standard library only."""
import argparse
import datetime as dt
import getpass
import json
import os
from pathlib import Path
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid


class StopRecording(Exception):
    pass


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None  # Never forward an API key to another host.


class Recorder:
    def __init__(self, base, key, output, request_gap=.10, timeout=3.):
        parsed = urllib.parse.urlsplit(base)
        if parsed.scheme not in ('http', 'https') or not parsed.hostname:
            raise ValueError('Supply an http(s) REST base URL')
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError('Base URL must not contain credentials, query or fragment')
        self.base, self.key, self.output = base.rstrip('/'), key, output
        self.gap, self.timeout, self.next_request = request_gap, timeout, 0.
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
        self.seq, self.cycle, self.heat = 0, 0, 0
        self.previous_case = None
        self.disabled = set()
        self.cooldown = 0.
        self.emit({'kind': 'metadata', 'schema': 1, 'session': str(uuid.uuid4()),
                   'base_url': self.base, 'request_gap_seconds': request_gap,
                   'note': 'GET only; polled responses are asynchronous, not atomic snapshots'})

    def emit(self, row):
        self.output.write(json.dumps(row, allow_nan=False, separators=(',', ':'))+'\n')
        self.output.flush()

    def fetch(self, endpoint, params=None, optional=False):
        params = params or {}
        if endpoint in self.disabled:
            return None
        time.sleep(max(0., self.next_request-time.monotonic()))
        started = time.monotonic()
        row = {'kind':'response', 'seq':self.seq, 'cycle':self.cycle, 'heat':self.heat,
               'endpoint':endpoint, 'params':params,
               'request_utc':dt.datetime.now(dt.timezone.utc).isoformat(),
               'request_monotonic':started}
        self.seq += 1
        query = urllib.parse.urlencode(params)
        url = self.base + endpoint + ('?'+query if query else '')
        request = urllib.request.Request(url, headers={'X-API-key':self.key}, method='GET')
        data, fatal = None, None
        try:
            with self.opener.open(request, timeout=self.timeout) as response:
                row['http_status'] = response.status
                try:
                    data = json.loads(response.read())
                    row['data'] = data
                except (ValueError, UnicodeError):
                    row['error'] = 'Invalid JSON response'
        except urllib.error.HTTPError as error:
            row['http_status'] = error.code
            row['error'] = 'HTTP error'  # No headers or response bodies: avoid credential echoes.
            if error.code in (401,403):
                fatal = 'Authentication/access rejected. Check the RIT client API settings and key.'
            elif error.code == 429:
                try: delay = float(error.headers.get('Retry-After','2'))
                except ValueError: delay = 2.
                self.cooldown = max(2., min(60., delay))
            elif optional and error.code in (400,404,405):
                self.disabled.add(endpoint)
                print(f'Optional endpoint unavailable: {endpoint}; raw error recorded.')
            elif 300 <= error.code < 400:
                fatal = 'Redirect rejected. Use the direct RIT REST base URL.'
        except (urllib.error.URLError, TimeoutError, OSError):
            row['error'] = 'Connection failure or timeout'
            self.cooldown = 2.
        finally:
            row['response_monotonic'] = time.monotonic()
            row['latency_ms'] = (row['response_monotonic']-started)*1000
            self.next_request = row['response_monotonic']+self.gap
        # Label resets on the case response itself, before recording it.
        if endpoint == '/case' and isinstance(data,dict):
            current = (data.get('period'),data.get('tick'))
            if self.previous_case is not None:
                old_period,old_tick = self.previous_case
                p,t = current
                if ((isinstance(p,(int,float)) and isinstance(old_period,(int,float)) and p<old_period)
                    or (p==old_period and isinstance(t,(int,float)) and isinstance(old_tick,(int,float)) and t<old_tick)):
                    self.heat += 1
                    print('Case clock reset detected; starting a new heat segment.')
            self.previous_case = current
            row['heat'] = self.heat
        self.emit(row)
        if fatal: raise StopRecording(fatal)
        if self.cooldown:
            delay,self.cooldown = self.cooldown,0.
            time.sleep(delay)
        return data

    def run(self, tickers, duration, interval, depth, tape_limit):
        end = time.monotonic()+duration
        slow_due, progress_due = 0., 0.
        while time.monotonic()<end:
            cycle_start = time.monotonic()
            case = self.fetch('/case')
            if not isinstance(case,dict) or 'tick' not in case:
                time.sleep(min(1.,max(0.,end-time.monotonic())))
                continue
            if cycle_start>=slow_due:
                self.fetch('/limits',optional=True)
                self.fetch('/trader',optional=True)
                self.fetch('/news',optional=True)  # May contain the announced CRO limit.
                slow_due = time.monotonic()+5.
            self.fetch('/securities')
            self.fetch('/orders',{'status':'OPEN'},optional=True)
            self.fetch('/orders',{'status':'TRANSACTED'},optional=True)
            for ticker in tickers:
                self.fetch('/securities/book',{'ticker':ticker,'limit':depth})
                self.fetch('/securities/tas',{'ticker':ticker,'limit':tape_limit},optional=True)
            # Bracket each cycle: never pretend sequential responses are simultaneous.
            after = self.fetch('/case')
            self.emit({'kind':'cycle_end','cycle':self.cycle,'heat':self.heat,
                       'case_before':case,'case_after':after,
                       'elapsed_seconds':time.monotonic()-cycle_start})
            if time.monotonic()>=progress_due:
                print(f'cycle={self.cycle} heat_segment={self.heat} tick={case.get("tick")} status={case.get("status")}')
                progress_due = time.monotonic()+10.
            self.cycle += 1
            time.sleep(max(0., min(interval-(time.monotonic()-cycle_start),end-time.monotonic())))


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--base-url',default='http://localhost:9999/v1')
    parser.add_argument('--output',type=Path)
    parser.add_argument('--seconds',type=float,default=420)
    parser.add_argument('--interval',type=float,default=1.,help='Minimum cycle duration; requests may make cycles longer')
    parser.add_argument('--request-gap',type=float,default=.10,help='Delay after each request; increase if rate limited')
    parser.add_argument('--depth',type=int,default=100)
    parser.add_argument('--tape-limit',type=int,default=1000)
    parser.add_argument('--tickers',nargs='+',default=['SPNG','SMMR','ATMN','WNTR'])
    args=parser.parse_args()
    if min(args.seconds,args.interval,args.depth,args.tape_limit)<=0 or args.request_gap<0:
        parser.error('Durations and sizes must be positive; request gap cannot be negative')
    key=os.environ.get('RIT_API_KEY') or getpass.getpass('RIT API key (hidden): ')
    if not key.strip(): parser.error('API key is required')
    output=args.output or Path('recordings')/('rit_'+dt.datetime.now().strftime('%Y%m%d_%H%M%S')+'_'+uuid.uuid4().hex[:6]+'.jsonl')
    output.parent.mkdir(parents=True,exist_ok=True)
    try:
        with output.open('x',encoding='utf-8') as stream:
            print(f'Recording to {output}. Press Ctrl+C to stop.')
            Recorder(args.base_url,key,stream,args.request_gap).run(args.tickers,args.seconds,args.interval,args.depth,args.tape_limit)
    except KeyboardInterrupt:
        print('\nStopped; completed responses are saved.')
    except StopRecording as error:
        print(str(error))
        raise SystemExit(1)
    except FileExistsError:
        parser.error('Output exists; choose a new filename')

if __name__=='__main__': main()
