import argparse
import json
import math
import os
import time
from pathlib import Path

import requests

from tender_analyzer import RITClient, TICKERS, analyze, show


ROOT = Path(__file__).resolve().parent
TRADE_LOCK = ROOT / "active_trade.json"
PROCESS_LOCK = ROOT / "bot_running.lock"

POLL_INTERVAL = 0.2
CUTOFF_TICKS = 45
CONFIRM_TIMEOUT = 8.0


class RoundEnded(RuntimeError):
    pass


def integer(value):
    number = float(value)
    if not math.isfinite(number) or not number.is_integer():
        raise RuntimeError(f"Niepoprawna liczba akcji: {value}")
    return int(number)


def securities(client):
    result = {
        row["ticker"]: row
        for row in client.get("securities")
    }
    if not TICKERS.issubset(result):
        raise RuntimeError("W API brakuje BLU, RED lub GRN.")
    return result


def positions(market):
    return {
        ticker: integer(market[ticker]["position"])
        for ticker in TICKERS
    }


def require_flat(client):
    market = securities(client)
    if any(positions(market).values()):
        raise RuntimeError(
            "Pozycje nie są zerowe. Sprawdź je w RIT."
        )
    if client.get("orders", status="OPEN"):
        raise RuntimeError(
            "Są aktywne zlecenia. Sprawdź Open Orders w RIT."
        )
    return market


def read_nlv(client):
    value = client.get("trader").get("nlv")
    if value is None:
        return None
    value = float(value)
    return value if math.isfinite(value) else None


def difference(after, before):
    if after is None or before is None:
        return None
    return after - before


def save_trade(state):
    temporary = TRADE_LOCK.with_suffix(".tmp")
    temporary.write_text(
        json.dumps(state, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    os.replace(temporary, TRADE_LOCK)


def post_once(client, endpoint, **params):
    # Brak automatycznych powtórzeń POST.
    # Serwer może wykonać żądanie mimo utraty odpowiedzi.
    try:
        response = client.session.post(
            f"{client.base_url}/{endpoint}",
            params=params,
            timeout=(3, 5),
        )
        response.raise_for_status()
        return response.json()
    except (requests.RequestException, ValueError) as error:
        raise RuntimeError(
            f"Niepotwierdzony POST /{endpoint}: {error}. "
            "Sprawdź pozycje i zlecenia w RIT. "
            "Żądanie nie będzie ponowione."
        ) from error


class RoundGuard:
    def __init__(self, start):
        self.period = start["period"]
        self.last_tick = start["tick"]
        self.length = start["ticks_per_period"]

    def read(self, client, require_active=False):
        case = client.get("case")

        if (
            case["period"] != self.period
            or case["tick"] < self.last_tick
            or case["status"] == "STOPPED"
        ):
            raise RoundEnded("Runda zakończona lub zresetowana.")

        self.last_tick = case["tick"]

        if require_active and case["status"] != "ACTIVE":
            raise RuntimeError(
                "Runda nieaktywna podczas transakcji. "
                "Sprawdź pozycje i zlecenia w RIT."
            )

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
        f"Nie potwierdzono pozycji {expected}. "
        f"Ostatni odczyt: {actual}. "
        "Sprawdź Position i Open Orders w RIT."
    )


def execute(client, guard, tender, log):
    market = require_flat(client)
    case = guard.read(client, require_active=True)

    if guard.length - case["tick"] <= CUTOFF_TICKS:
        return False

    ticker = tender["ticker"]
    quantity = integer(tender["quantity"])
    max_size = integer(market[ticker]["max_trade_size"])

    if quantity <= 0 or max_size <= 0:
        raise RuntimeError("Niepoprawny rozmiar transakcji.")

    # Ostatnia wycena przed przyjęciem.
    limits = client.get("limits")
    book = client.get(
        "securities/book", ticker=ticker, limit=1000
    )
    case = guard.read(client, require_active=True)

    if guard.length - case["tick"] <= CUTOFF_TICKS:
        return False

    result = analyze(
        tender, market, limits, case, book, []
    )
    if result["reasons"]:
        return False

    show(result)
    before_nlv = read_nlv(client)

    # Nie handlujemy na wycenie sprzed długiego opóźnienia.
    fresh = guard.read(client, require_active=True)
    if (
        fresh["tick"] - case["tick"] > 1
        or guard.length - fresh["tick"] <= CUTOFF_TICKS
        or tender["expires"] - fresh["tick"] < 3
    ):
        return False

    rate = float(
        market[ticker].get("api_orders_per_second", 0) or 0
    )
    gap = max(0.25, 1.05 / rate) if rate > 0 else 0.25

    expected = {name: 0 for name in TICKERS}
    expected[ticker] = (
        quantity if tender["action"] == "BUY" else -quantity
    )

    state = {
        "tender_id": tender["tender_id"],
        "ticker": ticker,
        "phase": "PRZED_PRZYJECIEM",
        "expected_positions": dict(expected),
    }

    # Blokada pozostaje, jeżeli transakcja zostanie przerwana.
    with TRADE_LOCK.open("x", encoding="utf-8") as file:
        json.dump(state, file, indent=2)

    log("trade_start", tender=tender, analysis=result)
    started = time.perf_counter()

    accepted = post_once(
        client,
        f"tenders/{tender['tender_id']}",
        price=tender["price"],
    )
    if not isinstance(accepted, dict) or accepted.get("success") is not True:
        raise RuntimeError(
            f"Nie potwierdzono przyjęcia tendera: {accepted}"
        )

    state["phase"] = "TENDER_PRZYJETY"
    save_trade(state)

    wait_position(client, guard, expected)
    print(
        f"PRZYJĘTO TENDER. {ticker}: {expected[ticker]}",
        flush=True,
    )

    while expected[ticker] != 0:
        guard.read(client, require_active=True)

        actual = positions(securities(client))
        if actual != expected:
            raise RuntimeError(
                f"Nieoczekiwana pozycja: {actual}; "
                f"oczekiwano {expected}."
            )
        if client.get("orders", status="OPEN"):
            raise RuntimeError("Wykryto aktywne zlecenia.")

        amount = min(abs(expected[ticker]), max_size)
        action = "SELL" if expected[ticker] > 0 else "BUY"
        next_expected = dict(expected)
        next_expected[ticker] += (
            -amount if action == "SELL" else amount
        )

        state.update(
            phase="PRZED_ZLECENIEM",
            action=action,
            quantity=amount,
            expected_positions=next_expected,
        )
        save_trade(state)

        sent_at = time.monotonic()
        order = post_once(
            client,
            "orders",
            ticker=ticker,
            type="MARKET",
            quantity=amount,
            action=action,
        )

        if not isinstance(order, dict) or "order_id" not in order:
            raise RuntimeError(
                f"Nieoczekiwana odpowiedź na zlecenie: {order}"
            )

        state.update(
            phase="ZLECENIE_WYSLANE",
            order_id=order["order_id"],
        )
        save_trade(state)
        log(
            "order_sent",
            ticker=ticker,
            action=action,
            quantity=amount,
            response=order,
        )

        # Sama odpowiedź POST nie wystarcza.
        wait_position(client, guard, next_expected)
        expected = next_expected

        print(
            f"WYKONANO {action} {amount} {ticker}; "
            f"pozycja: {expected[ticker]}",
            flush=True,
        )
        log("position_confirmed", positions=expected)

        if expected[ticker]:
            remaining_pause = gap - (
                time.monotonic() - sent_at
            )
            if remaining_pause > 0:
                time.sleep(remaining_pause)

    require_flat(client)
    guard.read(client, require_active=True)

    # Pozycje i zlecenia są już potwierdzone jako zerowe.
    state["phase"] = "ZAMKNIETO"
    save_trade(state)

    after_nlv = read_nlv(client)
    guard.read(client, require_active=True)
    elapsed = time.perf_counter() - started

    log(
        "trade_completed",
        tender_id=tender["tender_id"],
        seconds=elapsed,
        estimated_profit=result["profit"],
        nlv_before=before_nlv,
        nlv_after=after_nlv,
        nlv_change=difference(after_nlv, before_nlv),
    )

    TRADE_LOCK.unlink()
    print(
        f"GOTOWE: pozycje 0, brak zleceń. "
        f"Czas: {elapsed:.3f} s; "
        f"zmiana NLV: {difference(after_nlv, before_nlv)}",
        flush=True,
    )
    return True


def run(client, live):
    if TRADE_LOCK.exists():
        raise RuntimeError(
            "Istnieje active_trade.json po poprzedniej próbie. "
            "Sprawdź pozycje i aktywne zlecenia w RIT."
        )

    print("HANDEL WŁĄCZONY" if live else "PODGLĄD — BEZ HANDLU")
    require_flat(client)

    print(
        "Czekam na początek rundy: ACTIVE, tick 0–3.",
        flush=True,
    )
    last_message = 0

    while True:
        start = client.get("case")

        if start["status"] == "ACTIVE" and 0 <= start["tick"] <= 3:
            break

        if time.monotonic() - last_message >= 5:
            print(
                f"Czekam... status={start['status']}, "
                f"tick={start['tick']}",
                flush=True,
            )
            last_message = time.monotonic()

        time.sleep(POLL_INTERVAL)

    if (
        start["name"] != "Liquidity Trading"
        or start["ticks_per_period"] != 300
    ):
        raise RuntimeError("Nieoczekiwany case lub długość rundy.")

    guard = RoundGuard(start)
    require_flat(client)
    baseline = read_nlv(client)
    guard.read(client, require_active=True)

    stamp = time.strftime("%Y%m%d_%H%M%S")
    journal = ROOT / f"session_{stamp}_{os.getpid()}.jsonl"

    def log(event, **data):
        with journal.open("a", encoding="utf-8") as file:
            file.write(json.dumps(
                {
                    "time": time.strftime("%Y-%m-%d %H:%M:%S"),
                    "event": event,
                    **data,
                },
                ensure_ascii=False,
            ) + "\n")

    observed = set()
    qualified = set()
    completed = set()

    last_nlv = baseline
    last_sample = -10
    normal_end = False

    def counter_text():
        return (
            f"oferty: {len(observed)} | "
            f"spełniające warunki: {len(qualified)} | "
            f"zamknięte: {len(completed)}"
        )

    log("start", live=live, case=start, nlv=baseline)
    print(f"START RUNDY. Dziennik: {journal.name}", flush=True)

    try:
        while True:
            case = guard.read(client)

            if case["status"] != "ACTIVE":
                time.sleep(0.5)
                continue

            market = require_flat(client)

            # Odczytujemy oferty również w końcówce rundy,
            # gdy nie przyjmujemy już nowych transakcji.
            tenders = client.get("tenders")
            now = guard.read(client)

            for tender in tenders:
                tender_id = tender["tender_id"]

                if (
                    tender.get("period") != guard.period
                    or tender_id in observed
                ):
                    continue

                observed.add(tender_id)
                log(
                    "tender_observed",
                    tick=now["tick"],
                    tender=tender,
                )
                print(
                    f"NOWA OFERTA #{len(observed)} | "
                    f"id={tender_id} | {tender.get('ticker')} | "
                    f"{tender.get('action')} "
                    f"{tender.get('quantity')}",
                    flush=True,
                )

            if now["tick"] - last_sample >= 5:
                value = read_nlv(client)
                guard.read(client)
                last_nlv = value
                last_sample = now["tick"]

                log(
                    "account",
                    tick=now["tick"],
                    nlv=value,
                    observed=len(observed),
                    qualified=len(qualified),
                    completed=len(completed),
                )
                print(
                    f"Tick {now['tick']}/300 | "
                    f"{counter_text()} | NLV: {value}",
                    flush=True,
                )

            if now["tick"] >= guard.length - 1:
                normal_end = True
                break

            if guard.length - now["tick"] <= CUTOFF_TICKS:
                time.sleep(POLL_INTERVAL)
                continue

            for tender in tenders:
                tender_id = tender["tender_id"]

                if (
                    tender.get("period") != guard.period
                    or tender.get("ticker") not in TICKERS
                    or tender_id in completed
                ):
                    continue

                # W podglądzie nie powtarzamy już pokazanej oferty.
                # W live ponawiamy ocenę, jeżeli execute jej nie przyjął.
                if not live and tender_id in qualified:
                    continue

                market = require_flat(client)
                limits = client.get("limits")
                book = client.get(
                    "securities/book",
                    ticker=tender["ticker"],
                    limit=1000,
                )
                fresh = guard.read(client)

                if guard.length - fresh["tick"] <= CUTOFF_TICKS:
                    break

                result = analyze(
                    tender, market, limits, fresh, book, []
                )
                if result["reasons"]:
                    continue

                if tender_id not in qualified:
                    qualified.add(tender_id)
                    log(
                        "tender_qualified",
                        tick=fresh["tick"],
                        analysis=result,
                    )

                if not live:
                    show(result)
                    print("PODGLĄD: nic nie wysłano.")
                    continue

                # execute wykonuje własną ponowną wycenę.
                if execute(client, guard, tender, log):
                    completed.add(tender_id)
                    last_nlv = read_nlv(client)
                    guard.read(client)
                    print(counter_text(), flush=True)
                    break

            time.sleep(POLL_INTERVAL)

    except RoundEnded as error:
        if TRADE_LOCK.exists():
            raise RuntimeError(
                "Runda zakończyła się podczas transakcji. "
                "Sprawdź pozycje i rozliczenie w RIT."
            ) from error

        normal_end = True
        print(error)

    except Exception as error:
        log("error", message=str(error))
        raise

    finally:
        change = difference(last_nlv, baseline)

        log(
            "session_summary",
            normal_end=normal_end,
            observed=len(observed),
            qualified=len(qualified),
            completed=len(completed),
            last_recorded_nlv=last_nlv,
            nlv_change=change,
        )

        print("\nPODSUMOWANIE")
        print(f"Zaobserwowane tendery: {len(observed)}")
        print(f"Spełniające warunki: {len(qualified)}")
        print(f"Zamknięte transakcje: {len(completed)}")
        print(f"Ostatnia odnotowana zmiana NLV: {change}")
        print(f"Dziennik: {journal.name}")
        print("Końcowy P&L i kary sprawdź w aplikacji RIT.")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--live", action="store_true")
    args = parser.parse_args()

    key = os.getenv("RIT_API_KEY", "").strip()
    if not key:
        print("Ustaw RIT_API_KEY w terminalu.")
        return

    client = RITClient(
        key,
        os.getenv("RIT_BASE_URL", "http://localhost:9999/v1"),
    )
    owns_process_lock = False

    try:
        with PROCESS_LOCK.open("x", encoding="utf-8") as file:
            file.write(str(os.getpid()))
        owns_process_lock = True

        while True:
            run(client, args.live)
            print(
                "\nRunda zakończona. Czekam na następną...",
                flush=True,
            )
            time.sleep(1)

    except FileExistsError:
        print(
            "STOP: istnieje blokada programu lub transakcji. "
            "Sprawdź, czy inna kopia programu nadal działa."
        )
    except KeyboardInterrupt:
        print("\nZatrzymano. Sprawdź pozycje i zlecenia w RIT.")
    except Exception as error:
        print(f"\nSTOP: {error}")
    finally:
        client.close()
        if owns_process_lock:
            PROCESS_LOCK.unlink(missing_ok=True)

    if TRADE_LOCK.exists():
        print(
            "Pozostał active_trade.json. "
            "Sprawdź Position i Open Orders w RIT. "
            "Może być konieczne ręczne zamknięcie pozycji."
        )


if __name__ == "__main__":
    main()