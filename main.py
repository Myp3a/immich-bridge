import asyncio
from datetime import datetime
from hashlib import sha1
import logging
import os
from time import sleep
from typing import Any, Literal, TypeVar, cast

import requests

from config import IMMICH_KEY, IMMICH_URL

logging.basicConfig(
    format="{asctime} | {levelname:<8} | {name} | {message}",
    style="{",
    level=logging.DEBUG,
)

# breaks log format
import osxphotos  # noqa: E402


_log = logging.getLogger("immich_bridge")

PROCESSED_ALBUM_NAME = "immich_bridge"

SQL_GET_SOURCE_APPS = """
SELECT DISTINCT ZAAA.ZIMPORTEDBYBUNDLEIDENTIFIER
FROM ZASSET
JOIN ZADDITIONALASSETATTRIBUTES ZAAA
    ON ZASSET.ZADDITIONALATTRIBUTES = ZAAA.Z_PK
WHERE ZASSET.ZTRASHEDSTATE = 0;
"""

SQL_GET_IMAGES_PER_APP = """
SELECT DISTINCT ZASSET.ZUUID
FROM ZASSET
JOIN ZADDITIONALASSETATTRIBUTES ZAAA
    ON ZASSET.ZADDITIONALATTRIBUTES = ZAAA.Z_PK
WHERE ZASSET.ZTRASHEDSTATE = 0
    AND ZAAA.ZIMPORTEDBYBUNDLEIDENTIFIER LIKE ?;
"""
SQL_GET_IMAGES_NO_APP = """
SELECT DISTINCT ZASSET.ZUUID
FROM ZASSET
JOIN ZADDITIONALASSETATTRIBUTES ZAAA
    ON ZASSET.ZADDITIONALATTRIBUTES = ZAAA.Z_PK
WHERE ZASSET.ZTRASHEDSTATE = 0
    AND ZAAA.ZIMPORTEDBYBUNDLEIDENTIFIER IS NULL;
"""

APP_TO_ALBUM_MAPPINGS = {
    "ph.telegra.Telegraph": "memes",
    "com.apple.camera": "mabilka",
    "com.apple.springboard": "screenshots",
    "com.apple.sharingd": None,  # AirDrop recv
    "jp.co.canon.ic.cameraconnect": "jpgs",
    "com.apple.Preview": None,
    "com.vk.vkclient": "memes",
    "com.valvesoftware.Steam": "screenshots",
    "org.mozilla.ios.Firefox": "memes",
    "com.adobe.lrmobile": "jpgs",
    "au.com.savageinteractive.procreate": None,
    "pinterest": "memes",
    "com.hammerandchisel.discord": "memes",
    "com.apple.quicklook.UIExtension": None,
    "com.apple.DocumentsApp": "jpgs",
    "com.apple.replayd": "jpgs",  # Files
    "com.google.Gmail": None,
    "app.alextran.immich": -1,
    "com.apple.SafariViewService": None,
    "com.apple.mobilesafari": None,
    "com.apple.mobilenotes": None,
    "com.apple.mobileslideshow": -1,
    "com.miHoYo.GenshinImpact": "screenshots",
    "com.zhiliaoapp.musically": "memes",
    "com.borderleap.patterned": "screenshots",
    "com.apple.WorkflowKit.BackgroundShortcutRunner": None,  # Shortcuts
    None: "mabilka",  # Photos...
}


class Immich:
    def __init__(self) -> None:
        self.sess = requests.Session()
        self.base_url = IMMICH_URL
        self.api_key = IMMICH_KEY

    def _request(
        self,
        method: Literal["GET", "POST", "PUT"],
        url: str,
        json: dict[str, Any] | None = None,
        data: dict[str, Any] | None = None,
        files: dict[str, Any] | None = None,
    ) -> dict[str, Any] | list[dict[str, Any]]:
        _log.info(
            f"> {method} {self.base_url + url} {data if data else json if json else {}}"
        )
        try:
            resp = self.sess.request(
                method,
                self.base_url + url,
                headers={"x-api-key": self.api_key},
                json=json,
                data=data,
                files=files,
            )
        except requests.exceptions.ConnectionError:
            _log.warning("lost connection to server, retrying once")
            sleep(5)
            resp = self.sess.request(
                method,
                self.base_url + url,
                headers={"x-api-key": self.api_key},
                json=json,
                data=data,
                files=files,
            )
        _log.info(f"< {method} {self.base_url + url} {resp.status_code} {resp.text}")
        if not resp.ok:
            raise RuntimeError(
                f"Immich API fail: {method} {url} {resp.status_code}, {resp.text}"
            )
        return resp.json()

    def get(self, url: str) -> dict[str, Any] | list[dict[str, Any]]:
        resp = self._request("GET", url)
        return resp

    def post(
        self,
        url: str,
        json: dict[str, Any] | None = None,
        data: dict[str, Any] | None = None,
        files: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        resp = self._request("POST", url, json=json, data=data, files=files)
        return cast(dict[str, Any], resp)

    def put(self, url: str, json: dict[str, Any] | None) -> dict[str, Any]:
        resp = self._request("PUT", url, json=json)
        return cast(dict[str, Any], resp)

    def album_ids(self) -> dict[str, str]:
        albums = cast(list[dict[str, Any]], self.get("/albums"))
        album_ids = {a["albumName"]: a["id"] for a in albums}
        return album_ids

    def upload(
        self,
        binary_data: bytes,
        filename: str,
        photo_id: str,
        date_created: datetime,
        live_uuid: str | None = None,
    ) -> str:
        data = {
            "deviceAssetId": photo_id,
            "deviceId": "immich-icloud",
            "fileCreatedAt": date_created.isoformat(),
            "fileModifiedAt": date_created.isoformat(),
            "metadata": {},
        }
        if live_uuid:
            data["livePhotoVideoId"] = live_uuid
        resp = self.post(
            "/assets", data=data, files={"assetData": (filename, binary_data)}
        )
        res_id = resp["id"]
        return res_id

    def add_to_album(self, album_id: str, photo_id: str) -> None:
        self.put("/albums/assets", {"albumIds": [album_id], "assetIds": [photo_id]})

    def exists_on_server(self, binary_data: bytes, filename: str) -> str | None:
        h = sha1()
        h.update(binary_data)
        resp = self.post(
            "/assets/bulk-upload-check",
            json={"assets": [{"checksum": h.hexdigest(), "id": filename}]},
        )
        if (
            resp["results"][0]["action"] == "reject"
            and resp["results"][0]["reason"] == "duplicate"
        ):
            return resp["results"][0]["assetId"]
        elif resp["results"][0]["action"] == "accept":
            return None
        else:
            raise RuntimeError("Immich API fail: unknown existence state")


R = TypeVar("R")


class MacPhotos:
    def __init__(self) -> None:
        self.db = osxphotos.PhotosDB()
        conn, self.cur = self.db.get_db_connection()
        self.processed_album = osxphotos.photosalbum.PhotosAlbum(PROCESSED_ALBUM_NAME)

    def app_ids(self) -> list[str]:
        self.cur.execute(SQL_GET_SOURCE_APPS)
        ids = [r[0] for r in self.cur.fetchall()]
        return ids

    def app_image_ids(self, app_id: str | None) -> list[str]:
        if not app_id:
            self.cur.execute(SQL_GET_IMAGES_NO_APP)
        else:
            self.cur.execute(SQL_GET_IMAGES_PER_APP, (app_id,))
        ids = [r[0] for r in self.cur.fetchall()]
        return ids

    def image_info(self, uuid: str) -> osxphotos.PhotoInfo:
        p = self.db.get_photo(uuid)
        if not p:
            raise RuntimeError(f"macOS Photos fail: no photo {uuid}")
        return p

    def live_path(self, photo: osxphotos.PhotoInfo) -> str | None:
        if not photo.live_photo:
            return None
        video = photo.path_live_photo
        if not video:
            raise RuntimeError(
                f"macOS Photos fail: live exists, but no data on {photo.uuid}"
            )
        return video

    def capture_date(self, photo: osxphotos.PhotoInfo) -> datetime:
        # save date for memes
        if photo.exif_info is None:
            raise RuntimeError(f"macOS Photos fail: no exif on {photo.uuid}")
        if photo.exif_info.date is None:
            return photo.date
        return photo.exif_info.date

    def processed(self, photo: osxphotos.PhotoInfo) -> bool:
        albums = photo.albums
        return PROCESSED_ALBUM_NAME in albums

    def burst_should_skip(self, photo: osxphotos.PhotoInfo) -> bool:
        return photo.burst and not photo.burst_key

    def add_to_processed(self, photo: osxphotos.PhotoInfo) -> None:
        self.processed_album.add(photo)

    def filenames(self, photo: osxphotos.PhotoInfo) -> list[str]:
        names = [photo.filename]
        if photo.live_photo:
            names.append(".".join(photo.filename.split(".")[:-1] + ["mov"]))
        return names

    def export(self, photo: osxphotos.PhotoInfo) -> list[str]:
        export = (
            osxphotos.PhotoExporter(photo)
            .export(
                "./tmp/",
                options=osxphotos.ExportOptions(
                    download_missing=True, live_photo=True, verbose=_log.info
                ),
            )
            .exported
        )
        return export

    async def fetch_photo_full_data(self, photo_uuid: str) -> dict[str, Any]:
        photo = self.image_info(photo_uuid)
        save_date = self.capture_date(photo)
        retries = 3
        export = []
        while retries > 0:
            export = self.export(photo)
            if not export:
                retries -= 1
                _log.warning(f"not exported anything, retries left: {retries}")
                sleep(5)
            else:
                break
        if photo.isphoto:
            res = {
                "type": "photo",
                "photo_path": next(
                    p
                    for p in export
                    if p.lower().endswith(
                        (
                            ".jpg",
                            ".jpeg",
                            ".heic",
                            ".png",
                            ".gif",
                            ".cr2",
                            ".cr3",
                            ".webp",
                        )
                    )
                ),
                "live_path": next(
                    (p for p in export if p.lower().endswith((".mov"))), None
                ),
                "date": save_date,
                "filename": photo.filename,
            }
        elif photo.ismovie:
            res = {
                "type": "video",
                "video_path": next(
                    p for p in export if p.lower().endswith((".mov", ".mp4"))
                ),
                "date": save_date,
                "filename": photo.filename,
            }
        else:
            raise RuntimeError(
                f"macOS Photos fail: {photo.uuid} is neither a photo or video"
            )
        return res


just_export = False
ignore_processed = False
apps_mask: list[str] = []


async def main():
    mac = MacPhotos()
    app_ids = mac.app_ids()
    for app in app_ids:
        if app not in APP_TO_ALBUM_MAPPINGS:
            _log.warning(f"unknown app {app}")
    if apps_mask:
        for app in list(apps_mask):
            if app not in app_ids:
                apps_mask.remove(app)
        app_ids = apps_mask
    immich = Immich()
    albums = immich.album_ids()
    for app in app_ids:
        _log.info(f"backing up {app}")
        if APP_TO_ALBUM_MAPPINGS[app] == -1:
            _log.warning(f"app {app} set as ignored, skipping")
            continue
        if APP_TO_ALBUM_MAPPINGS[app] is None:
            _log.warning(f"app {app} set as None, uploading but not adding to albums")
        img_ids = mac.app_image_ids(app)
        for img in img_ids:
            img_obj = mac.image_info(img)
            if mac.processed(img_obj) and not ignore_processed:
                continue
            _log.info(f"processing pic {img}")
            if mac.burst_should_skip(img_obj):
                _log.info("part of burst, but not key, skipping")
                continue
            img_data = await mac.fetch_photo_full_data(img)
            if just_export:
                continue
            if img_data["type"] == "photo":
                _log.info("it is photo")
                with open(img_data["photo_path"], "rb") as inph:
                    photo_data = inph.read()
                if img_data["live_path"]:
                    _log.info("with live data")
                    with open(img_data["live_path"], "rb") as inlv:
                        live_data = inlv.read()
                else:
                    live_data = None
                if live_data:
                    live_filename = ".".join(
                        img_data["filename"].split(".")[:-1] + ["mov"]
                    )
                    if (
                        live_id := immich.exists_on_server(live_data, live_filename)
                    ) is None:
                        live_id = immich.upload(
                            live_data, live_filename, img + "lv", img_data["date"]
                        )
                else:
                    live_id = None
                if (
                    new_id := immich.exists_on_server(photo_data, img_data["filename"])
                ) is None:
                    new_id = immich.upload(
                        photo_data,
                        img_data["filename"],
                        img + "ph",
                        img_data["date"],
                        live_id,
                    )
                os.remove(img_data["photo_path"])
                if live_data:
                    os.remove(img_data["live_path"])
            elif img_data["type"] == "video":
                _log.info("it is video")
                with open(img_data["video_path"], "rb") as invid:
                    vid_data = invid.read()
                if (
                    new_id := immich.exists_on_server(vid_data, img_data["filename"])
                ) is None:
                    new_id = immich.upload(
                        vid_data, img_data["filename"], img + "vd", img_data["date"]
                    )
                os.remove(img_data["video_path"])
            else:
                raise RuntimeError("never")
            if APP_TO_ALBUM_MAPPINGS[app] is not None:
                immich.add_to_album(albums[APP_TO_ALBUM_MAPPINGS[app]], new_id)
            mac.add_to_processed(img_obj)


if __name__ == "__main__":
    asyncio.run(main())
