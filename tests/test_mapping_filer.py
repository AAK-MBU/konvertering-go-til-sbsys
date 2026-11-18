"""Hver kodeliste koden slår op i, skal have en fil.

`konv mapping load` indlæser hver CSV i mapping/ og bruger filnavnet som
listenavn. Mangler filen, findes listen aldrig i `map.kodeliste`, og hver
eneste opslag i den falder som "manglende mapning" — uden at nogen kan se
HVOR den skulle have stået. Det er en irriterende fejl at jage, og den
opstår hver gang en ny objekttype kommer til.

Testen holder de to sider sammen: koden og mappen.
"""

from __future__ import annotations

import csv
import re
from pathlib import Path

import pytest

MAPPE = Path("mapping")
KOLONNER = [
    "liste", "go_vaerdi", "go_tekst", "sbsys_vaerdi", "sbsys_tekst",
    "handling", "kommentar", "godkendt_af", "godkendt_dato",
]


def _lister_i_koden() -> set[str]:
    """Navnene koden faktisk slår op på. Også når kaldet er delt over
    flere linjer — det er netop dér man glemmer at oprette filen."""
    kilde = "\n".join(p.read_text(encoding="utf-8") for p in Path("src").rglob("*.py"))
    # Også lister der slås op via en konstant: FELT_LISTE = "tilbudsfelt"
    return (set(re.findall(r'slaa_op\(\s*"(\w+)"', kilde))
            | set(re.findall(r'^\w*_LISTE\s*=\s*"(\w+)"', kilde, re.MULTILINE)))


def _lister_i_sql() -> set[str]:
    """Lister der slås op i SQL — bølgetildelingen i extract-laget."""
    kilde = "\n".join(p.read_text(encoding="utf-8") for p in Path("sql").glob("*.sql"))
    return set(re.findall(r"fn_slaa_op\(\s*'(\w+)'", kilde))


def test_der_findes_en_fil_til_hver_sql_liste():
    """Bølgetildelingen fejler tavst hvis listen ikke findes: fn_slaa_op
    returnerer NULL, sagen får ingen bølge, og den opdages først når nogen
    undrer sig over at en skole mangler."""
    mangler = sorted(n for n in _lister_i_sql() if not (MAPPE / f"{n}.csv").exists())
    assert not mangler, f"SQL slår op i lister uden en fil: {mangler}"


def test_der_findes_en_fil_til_hver_liste():
    mangler = sorted(n for n in _lister_i_koden() if not (MAPPE / f"{n}.csv").exists())
    assert not mangler, (
        f"koden slår op i lister uden en fil: {mangler}. "
        f"Opret mapping/<navn>.csv med samme kolonner som de øvrige."
    )


def test_ingen_filer_uden_et_opslag():
    """Den anden vej: en fil ingen slår op i, er død vægt — og den ligner
    noget der virker."""
    # Bølgelisterne slås ikke op i Python. De bruges i extract-laget
    # (map.fn_slaa_op i sql/100_extract_skabelon.sql), hvor bølgen
    # tildeles: 'boelge' oversætter Cases.Organisering (PPR/ELM/BOR/EMN),
    # og 'boelge_enhed' deler ELM op i sine seks bølger.
    kendte = _lister_i_koden() | _lister_i_sql() | {"boelge"}
    overflødige = sorted(
        p.stem for p in MAPPE.glob("*.csv") if p.stem not in kendte
    )
    assert not overflødige, f"filer ingen slår op i: {overflødige}"


@pytest.mark.parametrize("sti", sorted(MAPPE.glob("*.csv")), ids=lambda p: p.stem)
def test_kolonnerne_er_de_rigtige(sti: Path):
    with sti.open(encoding="utf-8-sig", newline="") as f:
        hoved = next(csv.reader(f, delimiter=";"), [])
    assert hoved == KOLONNER, f"{sti.name} har forkerte kolonner"


@pytest.mark.parametrize("sti", sorted(MAPPE.glob("*.csv")), ids=lambda p: p.stem)
def test_listenavnet_matcher_filnavnet(sti: Path):
    """Filnavnet BLIVER listenavnet ved indlæsning. Står der noget andet i
    liste-kolonnen, er det misvisende for den der læser filen."""
    with sti.open(encoding="utf-8-sig", newline="") as f:
        for raekke in csv.DictReader(f, delimiter=";"):
            assert raekke["liste"] == sti.stem, (
                f"{sti.name}: liste={raekke['liste']!r}, men filen hedder {sti.stem}"
            )


@pytest.mark.parametrize("sti", sorted(MAPPE.glob("*.csv")), ids=lambda p: p.stem)
def test_hver_raekke_har_lige_mange_felter(sti: Path):
    """Et semikolon i en kommentar flytter resten af rækken én kolonne til
    højre. `mapping load` fejlede 17-11-2026 med "String or binary data
    would be truncated", fordi kommentarteksten landede i godkendt_af."""
    with sti.open(encoding="utf-8-sig", newline="") as f:
        forkerte = [nr for nr, r in enumerate(csv.reader(f, delimiter=";"), 1)
                    if r and len(r) != len(KOLONNER)]
    assert not forkerte, (
        f"{sti.name}: linje {forkerte[:5]} har ikke {len(KOLONNER)} felter — "
        f"står der et semikolon i en tekst?")
