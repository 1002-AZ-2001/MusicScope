from types import SimpleNamespace

import pytest
from mutagen.id3 import ID3, TALB, TDOR, TDRC, TIT2, TPE1, TPE2, TRCK, TXXX, UFID

from musicscope.metadata import canonical_tags, read_track
from musicscope.normalization import name_key


def test_unicode_preserves_voicing_and_non_latin():
    assert name_key("Beyoncé — ＬＩＶＥ") == name_key("BEYONCE live")
    assert name_key("カ") != name_key("ガ")
    assert name_key("ガ") == name_key("ｶﾞ")
    assert name_key("Album Deluxe") != name_key("Album")


def test_id3_dates_identifiers_and_track_totals():
    tags = ID3()
    for frame in [TIT2(encoding=3, text=["Chanson"]), TALB(encoding=3, text=["Album"]),
                  TPE1(encoding=3, text=["Invité"]), TPE2(encoding=3, text=["Groupe"]),
                  TDOR(encoding=3, text=["1995"]), TDRC(encoding=3, text=["2020-01-02"]),
                  TRCK(encoding=3, text=["3/12"]),
                  TXXX(encoding=3, desc="MusicBrainz Album Id", text=["edition"]),
                  TXXX(encoding=3, desc="MusicBrainz Release Group Id", text=["group"]),
                  UFID(owner="http://musicbrainz.org", data=b"recording")]:
        tags.add(frame)
    result = canonical_tags(tags)
    assert result["artist"] == ["Invité"]
    assert result["albumartist"] == ["Groupe"]
    assert result["originaldate"] == ["1995"]
    assert result["date"] == ["2020-01-02"]
    assert result["musicbrainz_albumid"] == ["edition"]
    assert result["musicbrainz_releasegroupid"] == ["group"]
    assert result["musicbrainz_trackid"] == ["recording"]
    assert result["tracktotal"] == ["12"]


def test_mp4_atoms_and_freeform():
    result = canonical_tags({"©nam": ["Titre"], "©ART": ["Invité"], "aART": ["Groupe"],
                             "©alb": ["Album"], "©day": ["2010"],
                             "trkn": [(2, 9)], "disk": [(1, 2)],
                             "----:com.apple.iTunes:MusicBrainz Track Id": [b"recording"],
                             "----:com.apple.iTunes:ORIGINALDATE": [b"2001"]})
    assert result["title"] == ["Titre"]
    assert result["albumartist"] == ["Groupe"]
    assert result["originaldate"] == ["2001"]
    assert result["date"] == ["2010"]
    assert result["tracknumber"] == ["2"]
    assert result["tracktotal"] == ["9"]
    assert result["disctotal"] == ["2"]
    assert result["musicbrainz_trackid"] == ["recording"]


def test_vorbis_tags_case_and_multi_artists():
    result = canonical_tags({"TITLE": ["Titre"], "ALBUMARTIST": ["Groupe"],
                             "ARTIST": ["Groupe", "Invité"], "DATE": ["2020"],
                             "ORIGINALDATE": ["2000"], "MUSICBRAINZ_ALBUMARTISTID": ["a"],
                             "MUSICBRAINZ_ARTISTID": ["b", "c"],
                             "MUSICBRAINZ_RELEASEGROUPID": ["g"],
                             "MUSICBRAINZ_TRACKID": ["recording"],
                             "MUSICBRAINZ_RELEASETRACKID": ["release-track"],
                             "DISCNUMBER": ["2/3"]})
    assert result["artist"] == ["Groupe", "Invité"]
    assert result["musicbrainz_trackid"] == ["recording"]
    assert result["musicbrainz_artistid"] == ["b", "c"]
    assert result["disctotal"] == ["3"]


@pytest.mark.parametrize("key", ["RELEASECOUNTRY", "TXXX:MusicBrainz Album Release Country",
                                 "----:com.apple.iTunes:MusicBrainz Album Release Country"])
def test_release_country_tag_aliases(key):
    assert canonical_tags({key: [b"FR"]})["releasecountry"] == ["FR"]


def test_read_track_does_not_save(monkeypatch, tmp_path):
    path = tmp_path / "test.flac"
    path.write_bytes(b"read only fixture")
    audio = type("FLAC", (), {"tags": {"TITLE": ["Titre"]},
                              "info": SimpleNamespace(length=12.5)})()
    monkeypatch.setattr("musicscope.metadata.mutagen.File", lambda *a, **kw: audio)
    track = read_track(path)
    assert track.duration == 12.5
    assert track.codec == "FLAC"
    assert track.size == len(b"read only fixture")
    assert path.read_bytes() == b"read only fixture"


def test_invalid_audio_rejected(monkeypatch, tmp_path):
    path = tmp_path / "test.flac"
    path.touch()
    monkeypatch.setattr("musicscope.metadata.mutagen.File", lambda *a, **kw: None)
    with pytest.raises(ValueError, match="invalide"):
        read_track(path)
