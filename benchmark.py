import math
import os
import statistics
import time

import requests


BASE_URL = os.getenv(
    "RIT_BASE_URL", "http://localhost:9999/v1"
).rstrip("/")

session = requests.Session()
session.trust_env = False


def get(endpoint, **params):
    response = session.get(
        f"{BASE_URL}/{endpoint}",
        params=params,
        timeout=(3, 5),
    )
    if response.status_code == 429:
        raise RuntimeError(
            "HTTP 429: osiągnięto limit zapytań. "
            "Zatrzymujemy test."
        )
    response.raise_for_status()
    return response.json()


def report(name, samples):
    ordered = sorted(samples)
    p95 = ordered[math.ceil(0.95 * len(ordered)) - 1]

    print(f"\n{name} — {len(samples)} pomiarów")
    print(f"Mediana: {statistics.median(samples):.1f} ms")
    print(f"95% pomiarów nie przekroczyło: {p95:.1f} ms")
    print(f"Najwolniejszy: {max(samples):.1f} ms")


def measure(name, operation, count, pause):
    samples = []
    for _ in range(count):
        start = time.perf_counter()
        operation()
        samples.append((time.perf_counter() - start) * 1000)
        time.sleep(pause)
    report(name, samples)


def analysis_cycle():
    get("case")
    get("securities")
    get("limits")
    get("orders", status="OPEN")
    tenders = get("tenders")

    # Pobieramy jeden arkusz również wtedy, gdy nie ma tenderów.
    ticker = tenders[0]["ticker"] if tenders else "GRN"
    get("securities/book", ticker=ticker, limit=1000)
    get("case")


def main():
    key = os.getenv("RIT_API_KEY", "").strip()
    if not key:
        print("Brakuje RIT_API_KEY. Ustaw klucz w terminalu.")
        return

    session.headers.update({"X-API-Key": key})

    try:
        print("Test odczytu API. Nie składa zleceń.")
        get("case")  # Rozgrzewka połączenia, poza pomiarem.

        measure(
            "Pojedynczy odczyt /case",
            lambda: get("case"),
            count=20,
            pause=0.25,
        )

        measure(
            "Cykl analizy — 7 kolejnych odczytów API",
            analysis_cycle,
            count=5,
            pause=1.0,
        )

    except (requests.RequestException, RuntimeError,
            KeyError, IndexError, TypeError, ValueError) as error:
        print(f"\nTest przerwany: {error}")
    except KeyboardInterrupt:
        print("\nZatrzymano.")
    finally:
        session.close()


if __name__ == "__main__":
    main()