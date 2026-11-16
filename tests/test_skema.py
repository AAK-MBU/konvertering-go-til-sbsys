"""Værn om skemafilerne (sql/0*.sql), som `konv db init` kører i rækkefølge.

`stg.dokument.er_kladde` blev tilføjet med en ALTER der stod FØR
`CREATE TABLE stg.dokument`. På en eksisterende database gjorde det ingen
skade; på en tom database fejlede `db init`, fordi den forsøgte at ændre en
tabel der ikke fandtes endnu.
"""

from __future__ import annotations

import re
from pathlib import Path

SQL = Path(__file__).resolve().parents[1] / "sql"


def _uden_kommentarer(tekst: str) -> str:
    tekst = re.sub(r"/\*.*?\*/", "", tekst, flags=re.DOTALL)
    return re.sub(r"--[^\n]*", "", tekst)


def _alter_foer_create(filer: list[tuple[str, str]]) -> list[str]:
    """ALTER TABLE x ADD der står før CREATE TABLE x, på tværs af filerne i rækkefølge."""
    oprettet: set[str] = set()
    fejl: list[str] = []
    for navn, tekst in filer:
        for m in re.finditer(r"\b(CREATE|ALTER)\s+TABLE\s+([a-z]+\.[a-z_]+)", _uden_kommentarer(tekst),
                             flags=re.IGNORECASE):
            handling, tabel = m.group(1).upper(), m.group(2).lower()
            if handling == "CREATE":
                oprettet.add(tabel)
            elif tabel not in oprettet:
                fejl.append(f"{navn}: ALTER TABLE {tabel} før CREATE TABLE {tabel}")
    return fejl


def test_ingen_alter_foer_create_i_db_init():
    filer = [(p.name, p.read_text(encoding="utf-8")) for p in sorted(SQL.glob("0*.sql"))]
    assert filer, "fandt ingen skemafiler"
    fejl = _alter_foer_create(filer)
    assert not fejl, f"`konv db init` fejler på en tom database: {fejl}"


def test_tjekket_fanger_den_gamle_fejl():
    gammel = ("IF COL_LENGTH('stg.dokument', 'er_kladde') IS NULL\n"
              "    ALTER TABLE stg.dokument ADD er_kladde BIT;\n"
              "IF OBJECT_ID('stg.dokument') IS NULL\nCREATE TABLE stg.dokument (x INT);")
    assert _alter_foer_create([("030.sql", gammel)])
