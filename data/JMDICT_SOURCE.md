# JMdict source and attribution

The bundled `jmdict_english.sqlite3` database is derived from the English
distribution of JMdict. JMdict is owned and maintained by the Electronic
Dictionary Research and Development Group (EDRDG) and was begun by Jim Breen.

- Dictionary date: 2026-07-13
- JMdict entries: 217,856
- Source conversion: `scriptin/jmdict-simplified` 3.6.2
- Source artifact: `jmdict-eng-3.6.2+20260713141310.json.zip`
- JMdict project: https://www.edrdg.org/wiki/index.php/JMdict-EDICT_Dictionary_Project
- Conversion project: https://github.com/scriptin/jmdict-simplified
- Licence: Creative Commons Attribution-ShareAlike 4.0, subject to the EDRDG
  General Dictionary Licence Statement

The SQLite file is a format conversion for fast exact-form lookup. Definitions,
readings, word forms, part-of-speech tags, and related dictionary content remain
JMdict data; no copyright over that material is claimed by this application.

Local copies of the EDRDG licence statement and the Creative Commons legal code
are distributed beside this file. Run `scripts/update_jmdict.ps1` at least monthly
to rebuild the database from the latest English JMdict release.
