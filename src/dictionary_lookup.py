"""Fast offline Japanese-English word lookup backed by bundled JMdict data."""

from dataclasses import dataclass
import json
from pathlib import Path
import sqlite3

from offline_translation import resource_root


DICTIONARY_RELATIVE_PATH = Path("data") / "jmdict_english.sqlite3"
MAX_ENTRIES = 3
MAX_SENSES_PER_ENTRY = 4
MAX_GLOSSES_PER_SENSE = 3

POS_LABELS = {
    "n": "noun",
    "n-adv": "adverbial noun",
    "n-pref": "prefix noun",
    "n-suf": "suffix noun",
    "pn": "pronoun",
    "v1": "Ichidan verb",
    "v5b": "Godan verb",
    "v5g": "Godan verb",
    "v5k": "Godan verb",
    "v5k-s": "Godan verb",
    "v5m": "Godan verb",
    "v5n": "Godan verb",
    "v5r": "Godan verb",
    "v5s": "Godan verb",
    "v5t": "Godan verb",
    "v5u": "Godan verb",
    "vs": "suru verb",
    "vs-i": "suru verb",
    "vk": "kuru verb",
    "vt": "transitive",
    "vi": "intransitive",
    "adj-i": "i-adjective",
    "adj-na": "na-adjective",
    "adj-no": "no-adjective",
    "adv": "adverb",
    "exp": "expression",
    "prt": "particle",
    "conj": "conjunction",
    "int": "interjection",
}


class DictionarySetupError(RuntimeError):
    """Raised when the bundled JMdict database is missing or invalid."""


@dataclass(frozen=True)
class DictionaryEntry:
    """One JMdict entry: a headword/reading pair plus its list of senses
    (each a dict with "p" part-of-speech codes and "g" glosses, straight from
    the bundled database's JSON column)."""

    headword: str
    reading: str
    common: bool
    senses: list


@dataclass(frozen=True)
class DictionaryMatch:
    """The result of a successful lookup(): which candidate string actually
    matched, and every entry found for it (see MAX_ENTRIES)."""

    query: str
    entries: list
    dictionary_date: str


def bundled_dictionary_path():
    """Where the JMdict SQLite file lives -- next to the source checkout, or
    inside a PyInstaller bundle's extracted data (see
    offline_translation.resource_root)."""
    return resource_root() / DICTIONARY_RELATIVE_PATH


class LocalJapaneseDictionary:
    """Thread-owned exact-form JMdict lookup with compact display formatting."""

    def __init__(self, database_path=None):
        """Open the bundled JMdict database read-only (mode=ro&immutable=1 --
        this file never changes at runtime, so SQLite can skip locking
        overhead) and validate its schema version. Raises
        DictionarySetupError if the file is missing, unreadable, or from an
        incompatible schema version -- callers are expected to catch this
        and degrade to phrase-translation-only rather than fail startup."""
        self.database_path = Path(database_path or bundled_dictionary_path())
        if not self.database_path.is_file():
            raise DictionarySetupError(
                f"The bundled JMdict database is missing: {self.database_path}"
            )
        try:
            uri = self.database_path.resolve().as_uri() + "?mode=ro&immutable=1"
            self.connection = sqlite3.connect(uri, uri=True, timeout=2.0)
            metadata = dict(self.connection.execute("SELECT key, value FROM metadata"))
            if metadata.get("schema_version") != "1":
                raise DictionarySetupError("The bundled JMdict schema is unsupported.")
            self.dictionary_date = metadata["dictionary_date"]
            self.tags = json.loads(metadata["tags_json"])
        except DictionarySetupError:
            raise
        except Exception as exc:
            raise DictionarySetupError(
                f"The bundled JMdict database could not be opened: {exc}"
            ) from exc

    def lookup(self, candidates):
        """Try each candidate string (in order -- see
        HoverTranslator.dictionary_candidates for how they're built: exact
        text first, then lemma, then surface form) against the forms table
        and return the first match, or None if nothing matched. Stops at the
        first hit rather than merging results across candidates."""
        seen = set()
        for candidate in candidates:
            candidate = candidate.strip()
            if not candidate or candidate in seen:
                continue
            seen.add(candidate)
            rows = self.connection.execute(
                """
                SELECT e.headword, e.reading, e.common, e.senses_json,
                       f.common, f.kind
                FROM forms AS f
                JOIN entries AS e ON e.id = f.entry_id
                WHERE f.form = ?
                ORDER BY f.common DESC, e.common DESC, f.kind, e.id
                LIMIT ?
                """,
                (candidate, MAX_ENTRIES),
            ).fetchall()
            if rows:
                entries = [
                    DictionaryEntry(
                        headword=row[0],
                        reading=row[1],
                        common=bool(row[2]),
                        senses=json.loads(row[3]),
                    )
                    for row in rows
                ]
                return DictionaryMatch(candidate, entries, self.dictionary_date)
        return None

    def _part_of_speech(self, sense):
        """Human-readable part-of-speech label(s) for one sense, e.g.
        "noun · transitive" -- codes are mapped through POS_LABELS first,
        then the dictionary's own bundled tag glossary as a fallback for any
        code POS_LABELS doesn't cover."""
        labels = []
        for code in sense.get("p", []):
            label = POS_LABELS.get(code, self.tags.get(code, code))
            if label not in labels:
                labels.append(label)
        return " · ".join(labels[:3])

    @staticmethod
    def _definition(sense):
        """Semicolon-joined glosses for one sense, capped at
        MAX_GLOSSES_PER_SENSE so a sense with many near-duplicate glosses
        doesn't dominate the popup."""
        glosses = sense.get("g", [])[:MAX_GLOSSES_PER_SENSE]
        return "; ".join(glosses)

    def format_match(self, match):
        """Render a DictionaryMatch as the multi-line display string shown
        in the hover popup / saved-word detail panel: headword(+reading),
        part of speech, then numbered senses -- multiple entries (e.g. two
        unrelated words sharing a reading) get numbered headers instead."""
        lines = ["JMdict dictionary · EDRDG"]  # source attribution header, always first line
        multiple = len(match.entries) > 1
        for entry_number, entry in enumerate(match.entries, start=1):
            # Headword line: numbered ("1. ", "2. ", ...) only when there's more
            # than one entry to distinguish; reading shown in 【】 only when it
            # actually differs from the headword itself (kana-only words have
            # reading == headword, so showing it again would be redundant).
            prefix = f"{entry_number}. " if multiple else ""
            reading = f"【{entry.reading}】" if entry.reading != entry.headword else ""
            lines.append(f"{prefix}{entry.headword}{reading}")

            # Part-of-speech line, taken from the first sense only -- showing
            # it once per entry (not once per sense) keeps the popup compact.
            senses = entry.senses[:MAX_SENSES_PER_ENTRY]
            if senses:
                part_of_speech = self._part_of_speech(senses[0])
                if part_of_speech:
                    lines.append(part_of_speech)

            # One line per sense/definition -- numbered when there's only one
            # entry (so "1./2./3." reads as sense numbers), bulleted when
            # there are multiple entries (so numbers aren't ambiguous between
            # "which entry" and "which sense").
            for sense_number, sense in enumerate(senses, start=1):
                definition = self._definition(sense)
                if not definition:
                    continue
                marker = f"{sense_number}. " if not multiple else "• "
                lines.append(marker + definition)

            # Blank-line separator between entries (but not after the last one).
            if multiple and entry_number < len(match.entries):
                lines.append("")
        return "\n".join(lines).strip()

    def close(self):
        """Close the SQLite connection; failures are swallowed since this is
        always called during best-effort shutdown/error-recovery paths."""
        try:
            self.connection.close()
        except Exception:
            pass
