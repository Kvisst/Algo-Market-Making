# Algo Market Making

Zbiór skryptów Pythona do pracy z lokalnym REST API klienta RIT (Rotman Interactive Trader) podczas scenariusza Market Making. Repozytorium zawiera narzędzia diagnostyczne, rejestrację danych, benchmarki, model badawczy oraz eksperymentalną strategię zawierającą zlecenia limitowe.

> [!WARNING]
> Skrypty handlowe mogą składać prawdziwe zlecenia w praktycznym środowisku RIT. Używaj ich tylko na dedykowanym koncie treningowym i sprawdzaj konfigurację scenariusza. Domyślne wartości nie zostały skwantowane ani potwierdzono ich rentowności.

## Zawartość projektu

| Plik | Opis |
| --- | --- |
| `main.py` | Diagnostyka API — pobiera dane o rundzie, akcjach, limitach i otwartych zleceniach przez `GET`. |
| `benchmark.py` | Mierzy czas odpowiedzi pojedynczych zapytań i przykładowego cyklu analitycznego. |
| `record_rit.py` | Zapisuje read-only odpowiedzi API do pliku JSON Lines; nie wysyła zleceń. |
| `ritc_model.py` | Bezpieczny model badawczy kotowania, bez połączenia z API ani składania zleceń. |
| `strategy.py` | Eksperymentalny bot Market Making dla SPNG, SMMR, ATMN i WNTR z obsługą limitów i ryzyka. |
| `strategy_4.py`, `new.py`, `tender.py`, `tender_v2.py`, `wlacz_ciagly.py` | Warianty lub eksperymentalne skrypty scenariuszowe. Są to osobne implementacje i powinny być dokładnie sprawdzane przed uruchomieniem. |
| `tender_analyzer.py` | Narzędzie do analizy danych lub tenderów. |

## Wymagania

- Python 3.10 lub nowszy
- Uruchomiony klient RIT
- Włączony Client REST API
- Klucz API RIT
- Windows dla pełnego zgodnego wykorzystania skryptów RIT

Większość skryptów standardowych działa bez dodatkowych pakietów. `main.py` i `benchmark.py` wymagają biblioteki `requests`:

```bash
python -m pip install requests
```

## Konfiguracja

Domyślnie skrypty używają adresu:

```text
http://localhost:9999/v1
```

Inny adres można podać przez zmienną środowiskową:

```bash
set RIT_API_KEY=twój_klucz
set RIT_BASE_URL=http://localhost:9999/v1
```

Można również pozostawić zmienną `RIT_API_KEY` nieustawioną — wiele skryptów poprosi o klucz w ukrytym wejściu terminala.

Nie zapisuj klucza API bezpośrednio w kodzie i nie commituj plików `.env`.

## Użycie

### Diagnostyka API

Jednorazowy odczyt:

```bash
python main.py
```

Podgląd aktualnego stanu co 2 sekundy:

```bash
python main.py --watch
```

Skrypt wykonuje wyłącznie zapytania `GET` i nie składa zleceń.

### Benchmark API

```bash
python benchmark.py
```

Benchmark również wykonuje wyłącznie odczyty i mierzy opóźnienia REST API.

### Rejestracja danych

```bash
python record_rit.py --seconds 420 --interval 1 --depth 100 --tape-limit 1000
```

Domyślnie dane trafiają do katalogu `recordings/` jako plik JSON Lines. Można wskazać własną ścieżkę:

```bash
python record_rit.py --output recordings/session.jsonl --seconds 300
```

Rejestrator nie zmienia pozycji ani zleceń i używa wyłącznie żądań `GET`.

### Model badawczy

Uruchom samotest:

```bash
python ritc_model.py --self-test
```

Uruchom demonstrację generowania quote'ów:

```bash
python ritc_model.py --demo
```

Model nie zawiera adaptera API i nie wysyła zleceń. Wszystkie domyślne parametry są poglądowe i nie są skalibrowane do konkretnego heat.

### Eksperymentalny bot

Standardowy bot rozpoczyna działanie dopiero po podaniu flagi `--live`:

```bash
python strategy.py --live
```

Opcjonalne parametry:

```bash
python strategy.py --live --size 100 --stock-cap 500 --gross-cap 2000 --loss-limit 200
```

Bot:

- obsługuje SPNG, SMMR, ATMN i WNTR,
- składa tylko zlecenia `LIMIT`,
- wymaga osobnego konta bez zewnętrznych zleceń,
- monitoruje pozycje i limit exposure,
- zapisuje zdarzenia do katalogu `live_logs/`,
- próbuje potwierdzić anulowanie i wykonać zamknięcie pozycji przy wyjściu.

Nie uruchamiaj wersji live bez wcześniejszej weryfikacji samego scenariusza, limitów i ręcznego sprawdzenia statusu otwartych zleceń.

## Dobre praktyki

1. Najpierw uruchom `main.py` i sprawdź, czy API działa.
2. Następnie wykonaj `benchmark.py` albo `record_rit.py`.
3. Przetestuj logikę przez `ritc_model.py --self-test` i `--demo`.
4. Na ostatnim etapie uruchom wariant strategii na dedykowanym koncie praktycznym.
5. Nadzoruj proces i sprawdzaj pozycje oraz logi po każdym zagrożonym orderze lub błędzie API.

## Bezpieczeństwo API i zlecenia

- Klucz nie powinien być częścią URL ani logów.
- Zwróć uwagę na ewentualne ograniczenia limitu zapytań (`HTTP 429`).
- Nie powtarzaj automatycznie wysłanego zlecenia, jeśli odpowiedź API została utracona — najpierw sprawdź historię zleceń.
- Rezerwuj miejsce na odchylenia z rynku oraz natychmiastowe fill'e.
- Nie zakładaj, że skrypty są rentowne lub bezpieczne na każdym scenariuszu RIT.

## Pliki wygenerowane podczas pracy

- `.env` — ewentualne lokalne zmienne środowiskowe
- `.venv/`, `__pycache__/` — środowisko i cache Python
- `recordings/` — nagrania odpowiedzi API
- `live_logs/` — dzienniki działającego bota
- `*.py[cod]`, `*.backup_*` — pliki ignorowane przez Git

## Licencja

Repozytorium nie zawiera obecnie pliku licencji. Przed publicznym lub komercyjnym użyciem kodu warto dodać odpowiednią licencję i uzgodnić warunki korzystania.
