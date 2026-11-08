"""Export des constats, compatible tableurs francophones."""
import csv
from pathlib import Path

FIELDS = ["catégorie", "dossier", "autre_dossier", "statut", "champ", "actuel", "proposé", "motif", "lien"]


def export_csv(path: Path, rows: list[dict]) -> None:
    def safe(value):
        text = str(value if value is not None else "")
        # Les tags sont des entrées non fiables : empêcher les formules de tableur.
        return "'" + text if text.lstrip().startswith(("=", "+", "-", "@")) or text.startswith(("\t", "\r")) else text

    with path.open("w", newline="", encoding="utf-8-sig") as stream:
        writer = csv.DictWriter(stream, fieldnames=FIELDS, delimiter=";")
        writer.writeheader()
        for row in rows:
            writer.writerow({field: safe(row.get(field, "")) for field in FIELDS})
