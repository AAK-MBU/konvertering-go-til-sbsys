"""Ingen kolonne må hedde det samme som et reserveret T-SQL-ord.

Baggrund: `stg.part` havde en kolonne der hed `by` — dansk for bymæssig
bebyggelse, men også halvdelen af `GROUP BY`. SQL Server svarede
"Incorrect syntax near the keyword 'by'", og fejlen pegede ikke på kolonnen,
men på en tilfældig linje i nærheden.

Den slags findes ellers kun ved at køre mod en rigtig server. Her tager det
et øjeblik og kræver ingen database.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

SQL_MAPPE = Path(__file__).resolve().parents[1] / "sql"

# Microsofts liste over reserverede nøgleord i T-SQL.
RESERVEREDE = {
    "add", "all", "alter", "and", "any", "as", "asc", "authorization", "backup",
    "begin", "between", "break", "browse", "bulk", "by", "cascade", "case",
    "check", "checkpoint", "close", "clustered", "coalesce", "collate",
    "column", "commit", "compute", "constraint", "contains", "containstable",
    "continue", "convert", "create", "cross", "current", "current_date",
    "current_time", "current_timestamp", "current_user", "cursor", "database",
    "dbcc", "deallocate", "declare", "default", "delete", "deny", "desc",
    "disk", "distinct", "distributed", "double", "drop", "dump", "else", "end",
    "errlvl", "escape", "except", "exec", "execute", "exists", "exit",
    "external", "fetch", "file", "fillfactor", "for", "foreign", "freetext",
    "freetexttable", "from", "full", "function", "goto", "grant", "group",
    "having", "holdlock", "identity", "identity_insert", "identitycol", "if",
    "in", "index", "inner", "insert", "intersect", "into", "is", "join", "key",
    "kill", "left", "like", "lineno", "load", "merge", "national", "nocheck",
    "nonclustered", "not", "null", "nullif", "of", "off", "offsets", "on",
    "open", "opendatasource", "openquery", "openrowset", "openxml", "option",
    "or", "order", "outer", "over", "percent", "pivot", "plan", "precision",
    "primary", "print", "proc", "procedure", "public", "raiserror", "read",
    "readtext", "reconfigure", "references", "replication", "restore",
    "restrict", "return", "revert", "revoke", "right", "rollback", "rowcount",
    "rowguidcol", "rule", "save", "schema", "securityaudit", "select",
    "session_user", "set", "setuser", "shutdown", "some", "statistics",
    "system_user", "table", "tablesample", "textsize", "then", "to", "top",
    "tran", "transaction", "trigger", "truncate", "tsequal", "union", "unique",
    "unpivot", "update", "updatetext", "use", "user", "values", "varying",
    "view", "waitfor", "when", "where", "while", "with", "writetext",
}

_BLOK_KOMMENTAR = re.compile(r"/\*.*?\*/", re.DOTALL)
_LINJE_KOMMENTAR = re.compile(r"--[^\n]*")
_TABEL = re.compile(
    r"CREATE TABLE (?P<navn>[a-z]+\.[a-z_]+)\s*\((?P<krop>.*?)^\);",
    re.DOTALL | re.MULTILINE,
)
_IKKE_KOLONNE = ("constraint", "primary", "foreign", "unique", "index", "check")


def _uden_kommentarer(sql: str) -> str:
    return _LINJE_KOMMENTAR.sub("", _BLOK_KOMMENTAR.sub("", sql))


def _kolonnenavne(krop: str) -> list[tuple[str, bool]]:
    """(navn, står_i_klammer) for hver kolonnedefinition."""
    fundne: list[tuple[str, bool]] = []
    for linje in krop.splitlines():
        ren = linje.strip()
        if not ren or ren.startswith(")"):
            continue
        if ren.split(" ")[0].lower() in _IKKE_KOLONNE:
            continue
        m = re.match(r"^(?P<aaben>\[?)(?P<navn>[A-Za-z_][A-Za-z0-9_]*)\]?\s+\S", ren)
        if m:
            fundne.append((m["navn"], bool(m["aaben"])))
    return fundne


def _tabeller() -> list[tuple[str, str, str]]:
    ud = []
    for fil in sorted(SQL_MAPPE.glob("*.sql")):
        for m in _TABEL.finditer(_uden_kommentarer(fil.read_text(encoding="utf-8"))):
            ud.append((fil.name, m["navn"], m["krop"]))
    return ud


TABELLER = _tabeller()


def test_der_findes_tabeller_at_tjekke():
    """Holder regexet i live — en tavs nul-match ville gøre testen værdiløs."""
    assert len(TABELLER) >= 10


@pytest.mark.parametrize(
    "tabel,krop",
    [(t, k) for _, t, k in TABELLER],
    ids=[f"{f}:{t}" for f, t, _ in TABELLER],
)
def test_ingen_reserverede_kolonnenavne(tabel, krop):
    syndere = [
        navn for navn, i_klammer in _kolonnenavne(krop)
        if navn.lower() in RESERVEREDE and not i_klammer
    ]
    assert not syndere, (
        f"{tabel} har kolonner med reserverede navne: {syndere}. "
        f"Omdøb dem — alternativet er [klammer] i hver eneste forespørgsel, "
        f"og det husker ingen."
    )


def test_testen_fanger_en_plantet_fejl():
    """Uden denne kan hele tjekket holde op med at virke uden at nogen ser det."""
    syndere = [n for n, k in _kolonnenavne("    by  NVARCHAR(100) NULL,\n")
               if n.lower() in RESERVEREDE and not k]
    assert syndere == ["by"]


def test_klammer_accepteres():
    assert _kolonnenavne("    [by]  NVARCHAR(100) NULL,\n") == [("by", True)]
