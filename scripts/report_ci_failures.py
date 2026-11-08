"""Publier les échecs JUnit en annotations GitHub, sans accès aux journaux privés."""
from pathlib import Path
import sys
import xml.etree.ElementTree as ET


def escape(value: str) -> str:
    return value.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")


def main() -> None:
    report = Path(sys.argv[1])
    if not report.is_file():
        print("::error title=Rapport pytest absent::La commande a échoué avant de produire son rapport JUnit.")
        return
    root = ET.parse(report).getroot()
    emitted = 0
    for case in root.iter("testcase"):
        for failure in case:
            if failure.tag not in {"failure", "error"}:
                continue
            name = f"{case.get('classname', '')}.{case.get('name', '')}"
            detail = (failure.text or failure.get("message", "Erreur pytest"))[-3500:]
            print(f"::error title={escape(name)}::{escape(detail)}")
            emitted += 1
            if emitted == 12:
                return


if __name__ == "__main__":
    main()
