"""AZ-ident -> SBSYS-bruger-id. Delt af sager (sagsbehandler) og dokumenter
(registreret af), så de slår op på præcis samme måde."""

from __future__ import annotations

import re
from typing import Any

# Samme mønster som sagsbehandler-rensningen: "az" + 5 tegn, som eget ord.
_AZ = re.compile(r"(?<![a-z0-9])(az[a-z0-9]{5})(?![a-z0-9])", re.I)


def find_az(vaerdi: str | None) -> str | None:
    """Første AZ-ident i en GO-værdi, ellers None (fx "Systemkonto (Inaktiv)")."""
    m = _AZ.search(vaerdi or "")
    return m.group(1).casefold() if m else None


def logon(vaerdi: str) -> str:
    """``AAK\\AZ12345`` / ``az12345@aarhus.dk`` / ``az12345`` -> ``az12345``."""
    return vaerdi.strip().split("\\")[-1].split("@")[0].casefold()


class BrugerOpslag:
    """Søger brugeren i SBSYS på LogonId. Kun ét PRÆCIST træf tæller —
    søger SBSYS bredere end på identen, filtreres det fra her. Svaret
    huskes for kørslen, også et "ikke fundet"."""

    def __init__(self, k: Any) -> None:
        self.k = k
        self._cache: dict[str, int | None] = {}

    def bruger_id(self, az: str) -> int | None:
        noegle = logon(az)
        if noegle in self._cache:
            return self._cache[noegle]
        op = self.k.endpoints.operation("soeg_bruger")
        svar = self.k.klient.kald(
            op.metode, op.sti, operation="soeg_bruger",
            json={"LogonId": az.strip()}, forventede_status=(200, 204, 404),
        )
        traef = [t for t in self.k.endpoints.soegningstraef(svar.krop)
                 if isinstance(t, dict) and logon(str(t.get("LogonId") or "")) == noegle]
        bruger_id = next((traef[0][n] for n in ("Id", "id") if traef[0].get(n) is not None),
                         None) if len(traef) == 1 else None
        self._cache[noegle] = int(bruger_id) if bruger_id else None
        return self._cache[noegle]
