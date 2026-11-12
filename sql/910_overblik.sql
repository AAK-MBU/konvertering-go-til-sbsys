/* =====================================================================
   Overblik over konverteringen.

   Kør hele filen i SSMS. Den skriver ingenting — kun SELECT — og giver
   otte resultatsæt i den rækkefølge man plejer at ville have dem:

     1  Kørsler            hvad er kørt, hvornår, mod hvilket SBSYS
     2  Status pr. bølge   hvor langt er vi
     3  Fra → til          hvilken GO-sag blev til hvilken SBSYS-sag
     4  Dokumenter         samme, for dokumenter
     5  Afviste            hvad kom ikke med, og hvorfor
     6  Afvisningsårsager  hvilken regel rammer flest
     7  Manglende mapning  hvad en fagperson skal tage stilling til
     8  Fejlede kald       hvad SBSYS svarede, og hvor lang tid det tog

   Resultat 3 og 4 er "hvad er gået fra hvad til hvad": GO-nøgle og
   -sagsnummer ved siden af SBSYS-id og -sagsnummer. Det er den tabel man
   skal bruge, når en sagsbehandler spørger "hvor blev min sag af?".

   Variablen nedenfor afgrænser til én bølge. Lad den være NULL for alt.
   ===================================================================== */

DECLARE @boelge VARCHAR(40) = NULL;     -- fx 'ppr'
DECLARE @maks   INT         = 200;      -- rækker pr. detaljeafsnit

/* --- 1. Kørsler ---------------------------------------------------- */
SELECT TOP (@maks)
       '1. Kørsler'            AS afsnit,
       r.run_id,
       r.fase,
       r.objekt_type,
       r.boelge,
       r.miljoe,
       CASE WHEN r.dry_run = 1 THEN 'dry-run' ELSE 'LIVE' END AS tilstand,
       r.maal                  AS sbsys,
       r.status,
       r.startet,
       r.afsluttet,
       DATEDIFF(SECOND, r.startet, ISNULL(r.afsluttet, SYSUTCDATETIME())) AS sekunder,
       r.note
FROM   konv.run r
WHERE  (@boelge IS NULL OR r.boelge = @boelge)
ORDER  BY r.run_id DESC;

/* --- 2. Status pr. bølge og objekttype ------------------------------
   'pending' tæller både det der aldrig er forsøgt og det der venter på
   næste kørsel. 'failed' er ikke terminalt — det kommer med igen når
   årsagen er rettet.                                                   */
SELECT '2. Status'                                                  AS afsnit,
       ISNULL(m.boelge, '(uden bølge)')                             AS boelge,
       m.objekt_type,
       COUNT(*)                                                     AS i_alt,
       SUM(CASE WHEN m.status = 'loaded'  THEN 1 ELSE 0 END)        AS indlaest,
       SUM(CASE WHEN m.status = 'pending' THEN 1 ELSE 0 END)        AS venter,
       SUM(CASE WHEN m.status = 'failed'  THEN 1 ELSE 0 END)        AS fejlet,
       SUM(CASE WHEN m.status = 'manual'  THEN 1 ELSE 0 END)        AS manuel,
       SUM(CASE WHEN m.status = 'skipped' THEN 1 ELSE 0 END)        AS sprunget_over,
       CAST(100.0 * SUM(CASE WHEN m.status = 'loaded' THEN 1 ELSE 0 END)
            / NULLIF(COUNT(*), 0) AS DECIMAL(5,1))                  AS pct_indlaest,
       MAX(m.opdateret)                                             AS senest
FROM   konv.id_map m
WHERE  (@boelge IS NULL OR m.boelge = @boelge)
GROUP  BY m.boelge, m.objekt_type
ORDER  BY boelge, m.objekt_type;

/* --- 3. Fra → til: sager -------------------------------------------
   Venstre side er GO, højre side er SBSYS. En række uden sbsys_id er
   ikke kommet over.                                                    */
SELECT TOP (@maks)
       '3. Sager fra GO til SBSYS' AS afsnit,
       s.boelge,
       s.kilde_noegle              AS go_noegle,
       s.go_sagsnummer,
       s.titel                     AS go_titel,
       s.kle_go,
       s.afdeling_go,
       '->'                        AS retning,
       m.sbsys_id,
       m.sbsys_nummer,
       m.status,
       m.forsoeg,
       m.run_id,
       m.opdateret                 AS sidst_forsoegt,
       m.sidste_fejl
FROM   stg.sag s
LEFT   JOIN konv.id_map m
       ON  m.objekt_type  = 'sag'
       AND m.kilde_system = 'GO'
       AND m.kilde_noegle = s.kilde_noegle
WHERE  (@boelge IS NULL OR s.boelge = @boelge)
ORDER  BY CASE WHEN m.sbsys_id IS NULL THEN 0 ELSE 1 END,   -- det uafklarede først
         s.go_sagsnummer;

/* --- 4. Fra → til: dokumenter --------------------------------------- */
SELECT TOP (@maks)
       '4. Dokumenter fra GO til SBSYS' AS afsnit,
       d.boelge,
       d.kilde_noegle                   AS go_noegle,
       d.titel                          AS go_titel,
       d.fil_navn,
       d.fil_status,
       '->'                             AS retning,
       m.sbsys_id,
       m.status,
       m.sidste_fejl,
       sm.sbsys_id                      AS sag_i_sbsys
FROM   stg.dokument d
LEFT   JOIN konv.id_map m
       ON  m.objekt_type  = 'dokument'
       AND m.kilde_noegle = d.kilde_noegle
LEFT   JOIN konv.id_map sm
       ON  sm.objekt_type  = 'sag'
       AND sm.kilde_noegle = d.sag_noegle
WHERE  (@boelge IS NULL OR d.boelge = @boelge)
ORDER  BY CASE WHEN m.sbsys_id IS NULL THEN 0 ELSE 1 END, d.kilde_noegle;

/* --- 5. Afviste ------------------------------------------------------
   Senest afviste først. Beskrivelsen er maskeret for CPR allerede ved
   indsættelsen.                                                        */
SELECT TOP (@maks)
       '5. Afviste' AS afsnit,
       a.tidspunkt,
       a.run_id,
       a.objekt_type,
       a.boelge,
       a.kilde_noegle,
       a.regel,
       a.alvorlighed,
       a.felt,
       a.vaerdi,
       a.beskrivelse
FROM   konv.afvist a
WHERE  (@boelge IS NULL OR a.boelge = @boelge)
ORDER  BY a.afvist_id DESC;

/* --- 6. Afvisningsårsager, sorteret efter hvor mange de rammer ------
   Den øverste række er næsten altid den billigste at rette.            */
SELECT '6. Afvisningsårsager' AS afsnit,
       a.objekt_type,
       a.regel,
       a.felt,
       COUNT(*)               AS antal,
       COUNT(DISTINCT a.vaerdi) AS forskellige_vaerdier,
       MIN(a.beskrivelse)     AS eksempel,
       MAX(a.tidspunkt)       AS senest
FROM   konv.afvist a
WHERE  (@boelge IS NULL OR a.boelge = @boelge)
GROUP  BY a.objekt_type, a.regel, a.felt
ORDER  BY antal DESC;

/* --- 7. Manglende mapning -------------------------------------------
   Det her er listen en fagperson skal have. `konv mapping mangler`
   skriver samme indhold til CSV.                                       */
SELECT TOP (@maks)
       '7. Mangler mapning' AS afsnit,
       k.liste,
       k.go_vaerdi,
       k.go_tekst,
       k.handling,
       k.kommentar
FROM   map.kodeliste k
WHERE  k.handling = 'bloker'
    OR (k.handling = 'map' AND (k.sbsys_vaerdi IS NULL OR k.sbsys_vaerdi = ''))
ORDER  BY k.liste, k.go_vaerdi;

/* --- 8. Fejlede kald -------------------------------------------------
   Kaldeloggen indeholder aldrig payloads — kun hvad der blev kaldt,
   hvad der kom retur og hvor lang tid det tog. Korrelations-id'et er
   det SBSYS' drift skal bruge for at finde kaldet i deres egen log.    */
SELECT TOP (@maks)
       '8. Fejlede kald' AS afsnit,
       k.tidspunkt,
       k.run_id,
       k.operation,
       k.metode,
       k.sti,
       k.http_status,
       k.forsoeg_nr,
       k.varighed_ms,
       k.korrelations_id,
       k.objekt_type,
       k.fejl_tekst
FROM   konv.api_kald k
WHERE  k.http_status IS NULL
    OR k.http_status >= 400
ORDER  BY k.tidspunkt DESC;
