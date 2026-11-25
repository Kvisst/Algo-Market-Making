"""RITC tender_v2: samodzielny Client REST bot, tylko symulacja RIT.
Podglad: python -u tender_v2.py
Handel i kolejne rundy: python -u tender_v2.py --live
Jedna runda: dodaj --once.
Wymaga requests i RIT_API_KEY; RIT_BASE_URL domyslnie localhost:9999/v1.
Bufor 0.03 CAD/akcje, prog 500 CAD po buforze; cutoff domyslnie 20 tickow.
"""

import argparse
import json
import os
import sys
import time

import math
from pathlib import Path

import requests


class RITError(Exception):
    pass


class RITClient:
    def __init__(self, api_key, base_url="http://localhost:9999/v1"):
        self.base_url = base_url.rstrip("/")
        self.session = requests.Session()
        # Lokalny klient RIT nie wymaga firmowego proxy HTTP.
        self.session.trust_env = False
        self.session.headers.update({"X-API-Key": api_key})

    def get(self, endpoint, **params):
        try:
            response = self.session.get(
                f"{self.base_url}/{endpoint.lstrip('/')}",
                params=params,
                timeout=(3, 5),
            )
        except requests.exceptions.ConnectionError as exc:
            raise RITError(
                "Brak polaczenia z API. Uruchom klienta RIT, zaloguj sie "
                "i sprawdz wlaczenie REST API oraz jego port."
            ) from exc
        except requests.exceptions.Timeout as exc:
            raise RITError("API nie odpowiedzialo w wyznaczonym czasie.") from exc
        if response.status_code in (401, 403):
            raise RITError(
                f"HTTP {response.status_code}: sprawdz klucz API i uprawnienia API w RIT."
            )
        if response.status_code == 429:
            raise RITError("HTTP 429: za duzo zapytan. Odczekaj i uruchom ponownie.")
        if not response.ok:
            raise RITError(f"GET /{endpoint}: HTTP {response.status_code}")
        try:
            return response.json()
        except ValueError as exc:
            raise RITError(f"GET /{endpoint}: odpowiedz nie jest JSON-em.") from exc

    def close(self):
        self.session.close()


TICKERS = {"BLU", "RED", "GRN"}
# Parametry startowe do testow, a nie parametry ustalone przez organizatorow.
BUFFER_PER_SHARE = 0.03
MIN_PROFIT = 500.0
MIN_TIME_LEFT = 20
MIN_OFFER_TIME = 3


def sweep_book(book, close_action, quantity, own_order_ids=()):
    """Symulacja wykonania po kolejnych ofertach; uwzglednia reszte zlecenia."""
    side = "asks" if close_action == "BUY" else "bids"
    orders = sorted(book[side], key=lambda row: float(row["price"]),
                    reverse=close_action == "SELL")
    remaining = quantity
    value = 0.0
    for row in orders:
        if row.get("order_id") in own_order_ids:
            continue
        available = max(0.0, float(row["quantity"]) - float(row["quantity_filled"]))
        take = min(remaining, available)
        value += take * float(row["price"])
        remaining -= take
        if remaining <= 0:
            break
    filled = quantity - remaining
    return {"filled": filled, "missing": remaining,
            "vwap": value / filled if filled else None, "value": value}


def analyze(tender, securities, limits, case, book, open_orders, cutoff=MIN_TIME_LEFT):
    """Ocena pojedynczego tendera przy zalozeniu startu ze wszystkimi pozycjami 0."""
    ticker = tender["ticker"]
    action = tender["action"]
    quantity = float(tender["quantity"])
    price = float(tender["price"])
    if action not in ("BUY", "SELL") or quantity <= 0 or price <= 0:
        raise ValueError("Niepoprawny kierunek, wolumen lub cena tendera")
    security = securities[ticker]
    fee = float(security["trading_fee"])
    if fee < 0:
        raise ValueError("Ujemna prowizja: wymaga sprawdzenia interpretacji API")
    max_order = float(security["max_trade_size"])
    if max_order <= 0:
        raise ValueError("Niepoprawny maksymalny rozmiar zlecenia")
    delta = quantity if action == "BUY" else -quantity
    positions = {name: float(securities[name]["position"]) for name in TICKERS}
    projected = dict(positions)
    projected[ticker] += delta
    gross = sum(abs(pos) for pos in projected.values())
    net = sum(projected.values())
    limit = next(row for row in limits if row["name"] == "LIMIT-STOCK")
    close_action = "SELL" if action == "BUY" else "BUY"
    sweep = sweep_book(book, close_action, quantity,
                       {row["order_id"] for row in open_orders})
    reasons = []
    if case["status"] != "ACTIVE":
        reasons.append("runda nieaktywna")
    if tender["period"] != case["period"]:
        reasons.append("oferta z innego okresu")
    if not tender.get("is_fixed_bid", False):
        reasons.append("oferta wymaga podania ceny; ta wersja obsluguje stala cene")
    if any(abs(pos) > 0.000001 for pos in positions.values()):
        reasons.append("istnieje otwarta pozycja; najpierw trzeba ja zamknac")
    if open_orders:
        reasons.append("istnieja aktywne zlecenia")
    if gross > float(limit["gross_limit"]) or abs(net) > float(limit["net_limit"]):
        reasons.append("przekroczenie limitu po przyjeciu")
    if case["ticks_per_period"] - case["tick"] <= cutoff:
        reasons.append(f"blokada nowych transakcji: zostalo <= {cutoff} tickow")
    if tender["expires"] - case["tick"] < MIN_OFFER_TIME:
        reasons.append("oferta wygasla lub zaraz wygasa")
    profit = None
    buffered = None
    if sweep["missing"] > 0:
        reasons.append("za mala widoczna glebokosc; nie wyceniam calego tendera")
    else:
        # BUY tender: kupujemy od instytucji, sprzedajemy na bidach.
        # SELL tender: sprzedajemy instytucji, odkupujemy z askow.
        trading_edge = (sweep["value"] - quantity * price if action == "BUY"
                        else quantity * price - sweep["value"])
        profit = trading_edge - quantity * fee
        buffered = profit - quantity * BUFFER_PER_SHARE
        if buffered < MIN_PROFIT:
            reasons.append("zysk po buforze mniejszy niz 500 CAD")
    return {
        "id": tender["tender_id"], "ticker": ticker, "action": action,
        "quantity": quantity, "price": price, "close_action": close_action,
        "vwap": sweep["vwap"], "missing": sweep["missing"],
        "fee": fee * quantity, "profit": profit, "buffered": buffered,
        "gross": gross, "net": net, "orders": math.ceil(quantity / max_order),
        "reasons": reasons,
    }


def show(result):
    r = result
    print(f"\nTENDER {r['id']} | {r['ticker']} | TWOJE {r['action']} "
          f"{r['quantity']:,.0f} @ {r['price']:.6f}")
    print(f"Zamkniecie: {r['close_action']}; minimum zlecen: {r['orders']}")
    print(f"Po przyjeciu: gross={r['gross']:,.0f}, net={r['net']:,.0f}")
    if r["missing"]:
        print(f"Brakuje ofert na {r['missing']:,.0f} akcji w pobranym arkuszu.")
    else:
        print(f"Srednia cena zamkniecia wg arkusza: {r['vwap']:.6f}")
        print(f"Prowizja zamkniecia: {r['fee']:,.2f} CAD")
        print(f"Szacowany zysk po prowizji: {r['profit']:,.2f} CAD")
        print(f"Zysk po buforze {BUFFER_PER_SHARE:.2f}/akcje: {r['buffered']:,.2f} CAD")
    if r["reasons"]:
        print("POMIN W MODELU: " + "; ".join(r["reasons"]))
    else:
        print("KANDYDAT WG MODELU - wymaga ponownej wyceny przed przyjeciem.")


ROOT = Path(__file__).resolve().parent
TRADE_LOCK = ROOT / "active_trade.json"
PROCESS_LOCK = ROOT / "bot_running.lock"
CONFIRM_TIMEOUT = 8.0
POLL_INTERVAL = 0.2


class RoundEnded(RuntimeError):
    pass


def integer(value):
    number = float(value)
    if not math.isfinite(number) or not number.is_integer():
        raise RuntimeError(f"Niepoprawna liczba akcji: {value}")
    return int(number)


def securities(client):
    result = {row["ticker"]: row for row in client.get("securities")}
    if not TICKERS.issubset(result):
        raise RuntimeError("Brakuje BLU, RED lub GRN.")
    return result


def positions(market):
    return {ticker: integer(market[ticker]["position"]) for ticker in TICKERS}


def require_flat(client):
    market = securities(client)
    if any(positions(market).values()) or client.get("orders", status="OPEN"):
        raise RuntimeError("Pozycje lub aktywne zlecenia nie sa zerowe. Sprawdz RIT.")
    return market


def read_nlv(client):
    value = client.get("trader").get("nlv")
    return float(value) if value is not None else None


def difference(after, before):
    return after - before if after is not None and before is not None else None


def save_trade(state):
    temporary = TRADE_LOCK.with_suffix(".tmp")
    temporary.write_text(json.dumps(state, indent=2), encoding="utf-8")
    os.replace(temporary, TRADE_LOCK)


def post_once(client, endpoint, **params):
    try:
        response = client.session.post(
            f"{client.base_url}/{endpoint}", params=params, timeout=(3, 5)
        )
        response.raise_for_status()
        return response.json()
    except (requests.RequestException, ValueError) as error:
        raise RuntimeError(
            f"Niepotwierdzony POST /{endpoint}: {error}. "
            "Nie ponawiam. Sprawdz pozycje i zlecenia w RIT."
        ) from error


class RoundGuard:
    def __init__(self, start):
        self.period = start["period"]
        self.last_tick = start["tick"]
        self.length = start["ticks_per_period"]

    def read(self, client, require_active=False):
        case = client.get("case")
        if (case["period"] != self.period or case["tick"] < self.last_tick
                or case["status"] == "STOPPED"):
            raise RoundEnded("Runda zakonczona lub zresetowana.")
        self.last_tick = case["tick"]
        if require_active and case["status"] != "ACTIVE":
            raise RuntimeError("Runda nieaktywna podczas transakcji. Sprawdz RIT.")
        return case


def wait_position(client, guard, expected):
    deadline = time.monotonic() + CONFIRM_TIMEOUT
    actual = None
    while time.monotonic() < deadline:
        guard.read(client, require_active=True)
        actual = positions(securities(client))
        opened = client.get("orders", status="OPEN")
        guard.read(client, require_active=True)
        if actual == expected and not opened:
            return
        time.sleep(0.1)
    raise RuntimeError(
        f"Nie potwierdzono pozycji {expected}. Ostatni odczyt: {actual}. "
        "Sprawdz Position i Open Orders w RIT."
    )


class Journal:
    def __init__(self, path):
        self.path = path
        self.evaluation_state = {}

    def log(self, event, **data):
        with self.path.open("a", encoding="utf-8") as file:
            file.write(json.dumps({
                "time": time.strftime("%Y-%m-%d %H:%M:%S"),
                "event": event, **data,
            }, ensure_ascii=False, allow_nan=False) + "\n")

    def evaluation(self, tender, result, tick, stage="scan"):
        # Wszystkie powody zapisujemy przy pierwszej ocenie i kazdej zmianie.
        # Dodatkowo wycena co tick; brak setek identycznych wpisow w jednym ticku.
        key = (tender["tender_id"], stage)
        signature = (tick, tuple(result["reasons"]))
        if self.evaluation_state.get(key) != signature:
            self.evaluation_state[key] = signature
            self.log("tender_evaluation", tick=tick, stage=stage,
                     tender_id=tender["tender_id"], analysis=result)
        return not result["reasons"]


def assess(client, guard, tender, cutoff, journal, stage="scan"):
    market = require_flat(client)
    limits = client.get("limits")
    book = client.get("securities/book", ticker=tender["ticker"], limit=1000)
    case = guard.read(client)
    result = analyze(tender, market, limits, case, book, [], cutoff=cutoff)
    journal.evaluation(tender, result, case["tick"], stage)
    return result, market, case


def execute(client, guard, tender, cutoff, journal):
    before_nlv = read_nlv(client)
    result, market, case = assess(
        client, guard, tender, cutoff, journal, stage="before_accept"
    )
    if result["reasons"]:
        return None
    checked_at = time.monotonic()
    ticker = tender["ticker"]
    quantity = integer(tender["quantity"])
    max_size = integer(market[ticker]["max_trade_size"])
    if quantity <= 0 or max_size <= 0:
        raise RuntimeError("Niepoprawny rozmiar transakcji.")
    rate = float(market[ticker].get("api_orders_per_second", 0) or 0)
    gap = max(0.25, 1.05 / rate) if rate > 0 else 0.25
    expected = {name: 0 for name in TICKERS}
    expected[ticker] = quantity if tender["action"] == "BUY" else -quantity
    state = {"version": "v2", "tender_id": tender["tender_id"],
             "ticker": ticker, "phase": "PRZED_PRZYJECIEM",
             "expected_positions": dict(expected)}
    fresh = guard.read(client, require_active=True)
    if (fresh["ticks_per_period"] - fresh["tick"] <= cutoff
            or tender["expires"] - fresh["tick"] < MIN_OFFER_TIME
            or fresh["tick"] - case["tick"] > 1
            or time.monotonic() - checked_at > 0.5):
        journal.log("accept_deferred", tender_id=tender["tender_id"],
                    reason="wycena nieaktualna lub za malo czasu")
        return None
    with TRADE_LOCK.open("x", encoding="utf-8") as file:
        json.dump(state, file, indent=2)
    journal.log("trade_start", tender=tender, analysis=result, tick=fresh["tick"])
    started = time.perf_counter()
    accepted = post_once(client, f"tenders/{tender['tender_id']}", price=tender["price"])
    if not isinstance(accepted, dict) or accepted.get("success") is not True:
        raise RuntimeError(f"Nie potwierdzono przyjecia tendera: {accepted}")
    state["phase"] = "TENDER_PRZYJETY"
    save_trade(state)
    journal.log("tender_accepted", tender_id=tender["tender_id"])
    wait_position(client, guard, expected)
    print(f"PRZYJETO {tender['tender_id']}: {ticker} {expected[ticker]}", flush=True)
    fill_value = 0.0
    fill_prices_complete = True
    while expected[ticker]:
        guard.read(client, require_active=True)
        if positions(securities(client)) != expected:
            raise RuntimeError("Nieoczekiwana zmiana pozycji. Sprawdz RIT.")
        if client.get("orders", status="OPEN"):
            raise RuntimeError("Wykryto aktywne zlecenie przed kolejna partia.")
        amount = min(abs(expected[ticker]), max_size)
        action = "SELL" if expected[ticker] > 0 else "BUY"
        next_expected = dict(expected)
        next_expected[ticker] += -amount if action == "SELL" else amount
        state.update(phase="PRZED_ZLECENIEM", action=action,
                     quantity=amount, expected_positions=next_expected)
        save_trade(state)
        sent_at = time.monotonic()
        order = post_once(client, "orders", ticker=ticker, type="MARKET",
                          quantity=amount, action=action)
        if not isinstance(order, dict) or "order_id" not in order:
            raise RuntimeError(f"Nieoczekiwana odpowiedz na zlecenie: {order}")
        state.update(phase="ZLECENIE_WYSLANE", order_id=order["order_id"])
        save_trade(state)
        journal.log("order_sent", ticker=ticker, action=action,
                    quantity=amount, response=order)
        wait_position(client, guard, next_expected)
        expected = next_expected
        journal.log("position_confirmed", positions=expected)
        # Ceny sa wiarygodne tylko dla calej potwierdzonej ilosci.
        if (float(order.get("quantity_filled", 0)) == amount
                and order.get("vwap") is not None
                and math.isfinite(float(order["vwap"])) and float(order["vwap"]) > 0):
            fill_value += amount * float(order["vwap"])
        else:
            fill_prices_complete = False
        print(f"WYKONANO {action} {amount}; {ticker}: {expected[ticker]}", flush=True)
        if expected[ticker]:
            time.sleep(max(0, gap - (time.monotonic() - sent_at)))
    require_flat(client)
    guard.read(client, require_active=True)
    state["phase"] = "ZAMKNIETO"
    save_trade(state)
    actual_profit = None
    if fill_prices_complete:
        edge = (fill_value - quantity * tender["price"] if tender["action"] == "BUY"
                else quantity * tender["price"] - fill_value)
        actual_profit = edge - quantity * float(market[ticker]["trading_fee"])
    elapsed = time.perf_counter() - started
    # Ten NLV moze jeszcze nie uwzgledniac opoznionego rozliczenia.
    after_nlv = read_nlv(client)
    guard.read(client, require_active=True)
    journal.log("trade_completed", tender_id=tender["tender_id"], seconds=elapsed,
                estimated_profit=result["profit"], actual_profit=actual_profit,
                actual_profit_basis="pelne wykonania VWAP minus prowizja market",
                nlv_before=before_nlv, nlv_after=after_nlv,
                nlv_change=difference(after_nlv, before_nlv))
    TRADE_LOCK.unlink()
    print(f"ZAMKNIETO: {elapsed:.3f} s | wynik z wykonan: {actual_profit} CAD", flush=True)
    return {"actual_profit": actual_profit, "nlv": after_nlv}


def run(client, live, cutoff):
    if TRADE_LOCK.exists():
        raise RuntimeError("Istnieje active_trade.json. Sprawdz pozycje i zlecenia w RIT.")
    require_flat(client)
    print(f"{'HANDEL' if live else 'PODGLAD'} | cutoff={cutoff} | bufor=0.03 | prog=500", flush=True)
    last_message = 0
    while True:
        start = client.get("case")
        if start["status"] == "ACTIVE" and 0 <= start["tick"] <= 3:
            break
        if time.monotonic() - last_message >= 5:
            print(f"Czekam na poczatek rundy: {start['status']}, tick {start['tick']}", flush=True)
            last_message = time.monotonic()
        time.sleep(POLL_INTERVAL)
    if start["name"] != "Liquidity Trading" or start["ticks_per_period"] != 300:
        raise RuntimeError("Nieoczekiwany case lub dlugosc rundy.")
    guard = RoundGuard(start)
    require_flat(client)
    baseline = read_nlv(client)
    guard.read(client, require_active=True)
    stamp = time.strftime("%Y%m%d_%H%M%S")
    journal = Journal(ROOT / f"session_v2_{stamp}_{os.getpid()}_{time.time_ns() % 1000000}.jsonl")
    journal.log("start", version="v2", live=live, case=start, nlv=baseline,
                settings={"cutoff": cutoff, "buffer_per_share": BUFFER_PER_SHARE,
                          "min_profit": MIN_PROFIT})
    observed, qualified, completed, displayed = set(), set(), set(), set()
    last_nlv, last_sample, actual_total = baseline, -10, 0.0
    prices_complete, normal_end = True, False
    print(f"START RUNDY | {journal.path.name}", flush=True)
    try:
        while True:
            case = guard.read(client)
            if case["status"] != "ACTIVE":
                time.sleep(0.5)
                continue
            require_flat(client)
            tenders = client.get("tenders")
            now = guard.read(client)
            for tender in tenders:
                tid = tender["tender_id"]
                if tender.get("period") == guard.period and tid not in observed:
                    observed.add(tid)
                    journal.log("tender_observed", tick=now["tick"], tender=tender)
                    print(f"OFERTA #{len(observed)}: {tid} {tender.get('ticker')}", flush=True)
            if now["tick"] - last_sample >= 5:
                value = read_nlv(client)
                guard.read(client)
                last_nlv, last_sample = value, now["tick"]
                journal.log("account", tick=last_sample, nlv=value,
                            observed=len(observed), qualified=len(qualified), completed=len(completed))
                print(f"Tick {last_sample}/300 | oferty {len(observed)} | "
                      f"kandydaci {len(qualified)} | zamkniete {len(completed)} | NLV {value}", flush=True)
            # Wyceniaj wszystkie oferty takze po cutoff; analyze zapisze powod blokady.
            for tender in tenders:
                tid = tender["tender_id"]
                if tender.get("period") != guard.period or tid in completed:
                    continue
                if tender.get("ticker") not in TICKERS:
                    journal.log("tender_unsupported", tender_id=tid)
                    continue
                result, market, fresh = assess(client, guard, tender, cutoff, journal)
                if result["reasons"]:
                    signature = tuple(result["reasons"])
                    key = (tid, signature)
                    if key not in displayed:
                        displayed.add(key)
                        print(f"POMIN {tid}: " + "; ".join(result["reasons"]), flush=True)
                    continue
                if tid not in qualified:
                    qualified.add(tid)
                    journal.log("tender_qualified", tick=fresh["tick"], analysis=result)
                if not live:
                    if (tid, "preview") not in displayed:
                        displayed.add((tid, "preview"))
                        show(result)
                    continue
                outcome = execute(client, guard, tender, cutoff, journal)
                if outcome is not None:
                    completed.add(tid)
                    last_nlv = outcome["nlv"]
                    if outcome["actual_profit"] is None:
                        prices_complete = False
                    else:
                        actual_total += outcome["actual_profit"]
                    break
            if guard.last_tick >= guard.length - 1:
                require_flat(client)
                value = read_nlv(client)
                guard.read(client)
                last_nlv = value
                normal_end = True
                break
            time.sleep(POLL_INTERVAL)
    except RoundEnded:
        if TRADE_LOCK.exists():
            raise RuntimeError("Koniec rundy z blokada transakcji. Sprawdz RIT.")
        normal_end = True
    except Exception as error:
        journal.log("error", message=str(error))
        raise
    finally:
        actual = actual_total if prices_complete else None
        journal.log("session_summary", normal_end=normal_end,
                    observed=len(observed), qualified=len(qualified), completed=len(completed),
                    last_recorded_nlv=last_nlv, nlv_change=difference(last_nlv, baseline),
                    actual_profit=actual, partial_actual_profit=actual_total)
        print(f"PODSUMOWANIE | oferty {len(observed)} | kandydaci {len(qualified)} | "
              f"zamkniete {len(completed)} | NLV {last_nlv} | "
              f"wynik z wykonan {actual} | {journal.path.name}", flush=True)
        print("NLV moze byc przed ostatecznym rozliczeniem; wynik z wykonan nie zawiera dodatkowych kar.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true", help="wlacz handel w symulacji")
    parser.add_argument("--once", action="store_true", help="tylko jedna runda")
    parser.add_argument("--cutoff", type=int, default=20, help="ticki bez nowych transakcji na koncu")
    args = parser.parse_args()
    if not 20 <= args.cutoff < 300:
        parser.error("cutoff musi byc od 20 do 299")
    key = os.getenv("RIT_API_KEY", "").strip()
    if not key:
        print("Ustaw RIT_API_KEY w terminalu.")
        return 1
    client = RITClient(key, os.getenv("RIT_BASE_URL", "http://localhost:9999/v1"))
    owns_lock = False
    try:
        with PROCESS_LOCK.open("x", encoding="utf-8") as file:
            file.write(str(os.getpid()))
        owns_lock = True
        while True:
            run(client, args.live, args.cutoff)
            if args.once:
                break
            print("Czekam na nastepna runde. Ctrl+C konczy program.", flush=True)
            time.sleep(1)
    except KeyboardInterrupt:
        print("Zatrzymano. Sprawdz pozycje i zlecenia w RIT.")
    except Exception as error:
        print(f"STOP: {error}")
        return 1
    finally:
        client.close()
        if owns_lock:
            PROCESS_LOCK.unlink(missing_ok=True)
        if TRADE_LOCK.exists():
            print("Pozostal active_trade.json. Sprawdz Position i Open Orders; moze byc potrzebne reczne zamkniecie.")
    return 0


if __name__ == "__main__":
    sys.exit(main())


