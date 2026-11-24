"""Build a private lookup from Core's confirmed image deliveries, using a JSONL export."""

import json
import sqlite3
import sys
from pathlib import Path

from superlily_contracts.rendering import RenderDocument, render_document_plain_text


def build(destination: Path) -> None:
    if destination.exists():
        raise FileExistsError(destination)
    db = sqlite3.connect(destination)
    destination.chmod(0o600)
    try:
        with db:
            db.execute("""CREATE TABLE transcripts(chat TEXT, message TEXT, filename TEXT, body TEXT NOT NULL,
                       PRIMARY KEY(chat,message,filename)) WITHOUT ROWID""")
            for line in sys.stdin:
                chat, message, artifact, digest, accessible, doc = json.loads(line)
                try:
                    body = render_document_plain_text(RenderDocument.model_validate(doc)) or accessible
                except ValueError:
                    body = accessible
                if not body:
                    continue
                for filename in (digest + ".png", "lily-render-" + artifact + ".png"):
                    db.execute(
                        "INSERT OR IGNORE INTO transcripts VALUES (?,?,?,?)", (chat, message, filename, body)
                    )
        print(
            db.execute("SELECT count(DISTINCT message) FROM transcripts").fetchone()[0],
            "confirmed image replies indexed",
        )
    finally:
        db.close()


if __name__ == "__main__":
    build(Path(sys.argv[1]))
