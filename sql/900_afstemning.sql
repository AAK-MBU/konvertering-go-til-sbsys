/*
  900_afstemning.sql — kvalitetssikring efter indlæsning.

  Afstemning er ikke "vi tjekker lige til sidst". Den er gaten mellem
  konvertering og undervisning: bliver der undervist i data der ikke er
  afstemt, opdages fejlen af en hel medarbejdergruppe på én gang.

  Kør pr. bølge, efter indlæsningen og før UV-blokken:
    sqlcmd -S <server> -d SBSYS_KONV -v run_id=<id> -v boelge=ppr -i sql/900_afstemning.sql

  Uden -v boelge køres der på tværs af alle bølger (brugbart i TEST).
*/

SET NOCOUNT ON;
DECLARE @run_id BIGINT      = TRY_CONVERT(BIGINT, '$(run_id)');
DECLARE @boelge VARCHAR(40) = NULLIF('$(boelge)', '');

/* --- 1. Totaler pr. objekttype ------------------------------------ */
INSERT INTO konv.afstemning (run_id, boelge, maal, dimension, antal_kilde, antal_maal)
SELECT @run_id, @boelge, 'total_pr_type', k.objekt_type, k.antal_kilde, ISNULL(m.antal_maal, 0)
FROM (
        SELECT 'sag' AS objekt_type, COUNT(*) AS antal_kilde FROM stg.sag
        WHERE @boelge IS NULL OR boelge = @boelge
  UNION SELECT 'dokument', COUNT(*) FROM stg.dokument
        WHERE @boelge IS NULL OR boelge = @boelge
  UNION SELECT 'notat',    COUNT(*) FROM stg.notat
        WHERE @boelge IS NULL OR boelge = @boelge
) k
LEFT JOIN (
    SELECT objekt_type, COUNT(*) AS antal_maal
    FROM   konv.id_map
    WHERE  status = 'loaded' AND (@boelge IS NULL OR boelge = @boelge)
    GROUP  BY objekt_type
) m ON m.objekt_type = k.objekt_type;

/* --- 2. Sager pr. år: fanger skæve datokonverteringer -------------- */
INSERT INTO konv.afstemning (run_id, boelge, maal, dimension, antal_kilde, antal_maal)
SELECT @run_id, @boelge, 'sager_pr_aar',
       CONVERT(VARCHAR(4), YEAR(s.oprettet_dato)),
       COUNT(*),
       SUM(CASE WHEN m.status = 'loaded' THEN 1 ELSE 0 END)
FROM   stg.sag s
LEFT   JOIN konv.id_map m ON m.objekt_type = 'sag' AND m.kilde_noegle = s.kilde_noegle
WHERE  @boelge IS NULL OR s.boelge = @boelge
GROUP  BY YEAR(s.oprettet_dato);

/* --- 3. Dokumenter pr. sag: fanger "sagen kom over, bilagene gjorde ikke" */
INSERT INTO konv.afstemning (run_id, boelge, maal, dimension, antal_kilde, antal_maal)
SELECT @run_id, @boelge, 'dok_pr_sag_afvigelse', s.kilde_noegle,
       COUNT(d.kilde_noegle),
       SUM(CASE WHEN dm.status = 'loaded' THEN 1 ELSE 0 END)
FROM   stg.sag s
JOIN   stg.dokument d ON d.sag_noegle = s.kilde_noegle
LEFT   JOIN konv.id_map dm ON dm.objekt_type = 'dokument' AND dm.kilde_noegle = d.kilde_noegle
WHERE  @boelge IS NULL OR s.boelge = @boelge
GROUP  BY s.kilde_noegle
HAVING COUNT(d.kilde_noegle) <> SUM(CASE WHEN dm.status = 'loaded' THEN 1 ELSE 0 END);

/* --- 4. Sager uden bølge: må ikke findes ved en bølgekørsel -------- */
INSERT INTO konv.afstemning (run_id, boelge, maal, dimension, antal_kilde, antal_maal)
SELECT @run_id, @boelge, 'sager_uden_boelge', '(alle)', COUNT(*), 0
FROM   stg.v_boelge_mangler;

GO

/* --- 5. Rapportviews til mennesker -------------------------------- */

CREATE OR ALTER VIEW konv.v_status AS
SELECT ISNULL(boelge, '(uden bølge)') AS boelge,
       objekt_type,
       SUM(CASE WHEN status = 'loaded'  THEN 1 ELSE 0 END) AS indlaest,
       SUM(CASE WHEN status = 'failed'  THEN 1 ELSE 0 END) AS fejlet,
       SUM(CASE WHEN status = 'manual'  THEN 1 ELSE 0 END) AS manuel,
       SUM(CASE WHEN status = 'skipped' THEN 1 ELSE 0 END) AS sprunget_over,
       SUM(CASE WHEN status = 'pending' THEN 1 ELSE 0 END) AS venter,
       COUNT(*) AS i_alt
FROM   konv.id_map
GROUP  BY boelge, objekt_type;
GO

/* Top-fejl: hvad skal vi rette først for at flytte flest objekter? */
CREATE OR ALTER VIEW konv.v_top_fejl AS
SELECT TOP 50
       ISNULL(boelge, '(uden bølge)') AS boelge,
       objekt_type, regel, alvorlighed, COUNT(*) AS antal,
       MIN(beskrivelse) AS eksempel
FROM   konv.afvist
GROUP  BY boelge, objekt_type, regel, alvorlighed
ORDER  BY COUNT(*) DESC;
GO

/* Fejlede API-kald med korrelations-id, så en udvikler kan gå direkte
   til den præcise request i loggen. */
CREATE OR ALTER VIEW konv.v_api_fejl AS
SELECT TOP 500
       tidspunkt, korrelations_id, objekt_type, kilde_noegle,
       operation, http_status, fejl_kode, fejl_tekst, forsoeg_nr
FROM   konv.api_kald
WHERE  http_status >= 400
ORDER  BY tidspunkt DESC;
GO

/*
  Gate-view: er bølgen klar til undervisning?
  Alt skal stå på 0 i afvigelseskolonnerne. Ét opslag, ét svar.
*/
CREATE OR ALTER VIEW konv.v_boelge_gate AS
SELECT f.boelge, f.navn, f.boelge_status,
       f.antal_sager, f.indlaeste_sager,
       f.antal_sager - f.indlaeste_sager AS manglende_sager,
       f.antal_dokumenter, f.indlaeste_dokumenter,
       f.antal_dokumenter - f.indlaeste_dokumenter AS manglende_dokumenter,
       ISNULL(fejl.antal, 0) AS afviste,
       CASE WHEN f.antal_sager = f.indlaeste_sager
             AND f.antal_dokumenter = f.indlaeste_dokumenter
             AND ISNULL(fejl.antal, 0) = 0
            THEN 'klar' ELSE 'ikke klar' END AS gate
FROM   stg.v_boelge_fremdrift f
LEFT   JOIN (SELECT boelge, COUNT(*) AS antal FROM konv.afvist
             WHERE alvorlighed = 'blok' GROUP BY boelge) fejl
       ON fejl.boelge = f.boelge;
GO
