from musicscope.audit import audit_album
from musicscope.models import Album, Track


def track(number, disc=1, **extra):
    tags = {"album": ["Album"], "albumartist": ["Groupe"], "title": ["Titre"],
            "artist": ["Groupe"], "date": ["2020"], "tracknumber": [str(number)],
            "discnumber": [str(disc)]}
    tags.update({key: [value] for key, value in extra.items()})
    return Track(path=f"/{disc}/{number}", tags=tags)


def test_guests_and_multidisc_are_valid():
    album = Album("/album", [track(1), track(2, artist="Invité"), track(1, 2), track(2, 2)])
    assert audit_album(album) == []


def test_duplicate_numbers_and_potential_missing_tracks():
    album = Album("/album", [track(1, tracktotal="4"), track(1), track(3)])
    issues = audit_album(album)
    assert any("répétés" in issue for issue in issues)
    assert any("manquantes [2, 4]" in issue for issue in issues)


def test_missing_and_inconsistent_tags():
    album = Album("/album", [track(1), track(2, album="Autre", date="1999", title="")], complete=False)
    issues = audit_album(album)
    assert any("Inventaire incomplet" in issue for issue in issues)
    assert any("Titre manquant" in issue for issue in issues)
    assert any("Titres d'album incohérents" in issue for issue in issues)
    assert any("Dates d'édition incohérents" in issue for issue in issues)


def test_disc_total_and_release_ids_conflict():
    album = Album("/album", [track(1, disctotal="2", musicbrainz_albumid="a"),
                              track(2, musicbrainz_albumid="b")])
    issues = audit_album(album)
    assert any("Nombre de disques à vérifier" in issue for issue in issues)
    assert any("MBID d'édition incohérents" in issue for issue in issues)


def test_extreme_numbers_are_bounded():
    album = Album("/album", [track(10**12, disc=10**12)])
    assert any("anormalement élevée" in issue for issue in audit_album(album))


def test_consistent_multi_albumartist_credit_is_not_conflict():
    first, second = track(1), track(2)
    first.tags["albumartist"] = ["Artiste A", "Artiste B"]
    second.tags["albumartist"] = ["Artiste B", "Artiste A"]
    assert audit_album(Album("/album", [first, second])) == []
    second.tags["albumartist"] = ["Artiste B"]
    assert any("Artistes d'album incohérents" in issue
               for issue in audit_album(Album("/album", [first, second])))
