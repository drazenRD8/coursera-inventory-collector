# coursera-inventory-collector
Python tool for extracting Coursera course data via API, with checkpointing, retry logic, and Excel/CSV export.
# Coursera Inventory Collector

Python alat za automatsko prikupljanje podataka o kursevima sa Coursera platforme putem zvaničnog API-ja.

**Verzija:** v1.0.0

## Namena

Alat je napravljen da kvartalno (svaka 3 meseca) ažurira listu kurseva i njihove atribute (naziv, opis, trajanje, jezici). Idealan za održavanje "inventory" tabela.

## Funkcije

- **API pristup**: Koristi zvanični Coursera API (nema scraping, nema blokada).
- **Checkpoint**: Čuva napredak posle svakog URL-a – može da se nastavi sa `--resume`.
- **Retry + backoff**: Automatski ponavlja zahteve za 429, 500, 502, 503, 504.
- **Progress bar**: Prikazuje napredak u realnom vremenu (`tqdm`).
- **Graceful shutdown**: `Ctrl+C` čuva checkpoint pre izlaza.
- **Excel izlaz**: Tri lista – All Results, Courses, Errors.
- **CSV izlaz**: Za one koji ne koriste Excel.
- **Logging**: Logovi u `coursera_collector.log` i `coursera_errors.log`.

## Instalacija

pip install -r requirements.txt

## Pokretanje

# Osnovno pokretanje (čita urls.txt, čuva u Excel)
python coursera_collector.py

# Sa CSV izlazom
python coursera_collector.py --format csv

# Nastavi iz checkpointa
python coursera_collector.py --resume

# Ponovo obradi sve
python coursera_collector.py --force

# Sporija obrada (za loše konekcije)
python coursera_collector.py --delay 1.0 --retries 5

## Argumenti

| Argument | Opis | Default |
|----------|------|---------|
| `--input` | Fajl sa URL-ovima | `urls.txt` |
| `--output` | Naziv izlaznog fajla | `coursera_inventory` |
| `--format` | `excel` ili `csv` | `excel` |
| `--delay` | Pauza između zahteva (sekunde) | `0.5` |
| `--retries` | Broj automatskih pokušaja | `3` |
| `--timeout` | HTTP timeout (sekunde) | `15` |
| `--resume` | Nastavi iz checkpointa | `False` |
| `--force` | Ponovo obradi sve | `False` |

## Automatsko pokretanje

Alat može da se zakaže da se pokreće automatski svaka 3 meseca putem `cron`-a:

```bash
# Otvori crontab
crontab -e

# Dodaj liniju (pokreće se 1. januara, aprila, jula, oktobra u 6:00)
0 6 1 1,4,7,10 * /usr/bin/python3 /putanja/do/coursera_collector.py >> /putanja/do/log.txt 2>&1