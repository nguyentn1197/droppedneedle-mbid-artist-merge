"""Build a tiny tagged FLAC library that reproduces the romanized-name split.

Usage: python scripts/e2e/make_library.py <music-dir>   (needs ffmpeg + mutagen)

Artists produced (all MBIDs are fake but well-formed):
  音乃瀬奏        album artist, tags carry M_KANADE  -> bootstrap links it to M_KANADE
  Otonose Kanade  album artist + collab contributor, tags carry M_KANADE -> MUST merge into 音乃瀬奏
  Other Artist    album artist, M_OTHER, no linked artist                -> no_target
  Some Band       no MBIDs at all                                        -> no_mbid
  Conflict Name   two tracks with different MBIDs                        -> conflict
  Lonely Alias    M_UNLINKED, no linked artist                           -> no_target
"""

import subprocess
import sys
from pathlib import Path

from mutagen.flac import FLAC

ROOT = Path(sys.argv[1])
M_KANADE = "11111111-1111-4111-8111-111111111111"  # stand-in for 音乃瀬奏's MBID
M_OTHER = "22222222-2222-4222-8222-222222222222"
M_CONFLICT_A = "33333333-3333-4333-8333-333333333333"
M_CONFLICT_B = "44444444-4444-4444-8444-444444444444"
M_UNLINKED = "55555555-5555-4555-8555-555555555555"


def flac(path: Path, tags: dict[str, list[str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            "ffmpeg",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "anullsrc=r=44100:cl=stereo",
            "-t",
            "2",
            str(path),
        ],
        check=True,
    )
    audio = FLAC(path)
    for key, values in tags.items():
        audio[key] = values
    audio.save()


def album(
    folder: str,
    album_title: str,
    album_artist: str,
    aa_mbid: str | None,
    tracks: list[tuple[str, list[str], list[str]]],
    extra=None,
) -> None:
    for number, (title, artists, artist_ids) in enumerate(tracks, start=1):
        tags = {
            "TITLE": [title],
            "ALBUM": [album_title],
            "ALBUMARTIST": [album_artist],
            "ARTIST": [" & ".join(artists)],
            "ARTISTS": artists,
            "TRACKNUMBER": [str(number)],
            "DATE": ["2024"],
        }
        if aa_mbid:
            tags["MUSICBRAINZ_ALBUMARTISTID"] = [aa_mbid]
        if artist_ids:
            tags["MUSICBRAINZ_ARTISTID"] = artist_ids
        tags.update(extra or {})
        flac(ROOT / folder / f"{number:02d} {title}.flac", tags)


# 1. Canonical-name album: the artist that will hold the MusicBrainz link.
album(
    "音乃瀬奏/Canonical Album",
    "Canonical Album",
    "音乃瀬奏",
    M_KANADE,
    [("Song A", ["音乃瀬奏"], [M_KANADE]), ("Song B", ["音乃瀬奏"], [M_KANADE])],
)
# 2. Romanized album, artist MBIDs only: becomes the local-only duplicate.
album(
    "Otonose Kanade/Romanized Album",
    "Romanized Album",
    "Otonose Kanade",
    M_KANADE,
    [
        ("Song C", ["Otonose Kanade"], [M_KANADE]),
        ("Song D", ["Otonose Kanade"], [M_KANADE]),
        ("Song E", ["Otonose Kanade"], [M_KANADE]),
    ],
)
# 3. Collab where the romanized artist is only a track contributor (position 1).
album(
    "Other Artist/Collab Single",
    "Collab Single",
    "Other Artist",
    M_OTHER,
    [("Duet", ["Other Artist", "Otonose Kanade"], [M_OTHER, M_KANADE])],
)
# 4. Local-only artist with no MBIDs at all: must be ignored.
album("Some Band/Untagged", "Untagged", "Some Band", None, [("Plain", ["Some Band"], [])])
# 5. Conflicting MBIDs across one artist's files: must be skipped and reported.
album(
    "Conflict Name/Conflict Album",
    "Conflict Album",
    "Conflict Name",
    None,
    [("X1", ["Conflict Name"], [M_CONFLICT_A]), ("X2", ["Conflict Name"], [M_CONFLICT_B])],
)
# 6. MBID present but no linked artist exists for it: report only.
album(
    "Lonely Alias/Lonely Album",
    "Lonely Album",
    "Lonely Alias",
    M_UNLINKED,
    [("L1", ["Lonely Alias"], [M_UNLINKED])],
)
print("library built at", ROOT)
