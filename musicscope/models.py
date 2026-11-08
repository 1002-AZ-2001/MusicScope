"""Modèles partagés par le moteur et l'interface, sans dépendance Qt."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field


@dataclass
class Track:
    path: str
    codec: str = ""
    size: int = 0
    mtime_ns: int = 0
    duration: float | None = None
    tags: dict[str, list[str]] = field(default_factory=dict)
    device: int | None = None
    inode: int | None = None

    def value(self, key: str) -> str:
        return "; ".join(self.tags.get(key, []))

    @property
    def disc(self) -> int:
        return self.number("discnumber") or 1

    @property
    def number_in_disc(self) -> int:
        return self.number("tracknumber")

    def number(self, key: str) -> int:
        try:
            return int(self.value(key).split("/")[0])
        except (ValueError, TypeError):
            return 0


@dataclass
class Album:
    path: str
    tracks: list[Track] = field(default_factory=list)
    complete: bool = True
    issues: list[str] = field(default_factory=list)

    def values(self, key: str) -> set[str]:
        return {value for track in self.tracks for value in track.tags.get(key, []) if value}

    def tag_signatures(self, key: str) -> set[tuple[str, ...]]:
        """Valeurs conjointes par piste, sans confondre un crédit multiple
        identique partout avec des crédits différents d'une piste à l'autre.
        Les champs absents sont traités séparément par l'audit des champs vides.
        """
        return {tuple(sorted({value for value in track.tags.get(key, []) if value}))
                for track in self.tracks if any(track.tags.get(key, []))}

    def value(self, key: str) -> str:
        return "; ".join(sorted(self.values(key)))

    @property
    def ordered_tracks(self) -> list[Track]:
        return sorted(self.tracks, key=lambda t: (t.disc, t.number_in_disc, t.path))


@dataclass
class Inventory:
    root: str
    albums: list[Album] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    scanned_at: str = ""

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> Inventory:
        return cls(root=data["root"], errors=data.get("errors", []),
                   scanned_at=data.get("scanned_at", ""), albums=[
                       Album(path=a["path"], complete=a.get("complete", True),
                             issues=a.get("issues", []),
                             tracks=[Track(**t) for t in a["tracks"]])
                       for a in data.get("albums", [])])
