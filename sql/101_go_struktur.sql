/*
  101_go_struktur.sql — hvad staar der egentlig i GOStaging?

  Koeres FOER 100_extract_skabelon.sql skrives faerdig. Svarer paa de tre
  ting extract-laget skal vide: hvilke kolonner findes, hvilken type har
  de, og hvor mange raekker er der.

  Kun laesning. Roerer ingenting.

      uv run konv db koer sql/101_go_struktur.sql

  Ligger GOStaging paa en anden instans, saa koer filen DER i stedet, i
  SSMS. Den bruger kun INFORMATION_SCHEMA og sys, som findes alle steder.
*/

SET NOCOUNT ON;

/* --- 1. Kolonner og typer, tabel for tabel -------------------------- */
SELECT
    c.TABLE_NAME                                        AS tabel,
    c.ORDINAL_POSITION                                  AS nr,
    c.COLUMN_NAME                                       AS kolonne,
    c.DATA_TYPE
      + CASE
          WHEN c.CHARACTER_MAXIMUM_LENGTH = -1 THEN '(max)'
          WHEN c.CHARACTER_MAXIMUM_LENGTH IS NOT NULL
               THEN '(' + CONVERT(VARCHAR(11), c.CHARACTER_MAXIMUM_LENGTH) + ')'
          WHEN c.DATA_TYPE IN ('decimal', 'numeric')
               THEN '(' + CONVERT(VARCHAR(11), c.NUMERIC_PRECISION) + ','
                        + CONVERT(VARCHAR(11), c.NUMERIC_SCALE) + ')'
          ELSE ''
        END                                             AS type,
    CASE c.IS_NULLABLE WHEN 'YES' THEN 'NULL' ELSE 'NOT NULL' END AS nullable
FROM   GOStaging.INFORMATION_SCHEMA.COLUMNS AS c
WHERE  c.TABLE_SCHEMA = 'dbo'
ORDER  BY c.TABLE_NAME, c.ORDINAL_POSITION;

/* --- 2. Raekketal pr. tabel ----------------------------------------- */
/* Fra sys.partitions, ikke COUNT(*): oejeblikkeligt, og paa en tabel med
   millioner af raekker er forskellen minutter. Tallet kan vaere et par
   raekker ved siden af lige efter en indlaesning; til at faa fornemmelsen
   af stoerrelsesordenen er det rigeligt. */
SELECT
    t.name                                              AS tabel,
    SUM(p.rows)                                         AS raekker
FROM   GOStaging.sys.tables      AS t
JOIN   GOStaging.sys.partitions  AS p ON p.object_id = t.object_id
WHERE  p.index_id IN (0, 1)
GROUP  BY t.name
ORDER  BY SUM(p.rows) DESC;

/* --- 3. Noeglekandidater -------------------------------------------- */
/* Den vigtigste enkeltbeslutning i extract-laget: hvilken kolonne er
   objektets STABILE identitet mellem to leverancer. Her listes de der
   ligner en noegle, saa de kan vurderes frem for gaettes.

   Bemaerk: en IDENTITY-kolonne er stagingdatabasens eget loebenummer og
   maa ALDRIG bruges som kilde_noegle — den aendrer sig ved naeste
   indlaesning. Den markeres derfor eksplicit nedenfor. */
SELECT
    t.name                                              AS tabel,
    c.name                                              AS kolonne,
    TYPE_NAME(c.user_type_id)                           AS type,
    CASE WHEN c.is_identity = 1 THEN 'IDENTITY - BRUG ALDRIG' ELSE '' END AS advarsel
FROM   GOStaging.sys.tables   AS t
JOIN   GOStaging.sys.columns  AS c ON c.object_id = t.object_id
WHERE  c.name LIKE '%id'
    OR c.name LIKE '%guid'
    OR c.name LIKE '%nummer'
    OR c.name LIKE '%key'
    OR c.is_identity = 1
ORDER  BY t.name, c.column_id;

/* --- 4. Primaernoegler, hvis der er nogen --------------------------- */
SELECT
    t.name                                              AS tabel,
    i.name                                              AS indeks,
    c.name                                              AS kolonne
FROM   GOStaging.sys.indexes        AS i
JOIN   GOStaging.sys.tables         AS t  ON t.object_id = i.object_id
JOIN   GOStaging.sys.index_columns  AS ic ON ic.object_id = i.object_id
                                         AND ic.index_id  = i.index_id
JOIN   GOStaging.sys.columns        AS c  ON c.object_id = ic.object_id
                                         AND c.column_id = ic.column_id
WHERE  i.is_primary_key = 1
ORDER  BY t.name, ic.key_ordinal;
