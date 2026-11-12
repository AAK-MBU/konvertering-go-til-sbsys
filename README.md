# GO → SBSYS konvertering

Teknisk fundament for konvertering af sager, dokumenter, parter, journalnotater og indsatser
fra GetOrganized til SBSYS.

Kilde: **stagingdatabase** (fem tabeller: Cases, Documents,
Contacts, Journalnotes, Foranstaltningsdata) + dokumentfiler på et netværksdrev
på samme server.

## Bærende princip

**Idempotent og genstartbar.** Enhver kørsel kan afbrydes hvor som helst og
startes igen uden dubletter i SBSYS og uden manuel oprydning.

## Pipeline

```
NC-stagingdb + netværksdrev → stg.* → loaders (Python) → SBSYS API
                                 ↑                          │
                          map.kodeliste              konv.id_map
                        (ejes af forretningen)     (idempotens-garantien)
```

## Repoets opbygning

```
sql/        DDL, udtræk, rettigheder og syntetiske testdata. 100_extract_skabelon.sql er den ENESTE fil
            der kender kildens struktur. Skemaet er kendt siden 16-10,
            så den kan skrives færdig uden at vente på data.
mapping/    Kodelister som CSV. Ejes af forretningen, ikke af udviklerne.
sbsys/      endpoints.toml — SBSYS' stier og feltnavne. Uden for koden,
            så en sti kan rettes uden en ny release.
src/konvertering/
  config.py    miljøafhængige indstillinger, fail-fast validering
  models.py    den kanoniske model (pydantic)
  idmap.py     idempotensen
  mapping.py   kodelisteopslag
  files.py     fileshare-opslag med de tre fallback-strategier
  sbsys/       API-klient: retry, throttling, korrelations-id, kaldelog
  loaders/     én loader pr. objekttype, ~40 linjer hver
  boelge.py    de ti bølger: validering, gate, fail-fast på tom bølge
  cli.py       samme kode kører manuelt og fra Automation Server
automation/  Automation Server-indgang (workqueue-mønster)
tools/       falsk_sbsys.py — attrap til at køre hele kæden lokalt
tests/       71 tests, ingen netværk, ingen database
```

## Kom i gang

```bash
uv sync --extra dev                                    # dev henter pytest/ruff med
cp .env.example .env                                   # udfyld
cp sbsys/endpoints.example.toml sbsys/endpoints.toml   # ret efter jeres API-doc
konv db init
konv selvtest                  # kontrollerer database, SBSYS og fileshare
konv boelge list               # de ti bølger
konv load sag --boelge ppr     # dry-run: validerer alt, sender intet
```

`--live` er det eneste der sender til SBSYS. Uden det er alt en tørkørsel.

En bølgekørsel stopper før den starter hvis bølgekoden er ukendt eller tom —
en stavefejl må ikke kunne give en grøn rapport over nul behandlede sager.

```bash
konv boelge gate --boelge ppr  # returkode 0 = klar til undervisning
uv run pytest                  # 71 tests, hverken database eller SBSYS
```
