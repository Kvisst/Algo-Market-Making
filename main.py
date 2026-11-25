
"""RITC: diagnostyka Client REST API. Wykonuje wylacznie zapytania GET.

Instalacja: python -m pip install requests
Start: python ritc_start.py
Podglad co 2 sekundy: python ritc_start.py --watch
Klucz podaj w ukrytym monicie lub zmiennej srodowiskowej RIT_API_KEY.
Opcjonalna zmienna RIT_BASE_URL: http://localhost:9999/v1
"""

import argparse
import getpass
import json
import os
import sys
import time

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


def pretty(label, data):
    print(f"\n--- {label} ---")
    print(json.dumps(data, indent=2, ensure_ascii=False))


def snapshot(client):
    pretty("RUNDA", client.get("case"))
    securities = client.get("securities")
    if not isinstance(securities, list):
        raise RITError("Nieoczekiwany format /securities: oczekiwano listy.")
    print("\n--- AKCJE ---")
    for security in securities:
        # Zostawiamy nazwy pol API, aby latwo porownac je z dokumentacja.
        fields = (
            "ticker", "position", "bid", "ask", "last", "vwap", "realized",
            "unrealized", "max_trade_size", "trading_fee", "limit_order_rebate",
        )
        print(json.dumps(
            {key: security[key] for key in fields if key in security},
            ensure_ascii=False,
        ))
    # Bledy dodatkowych endpointow nie ukrywaja udanego odczytu rynku.
    for endpoint, params in (
            ("limits", {}),
            ("orders", {"status": "OPEN"}),
            ("tenders", {}),
    ):
        try:
            pretty(endpoint.upper(), client.get(endpoint, **params))
        except RITError as exc:
            print(f"\n{endpoint}: {exc}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--watch", action="store_true", help="odswiezaj co 2 sekundy")
    args = parser.parse_args()
    api_key = os.environ.get("RIT_API_KEY", "").strip()
    if not api_key:
        api_key = getpass.getpass("Klucz API z klienta RIT (ukryty): ").strip()
    if not api_key:
        print("Nie podano klucza API.", file=sys.stderr)
        return 1
    client = RITClient(
        api_key, os.environ.get("RIT_BASE_URL", "http://localhost:9999/v1")
    )
    try:
        print("Tryb odczytu: skrypt nie sklada zlecen ani nie przyjmuje tenderow.")
        while True:
            snapshot(client)
            if not args.watch:
                break
            time.sleep(2)
    except RITError as exc:
        print(f"BLAD: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nZatrzymano.")
    finally:
        client.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())

