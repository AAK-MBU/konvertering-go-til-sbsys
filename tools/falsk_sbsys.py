"""Falsk SBSYS — en attrap du kan køre lokalt.

Formålet er at kunne se hele kæden virke uden et rigtigt SBSYS: payloads
bliver bygget, kald bliver sendt, id'er kommer retur, og konv.id_map bliver
fyldt. Det er den hurtigste måde at få tillid til at maskineriet hænger
sammen, før der overhovedet findes et testmiljø.

    python3 tools/falsk_sbsys.py          # lytter på 8080

I en anden terminal:

    export SBSYS_BASE_URL=http://localhost:8080
    export SBSYS_TOKEN=attrap
    konv load sag --boelge ppr --live

Serveren svarer 201 med et løbenummer på alt hvad den får, og skriver hvert
kald til skærmen. Den validerer ingenting — den beviser at vores side virker,
ikke at SBSYS gør.

Med --fejlprocent kan du fremprovokere 500-fejl og se at retry-logikken
arbejder, og at objekter der fejler alligevel ender korrekt i id_map.
"""

from __future__ import annotations

import argparse
import json
import random
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs

_taeller = {"id": 1000}
_fejlprocent = 0

# Det fiktive CPR fra sql/999_testdata.sql ($(cpr2|0202022345)), som
# loaderen sender med bindestreg. Attrappen "kender" ikke denne borger.
UKENDT_CPR = "020202-2345"


class Attrap(BaseHTTPRequestHandler):
    def _svar(self, kode: int, krop: object = None) -> None:
        # Bevidst "is None" og ikke "or {}": en tom liste er et gyldigt svar
        # på en søgning, og den skal sendes som [] — ikke som {}.
        data = json.dumps({} if krop is None else krop).encode("utf-8")
        self.send_response(kode)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:  # noqa: N802
        if self.path.startswith("/api/version"):
            return self._svar(200, {"version": "attrap-1.0"})
        # Søgning: svar altid "ikke fundet", så loaderen opretter
        self._svar(200, [])

    def do_POST(self) -> None:  # noqa: N802
        laengde = int(self.headers.get("Content-Length", 0))
        krop_raa = self.rfile.read(laengde)

        # Token-endpoint, så hele password-grant-kæden kan afprøves lokalt
        # længe før servicekontoen findes. Den kvitterer med hvad den fik —
        # grant_type og brugernavn — så man kan se at formularen er rigtig.
        if "token" in self.path:
            form = parse_qs(krop_raa.decode("utf-8", "replace"))
            grant = (form.get("grant_type") or ["?"])[0]
            bruger = (form.get("username") or ["-"])[0]
            klient = (form.get("client_id") or ["-"])[0]
            print(f"  200  {self.path}  grant_type={grant} "
                  f"client_id={klient} username={bruger}")
            if grant == "password" and not bruger.strip():
                print("       (password grant uden username — SBSYS ville svare 400)")
            return self._svar(200, {"access_token": "attrap-token", "expires_in": 3600})

        # Soegninger er POST i SBSYS og skal svare 200 med et resultat — ikke
        # 201 som en oprettelse. Svarede attrappen 201, afviste klienten svaret
        # (soegningerne forventer 200/204/404), og sagsindlaesningen stoppede
        # ved foerste sag, fordi partopslaget er foerste kald.
        # Uden query-strengen: sagssoegningen sender ?InkluderParterIResultat=true,
        # og saa ender stien ikke laengere paa /search.
        if self.path.split("?", 1)[0].rstrip("/").endswith("/search"):
            return self._soegning(krop_raa)

        if _fejlprocent and random.randint(1, 100) <= _fejlprocent:
            print(f"  500  {self.path}  (fremprovokeret)")
            return self._svar(500)

        _taeller["id"] += 1
        nyt_id = _taeller["id"]
        korrelation = self.headers.get("X-Correlation-Id", "-")
        print(f"  201  {self.path}  id={nyt_id}  {korrelation}")
        self._svar(201, {"Id": nyt_id, "Nummer": f"ATTRAP-{nyt_id}"})

    do_PATCH = do_POST

    def do_PUT(self) -> None:  # noqa: N802
        """Opdateringer, fx status efter oprettelsen: PUT /api/sag/{id}/status.

        Uden denne svarede attrappen 501, og hver sag fik en advarsel om at
        statussen ikke blev sat. Svarer 200 og viser hvad der blev sendt.
        """
        laengde = int(self.headers.get("Content-Length", 0))
        krop = self.rfile.read(laengde).decode("utf-8", "replace")
        print(f"  200  PUT {self.path}  {krop[:120]}")
        self._svar(200, {})

    def _soegning(self, krop_raa: bytes) -> None:
        """Personsoegning finder parten; alle andre soegninger finder intet.

        Personen findes altid — undtagen CPR'et UKENDT_CPR, der er det
        fiktive nummer TEST-PART-002 faar i sql/999_testdata.sql. Saa viser
        samme koersel baade en sag der oprettes, og en der afvises med
        'part_ikke_i_sbsys'.

        Sags-, kladde- og indsatssoegninger svarer tomt, saa loaderen tror
        objektet ikke findes i forvejen og opretter det.
        """
        try:
            kriterier = json.loads(krop_raa or b"{}")
        except ValueError:
            kriterier = {}
        if "/person/" in self.path or "/firma/" in self.path:
            nummer = str(kriterier.get("CprNummer") or kriterier.get("CvrNummer") or "")
            if nummer == UKENDT_CPR:
                print(f"  200  {self.path}  (ingen part fundet — som et ukendt CPR i SBSYS)")
                return self._svar(200, {"Results": []})
            # Stabilt PartId pr. nummer, saa samme borger faar samme id hver gang
            part_id = 7000 + sum(map(ord, nummer)) % 1000
            print(f"  200  {self.path}  PartId={part_id}")
            return self._svar(200, {"Results": [{"Id": part_id}]})
        print(f"  200  {self.path}  (intet fundet)")
        return self._svar(200, {"Results": []})

    def log_message(self, *_: object) -> None:
        """Stilhed — vi printer selv det vi vil se."""


def main() -> None:
    global _fejlprocent
    p = argparse.ArgumentParser("falsk-sbsys")
    p.add_argument("--port", type=int, default=8080,
                   help="0 lader styresystemet vælge en ledig port")
    p.add_argument("--fejlprocent", type=int, default=0,
                   help="Andel af kald der skal svare 500, til at afprøve retry")
    args = p.parse_args()
    _fejlprocent = args.fejlprocent

    try:
        server = HTTPServer(("127.0.0.1", args.port), Attrap)
    except OSError as e:
        print(f"Kunne ikke lytte på port {args.port}: {e}\n")
        # WinError 10013 er sjældent "porten er i brug" (det er 10048).
        # Oftest har Windows reserveret portområdet til Hyper-V, WSL eller
        # Docker, og så kan man ikke binde i det uanset rettigheder.
        if getattr(e, "winerror", None) == 10013:
            print("Windows har formentlig reserveret portområdet. Se hvilke:")
            print("  netsh interface ipv4 show excludedportrange protocol=tcp")
        print("Prøv en anden port:")
        print("  uv run python tools/falsk_sbsys.py --port 8099")
        print("Husk at rette SBSYS_BASE_URL i .env tilsvarende.")
        raise SystemExit(1) from None

    port = server.server_address[1]
    print(f"Falsk SBSYS lytter på http://localhost:{port}")
    print(f"Sæt i .env:  SBSYS_BASE_URL=http://localhost:{port}")
    print("Attrappen svarer også på token-endpointet, så password grant kan")
    print("afprøves uden en rigtig servicekonto:")
    print(f"  SBSYS_TOKEN_URL=http://localhost:{port}/connect/token")
    print("  SBSYS_CLIENT_ID=attrap-klient")
    print("  SBSYS_USERNAME=attrap-bruger")
    print("  SBSYS_PASSWORD=hvadsomhelst")
    print("  (og SBSYS_TOKEN udkommenteret — et statisk token vinder over alt)")
    if _fejlprocent:
        print(f"Fremprovokerer 500-fejl på {_fejlprocent} % af kaldene")
    print("Ctrl-C for at stoppe.\n")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStoppet.")


if __name__ == "__main__":
    main()
