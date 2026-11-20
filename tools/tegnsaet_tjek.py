"""Hvordan kommer danske bogstaver ud af databasen? (20-11-2026)

997_udvalg_kilde.sql fejlede på Windows med UnicodeDecodeError på "Åben"
(byte 0xC5 = "Å" i Windows-1252), også efter CAST til NVARCHAR. Forbindelsen
i db.py læser både VARCHAR og NVARCHAR som UTF-8.

Årsagen var forbindelsens tegnsæt (rettet i db.indstil_tegnsaet). Værktøjet
bruger db.py's forbindelse og læser det samme ord på seks måder: værdien,
eller fejlen. "Ã…ben" betyder data gemt forvansket FØR rettelsen.
Ingen persondata: kun statusværdien "Åben" og konstanter.

    uv run python tools\\tegnsaet_tjek.py
"""

from __future__ import annotations

import locale
import sys

import pyodbc

from konvertering.cli import _indlaes_env_fil
from konvertering.config import Settings
from konvertering.db import Database

FORESPOERGSLER = [
    ("1 konstant NVARCHAR", "SELECT N'Åben'"),
    ("2 konstant VARCHAR", "SELECT CAST(N'Åben' AS VARCHAR(10))"),
    ("3 stg.sag (NVARCHAR)", "SELECT TOP 1 status_go FROM stg.sag WHERE status_go LIKE N'%ben'"),
    ("4 map.kodeliste", "SELECT TOP 1 go_vaerdi FROM map.kodeliste "
                        "WHERE liste = 'sagsstatus' AND go_vaerdi LIKE N'%ben'"),
    ("5 udvalg.Cases raa", "SELECT Sagsstatus FROM udvalg.Cases"),
    ("6 udvalg.Cases CAST", "SELECT CAST(Sagsstatus AS NVARCHAR(256)) FROM udvalg.Cases"),
]


def main() -> int:
    print(_indlaes_env_fil())
    print(f"Python {sys.version.split()[0]}, pyodbc {pyodbc.version}, "
          f"locale {locale.getpreferredencoding(False)}")
    db = Database(Settings.fra_environment().db)
    forbindelse = db._connect()
    print(f"Driver: {forbindelse.getinfo(pyodbc.SQL_DRIVER_NAME)} "
          f"{forbindelse.getinfo(pyodbc.SQL_DRIVER_VER)}")
    for navn, sql in FORESPOERGSLER:
        try:
            raekke = forbindelse.cursor().execute(sql).fetchone()
            vaerdi = None if raekke is None else raekke[0]
            print(f"{navn:24} OK    {vaerdi!r}")
        except Exception as e:  # noqa: BLE001 — vi vil se ALLE fejl
            print(f"{navn:24} FEJL  {type(e).__name__}: {str(e)[:80]}")
    forbindelse.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
