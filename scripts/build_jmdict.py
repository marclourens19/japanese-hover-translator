r"""Build the compact runtime dictionary from jmdict-simplified English JSON.

Usage:
    python scripts/build_jmdict.py path\to\jmdict-eng-X.Y.Z.json \
        data\jmdict_english.sqlite3
"""

import argparse
import json
from pathlib import Path
import sqlite3


def choose_headword(entry):
    common_kanji = next((item for item in entry["kanji"] if item["common"]), None)
    kanji = common_kanji or (entry["kanji"][0] if entry["kanji"] else None)
    common_kana = next((item for item in entry["kana"] if item["common"]), None)
    kana = common_kana or entry["kana"][0]
    return (kanji or kana)["text"], kana["text"]


def compact_senses(entry):
    result = []
    for sense in entry["sense"]:
        glosses = [item["text"] for item in sense["gloss"] if item.get("text")]
        if not glosses:
            continue
        result.append(
            {
                "g": glosses,
                "p": sense.get("partOfSpeech", []),
                "m": sense.get("misc", []),
                "f": sense.get("field", []),
            }
        )
    return json.dumps(result, ensure_ascii=False, separators=(",", ":"))


def build(source_path, destination_path):
    source_path = Path(source_path)
    destination_path = Path(destination_path)
    payload = json.loads(source_path.read_text(encoding="utf-8"))

    destination_path.parent.mkdir(parents=True, exist_ok=True)
    if destination_path.exists():
        destination_path.unlink()

    connection = sqlite3.connect(destination_path)
    connection.executescript(
        """
        PRAGMA journal_mode = OFF;
        PRAGMA synchronous = OFF;
        PRAGMA temp_store = MEMORY;
        CREATE TABLE metadata (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        ) WITHOUT ROWID;
        CREATE TABLE entries (
            id INTEGER PRIMARY KEY,
            headword TEXT NOT NULL,
            reading TEXT NOT NULL,
            common INTEGER NOT NULL,
            senses_json TEXT NOT NULL
        );
        CREATE TABLE forms (
            form TEXT NOT NULL,
            entry_id INTEGER NOT NULL,
            common INTEGER NOT NULL,
            kind INTEGER NOT NULL,
            PRIMARY KEY (form, entry_id)
        ) WITHOUT ROWID;
        """
    )
    metadata = {
        "schema_version": "1",
        "source": "scriptin/jmdict-simplified",
        "source_version": payload["version"],
        "dictionary_date": payload["dictDate"],
        "languages": ",".join(payload["languages"]),
        "entry_count": str(len(payload["words"])),
        "tags_json": json.dumps(
            payload["tags"], ensure_ascii=False, separators=(",", ":")
        ),
    }
    connection.executemany(
        "INSERT INTO metadata (key, value) VALUES (?, ?)", metadata.items()
    )

    entry_rows = []
    form_rows = []
    for entry in payload["words"]:
        headword, reading = choose_headword(entry)
        common = int(
            any(item["common"] for item in entry["kanji"])
            or any(item["common"] for item in entry["kana"])
        )
        entry_id = int(entry["id"])
        entry_rows.append(
            (entry_id, headword, reading, common, compact_senses(entry))
        )
        unique_forms = {}
        for item in entry["kanji"]:
            unique_forms[item["text"]] = (
                max(int(item["common"]), unique_forms.get(item["text"], (0, 0))[0]),
                0,
            )
        for item in entry["kana"]:
            previous = unique_forms.get(item["text"], (0, 1))
            unique_forms[item["text"]] = (
                max(int(item["common"]), previous[0]),
                min(1, previous[1]),
            )
        form_rows.extend(
            (form, entry_id, form_common, kind)
            for form, (form_common, kind) in unique_forms.items()
        )

        if len(entry_rows) >= 5000:
            connection.executemany(
                "INSERT INTO entries VALUES (?, ?, ?, ?, ?)", entry_rows
            )
            connection.executemany(
                "INSERT INTO forms VALUES (?, ?, ?, ?)", form_rows
            )
            entry_rows.clear()
            form_rows.clear()

    if entry_rows:
        connection.executemany(
            "INSERT INTO entries VALUES (?, ?, ?, ?, ?)", entry_rows
        )
        connection.executemany(
            "INSERT INTO forms VALUES (?, ?, ?, ?)", form_rows
        )

    connection.executescript(
        """
        CREATE INDEX forms_lookup
        ON forms (form, common DESC, kind, entry_id);
        ANALYZE;
        VACUUM;
        """
    )
    connection.commit()
    count = connection.execute("SELECT COUNT(*) FROM entries").fetchone()[0]
    connection.close()
    print(
        f"Built {destination_path} with {count:,} entries "
        f"({destination_path.stat().st_size / 1024 / 1024:.1f} MiB)"
    )


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("source_json")
    parser.add_argument("destination_database")
    arguments = parser.parse_args()
    build(arguments.source_json, arguments.destination_database)


if __name__ == "__main__":
    main()
