# Immich iCloud Bridge
A small script to copy images from iCloud to Immich, preserving metadata and source app album.
> Pretty complete, never production-ready, delivered as-is.

# Config
1. Rename `config.py.example` to `config.py`
2. Fill Immich API URL
3. Create and fill API key with "all" permissions
4. Fill iCloud username (email) and password
5. Launch the script interactively - it will prompt for 2FA code
6. Edit `APP_TO_ALBUM_MAPPINGS` in `main.py` according to your apps
7. Create albums in Immich
8. Run `uv run sync` to sync photos

# Caveats
- Requires "all" API key for Immich 3.2.2, Immich bug in permissions handling. Expected permissions are `asset.read`, `asset.update`, `asset.upload`, `album.read`, `albumAsset.create`, `notification.create`, `notification.read`, `user.read`.
- No checks if the photo was modified / part of burst; TODO
- No check if similar notification was already sent; TODO
- No iCloud deletion to save space; left as-is to prevent data loss, just manually delete all items in `immich_bridge` album; possibly TODO behind feature flag