# Immich iCloud Bridge
A small script to copy images from iCloud to Immich, preserving metadata and source app album.
> Pretty complete, never production-ready, delivered as-is.

# Config
> Must be run on macOS!  
> Recommended: enable `Download Originals to this Mac` in Photos settings.
1. Rename `config.py.example` to `config.py`
2. Fill Immich API URL
3. Create and fill API key - permissions are `asset.read`, `asset.update`, `asset.upload`, `album.read` and `albumAsset.create`
4. Edit `APP_TO_ALBUM_MAPPINGS` in `main.py` according to your apps
5. Create albums in Immich
6. Run `uv run main.py` to sync photos

# Caveats
- Burst photos imported as single selected photo
- Photos app can hang sometimes - not yet found the cause
- Can't delete from iCloud - must do that manually. For convience, processed photos are put to the iCloud album `immich_bridge`
