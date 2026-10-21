"""Ava's music library: a tag index of your music folders, playlists built from it, and saved playlists.

    vocab.py      genre aliases (tags + speech), moods, eras, name keys for fuzzy matching
    index.py      the SQLite tag index (mutagen), built in the background, incremental, resumable
    spec.py       "some sad 90s rap" / "the album ADHD by Joyner Lucas" -> a MusicSpec (pure, no I/O)
    intent.py     the music commands she understands (play / save / add / rename / skip this artist ...)
    picker.py     finding artists / albums and picking + ordering tracks for a spec
    playlists.py  .m3u8 files: the list playing now, saved playlists, what's playing
    player.py     who plays the lists: Winamp on the desktop (the default), or a service's own player per room
    winamp.py     Winamp's own messages: play state, pause/resume, the current file
    ops.py        the operations the router's steps run

Ava only READS your music files (their tags). Playlists are .m3u8 lists of paths in her own data folder;
nothing in your music folders is ever written, moved or deleted.
"""
