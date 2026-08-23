FamilyNAS Dropbox — How To Use This Folder
===========================================

Drop files in the right subfolder below and they'll be automatically
imported within a few seconds. You don't need to do anything else —
no uploading through an app, no manual organizing afterward.

  photos/
      Drop any photo or video here (phone camera roll, screenshots,
      old scans, whatever). It gets uploaded straight into Immich
      (photos.local) under YOUR account, so it shows up as uploaded
      by you specifically.

  photos/favorites/
      Same as photos/, but also gets tagged as a Favorite in Immich.
      Handy for pulling a "best of" set later (e.g. yearly calendar
      photos) without having to go re-favorite things by hand.

  media/movies/
      Drop a movie file here. It lands in the Jellyfin Movies library
      (media.local) and the library refreshes automatically.

  media/tv/
      Drop a TV episode file here. It lands in the Jellyfin TV Shows
      library. Jellyfin does its own name/season/episode matching from
      the filename, so name the file close to how the show is really
      titled if you can (e.g. "Show.Name.S01E02.mkv" or
      "Show Name - 1x02 - Episode Title.mkv") — it's more forgiving
      than it looks, but exact numbering helps it match correctly.

  media/music/
      Drop a song or album track here. It lands in the Jellyfin Music
      library.

A few things worth knowing:

  - Supported video types: mp4, mkv, avi, mov, m4v, wmv, webm
    Supported audio types: mp3, m4a, flac, wav, aac, ogg, opus
  - If you drop a video/audio file loose in media/ without using the
    movies/tv/music subfolder, it'll do its best guess (audio -> music,
    video -> movies) — but it can't tell a movie from a TV episode that
    way, so please use the subfolders when you can.
  - Photos get moved into archive/photos/<date>/ once successfully
    uploaded — that's just a safety-net copy in case anything ever
    needs re-checking; the real copy lives in Immich itself.
  - Anything that fails to import (bad file type, permissions issue,
    etc.) gets moved into failed/ instead of silently disappearing or
    getting deleted — check there if something you dropped doesn't
    show up.
  - Attribution is automatic: whichever family Samba login you connect
    with is who the photo/video gets credited to in Immich. No login
    needed for Jellyfin drops — those are shared libraries for everyone.

Questions or something not importing right? Ask Igor.
