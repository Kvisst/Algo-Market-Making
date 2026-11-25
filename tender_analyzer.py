"""RITC: kalkulator tenderow. Wykonuje wylacznie zapytania GET.

Instalacja: python -m pip install requests
Start: python tender_analyzer.py
Podglad co 2 sekundy: python tender_analyzer.py --watch
Klucz podaj w ukrytym monicie lub zmiennej srodowiskowej RIT_API_KEY.
Opcjonalna zmienna RIT_BASE_URL: http://localhost:9999/v1
"""

import argparse
import getpass
import json
import os
import sys
import time

import math

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
MIN_TIME_LEFT = 30
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


def analyze(tender, securities, limits, case, book, open_orders):
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
    if case["ticks_per_period"] - case["tick"] < MIN_TIME_LEFT:
        reasons.append("mniej niz 30 tickow do konca rundy")
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


def snapshot(client):
    case = client.get("case")
    securities = {row["ticker"]: row for row in client.get("securities")}
    if not TICKERS.issubset(securities):
        raise RITError("Nie znaleziono BLU, RED i GRN. Sprawdz wybrany case.")
    limits = client.get("limits")
    open_orders = client.get("orders", status="OPEN")
    tenders = client.get("tenders")
    print(f"\n=== {case['name']} | {case['status']} | "
          f"tick {case['tick']}/{case['ticks_per_period']} ===")
    print("Pozycje: " + ", ".join(f"{t}: {securities[t]['position']}" for t in sorted(TICKERS)))
    if not tenders:
        print("Brak tenderow. Czekam na nastepny odczyt.")
    for tender in tenders:
        if tender.get("ticker") not in TICKERS:
            print("Pomijam tender na nieobslugiwany instrument.")
            continue
        try:
            book = client.get("securities/book", ticker=tender["ticker"], limit=1000)
            # Czas pobieramy ponownie, bo odczyty API nie stanowia jednej transakcji.
            fresh_case = client.get("case")
            if fresh_case["period"] != case["period"] or fresh_case["tick"] < case["tick"]:
                print("Runda zmienila sie podczas odczytu. Ponow analize.")
                return
            show(analyze(tender, securities, limits, fresh_case, book, open_orders))
        except (KeyError, ValueError, TypeError, StopIteration) as exc:
            print(f"Brak danych lub nieobslugiwany format: {type(exc).__name__}: {exc}")
            print("TENDER:", json.dumps(tender, ensure_ascii=False))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--watch", action="store_true")
    args = parser.parse_args()
    key = os.environ.get("RIT_API_KEY", "").strip()
    if not key:
        key = getpass.getpass("Klucz API: ").strip()
    if not key:
        print("Nie podano klucza.")
        return 1
    client = RITClient(key, os.environ.get("RIT_BASE_URL", "http://localhost:9999/v1"))
    print("ANALIZA: tylko odczyt. Nie przyjmuje tenderow i nie sklada zlecen.")
    print("Model: natychmiastowe zamkniecie po arkuszu + prowizja market.")
    print("Bufor 0.03 CAD/akcje i prog 500 CAD to zalozenia do testow.")
    print("Arkusz moze sie zmienic. Zysk nie jest gwarantowany; rabatow limit nie doliczamy.")
    try:
        while True:
            snapshot(client)
            if not args.watch:
                break
            time.sleep(0.2)
    except KeyboardInterrupt:
        print("\nZatrzymano.")
    except (RITError, KeyError, ValueError, TypeError) as exc:
        print(f"BLAD: {exc}")
        return 1
    finally:
        client.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())


