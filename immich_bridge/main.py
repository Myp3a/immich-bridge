import asyncio
import json
import logging
import os
import sys
from base64 import b64decode, b64encode
from collections.abc import AsyncIterator
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from hashlib import pbkdf2_hmac, sha1, sha256
from json import JSONDecodeError
from time import sleep
from typing import Any, Literal, Self, cast

import httpx
import srp

from config import ICLOUD_LOGIN, ICLOUD_PASS, IMMICH_KEY, IMMICH_URL

logging.basicConfig(
    format="{asctime} | {levelname:<8} | {name} | {message}",
    style="{",
    level=logging.INFO,
)

MMCS_SALT = b"com.apple.XattrObjectSalt\x00com.apple.DataObjectSalt\x00"
_log = logging.getLogger("immich_bridge")


PROCESSED_ALBUM_NAME = "immich_bridge"
ASSETS_DIR = "assets"
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


@dataclass
class MediaFile:
    filename: str
    url: str
    expected_size: int
    expected_hash: str

    @property
    def full_path(self) -> str:
        return f"{ASSETS_DIR}/{self.filename}"

    def validate(self) -> bool:
        if not os.path.exists(self.full_path):
            return False
        if os.path.getsize(self.full_path) != self.expected_size:
            return False
        with open(self.full_path, "rb") as inf:
            hash = sha1(MMCS_SALT)
            while chunk := inf.read(8192):
                hash.update(chunk)
            if b64encode(b"\x01" + hash.digest()).decode() != self.expected_hash:
                return False
        return True

    def hash(self) -> str:
        with open(self.full_path, "rb") as inf:
            hash = sha1()
            while chunk := inf.read(8192):
                hash.update(chunk)
            return hash.hexdigest()

    def remove(self) -> None:
        if os.path.exists(self.full_path):
            os.remove(self.full_path)


@dataclass
class MediaItem:
    original: MediaFile
    live: MediaFile | None
    created_date: datetime
    imported_by: str | None
    asset_id: str

    @classmethod
    def from_icloud(cls, master_record: dict[str, Any], asset_record: dict[str, Any]) -> Self:
        filename = b64decode(master_record["fields"]["filenameEnc"]["value"]).decode()
        filename_no_ext = ".".join(filename.split(".")[:-1])
        created_date_field = asset_record["fields"].get("assetDate", None)
        if not created_date_field:
            raise RuntimeError("Asset without creation date")
        tz_offset = timedelta(seconds=asset_record["fields"]["timeZoneOffset"]["value"])
        created_date = datetime.fromtimestamp(created_date_field["value"] / 1000, tz=timezone(tz_offset))
        imported_by = master_record["fields"].get("importedByBundleIdentifierEnc", None)
        if imported_by:
            imported_by = b64decode(imported_by["value"]).decode()
        original_data = master_record["fields"]["resOriginalRes"]["value"]
        original = MediaFile(
            filename=filename,
            url=original_data["downloadURL"],
            expected_size=original_data["size"],
            expected_hash=original_data["fileChecksum"],
        )
        live_field = master_record["fields"].get("resOriginalVidComplRes", {})
        if live_field:
            live = MediaFile(
                filename=f"{filename_no_ext}.mov",
                url=live_field["value"]["downloadURL"],
                expected_size=live_field["value"]["size"],
                expected_hash=live_field["value"]["fileChecksum"],
            )
        else:
            live = None
        asset_id = asset_record["recordName"]
        return cls(
            original=original,
            live=live,
            created_date=created_date,
            imported_by=imported_by,
            asset_id=asset_id,
        )

    def remove(self) -> None:
        if self.live:
            self.live.remove()
        self.original.remove()


class Immich:
    def __init__(self, base_url: str, api_key: str) -> None:
        self.cli = httpx.AsyncClient(timeout=httpx.Timeout(30.0, connect=60.0))
        self.base_url = base_url
        self.api_key = api_key

    async def __aenter__(self):
        await self.cli.__aenter__()
        return self

    async def __aexit__(self, exc_type, exc, tb):
        await self.cli.__aexit__()

    async def _request(
        self,
        method: Literal["GET", "POST", "PUT"],
        url: str,
        json: dict[str, Any] | None = None,
        data: dict[str, Any] | None = None,
        files: dict[str, Any] | None = None,
    ) -> dict[str, Any] | list[dict[str, Any]]:
        _log.debug(f"> {method} {self.base_url + url} {data if data else json if json else {}}")
        try:
            resp = await self.cli.request(
                method,
                self.base_url + url,
                headers={"x-api-key": self.api_key},
                json=json,
                data=data,
                files=files,
            )
        except (httpx.ConnectError, httpx.ReadError, httpx.TransportError):
            _log.warning("lost connection to server, retrying once")
            sleep(5)
            resp = await self.cli.request(
                method,
                self.base_url + url,
                headers={"x-api-key": self.api_key},
                json=json,
                data=data,
                files=files,
            )
        _log.debug(f"< {method} {self.base_url + url} {resp.status_code} {resp.text}")
        if not resp.is_success:
            raise RuntimeError(f"Immich API fail: {method} {url} {resp.status_code}, {resp.text}")
        return resp.json()

    async def get(self, url: str) -> dict[str, Any] | list[dict[str, Any]]:
        resp = await self._request("GET", url)
        return resp

    async def post(
        self,
        url: str,
        json: dict[str, Any] | None = None,
        data: dict[str, Any] | None = None,
        files: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        resp = await self._request("POST", url, json=json, data=data, files=files)
        return cast(dict[str, Any], resp)

    async def put(self, url: str, json: dict[str, Any] | None) -> dict[str, Any]:
        resp = await self._request("PUT", url, json=json)
        return cast(dict[str, Any], resp)

    async def album_ids(self) -> dict[str, str]:
        albums = cast(list[dict[str, Any]], await self.get("/albums"))
        album_ids = {a["albumName"]: a["id"] for a in albums}
        return album_ids

    async def upload(self, media: MediaItem, to_album: str | None = None) -> str:
        if media.live and not (live_id := await self.exists_on_server(media.live)):
            _log.info(f"Uploading {media.live.filename}")
            with open(media.live.full_path, "rb") as inf:
                live_resp = await self.post(
                    "/assets",
                    data={
                        "fileCreatedAt": media.created_date.isoformat(),
                        "fileModifiedAt": media.created_date.isoformat(),
                    },
                    files={"assetData": (media.live.filename, inf)},
                )
                live_id = live_resp["id"]
        else:
            live_id = None
        if not (original_id := await self.exists_on_server(media.original)):
            _log.info(f"Uploading {media.original.filename}")
            data = {
                "fileCreatedAt": media.created_date.isoformat(),
                "fileModifiedAt": media.created_date.isoformat(),
            }
            if live_id:
                data["livePhotoVideoId"] = live_id
            with open(media.original.full_path, "rb") as inf:
                resp = await self.post("/assets", data=data, files={"assetData": (media.original.filename, inf)})
            res_id = resp["id"]
            if to_album:
                await self.add_to_album(to_album, res_id)
            return res_id
        return original_id

    async def add_to_album(self, album_id: str, photo_id: str) -> None:
        await self.put("/albums/assets", {"albumIds": [album_id], "assetIds": [photo_id]})

    async def exists_on_server(self, media: MediaFile) -> str | None:
        resp = await self.post(
            "/assets/bulk-upload-check",
            json={"assets": [{"checksum": media.hash(), "id": media.filename}]},
        )
        if resp["results"][0]["action"] == "reject" and resp["results"][0]["reason"] == "duplicate":
            return resp["results"][0]["assetId"]
        elif resp["results"][0]["action"] == "accept":
            return None
        else:
            raise RuntimeError("Immich API fail: unknown existence state")

    async def me(self) -> str:
        data = cast(dict[str, Any], await self.get("/users/me"))
        return data["id"]

    async def notify(self, level: Literal["info", "error", "success", "warning"], text: str) -> None:
        my_id = await self.me()
        await self.post(
            "/admin/notifications", json={"title": "iCloud sync", "userId": my_id, "description": text, "level": level}
        )


@dataclass
class iCloudSession:
    cookies: list[tuple[str, str, str, str]]
    account_country_code: str
    ds_web_auth_token: str
    trust_token: str
    session_id: str
    scnt: str

    @classmethod
    def load(cls) -> Self:
        obj = cls(cookies=[], account_country_code="", ds_web_auth_token="", trust_token="", session_id="", scnt="")
        try:
            with open("icloud.session", "r") as data:
                js = json.load(data)
                obj.cookies = js.get("cookies", [])
                obj.account_country_code = js.get("account_country_code", "")
                obj.ds_web_auth_token = js.get("ds_web_auth_token", "")
                obj.trust_token = js.get("trust_token", "")
                obj.session_id = js.get("session_id", "")
                obj.scnt = js.get("scnt", "")
        except FileNotFoundError:
            pass
        return obj

    def save(self) -> None:
        with open("icloud.session", "w") as data:
            json.dump(asdict(self), data)


class iCloudPhotos:
    AuthState = Literal["active", "needs_validation", "needs_token_login", "needs_full_login", "needs_2fa"]

    def __init__(self, login: str, password: str) -> None:
        self.cli = httpx.AsyncClient(timeout=httpx.Timeout(30.0, connect=60.0))
        self.login = login
        self.password = password
        self.retry_count = 3
        self.state: iCloudPhotos.AuthState = "needs_validation"
        self.session = iCloudSession.load()
        for cookie in self.session.cookies:
            self.cli.cookies.set(cookie[0], cookie[1], cookie[2], cookie[3])
        self.db_url = ""
        self.db_get_params = {
            "remapEnums": True,
            "getCurrentSyncToken": True,
        }
        self.page_size = 100

    async def __aenter__(self):
        await self.cli.__aenter__()
        return self

    async def __aexit__(self, exc_type, exc, tb):
        await self.cli.__aexit__()

    def _dict_to_str(self, data: dict[str, Any] | httpx.Headers) -> str:
        return f"{{\n    {'\n    '.join(f'"{k}": {v}' for k, v in data.items())}\n}}"

    def _headers(self) -> dict[str, str]:
        headers = {
            "Accept": "application/json, text/javascript",
            "Content-Type": "application/json",
            "X-Apple-OAuth-Client-Id": "d39ba9916b7251055b22c7f910e2ea796ee65e98b2ddecea8f5dde8d9d1a815d",
            "X-Apple-OAuth-Client-Type": "firstPartyAuth",
            "X-Apple-OAuth-Redirect-URI": "https://www.icloud.com",
            "X-Apple-OAuth-Require-Grant-Code": "true",
            "X-Apple-OAuth-Response-Mode": "web_message",
            "X-Apple-OAuth-Response-Type": "code",
            "X-Apple-OAuth-State": "immich-bridge",
            "X-Apple-Widget-Key": "d39ba9916b7251055b22c7f910e2ea796ee65e98b2ddecea8f5dde8d9d1a815d",
            "Origin": "https://www.icloud.com",
            "Referer": "https://www.icloud.com/",
        }
        if self.session.session_id:
            headers["X-Apple-ID-Session-Id"] = self.session.session_id
        if self.session.scnt:
            headers["scnt"] = self.session.scnt
        return headers

    async def _request_with_retry(
        self,
        method: Literal["GET", "POST", "PUT"],
        url: str,
        headers: dict[str, str] | None = None,
        params: dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
        check_ok: bool = True,
    ) -> httpx.Response:
        for _ in range(self.retry_count):
            resp = await self._request(method, url, headers, params, json)
            if resp.is_success:
                return resp
            if resp.status_code == 403 or resp.status_code == 401:
                # Auth expired
                self.state = "needs_validation"
                await self.authenticate()
                continue
            # TODO: 429, etc.
            if check_ok:
                resp.raise_for_status()
            return resp
        raise RuntimeError("Failed to renew auth")

    async def _request(
        self,
        method: Literal["GET", "POST", "PUT"],
        url: str,
        headers: dict[str, str] | None = None,
        params: dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
    ) -> httpx.Response:
        req_headers = self._headers()
        if headers:
            req_headers |= headers

        _log.debug(f"> {method} {url}")
        _log.debug(f"headers:\n{self._dict_to_str(req_headers)}")
        if params:
            _log.debug(f"url params:\n{self._dict_to_str(params)}")
        if json:
            _log.debug(f"body:\n{self._dict_to_str(json)}")
        resp = await self.cli.request(
            method,
            url,
            params=params,
            json=json,
            headers=req_headers,
        )
        _log.debug(f"< {resp.status_code} {url}")
        _log.debug(f"headers:\n{self._dict_to_str(resp.headers)}")
        try:
            resp_debug = self._dict_to_str(resp.json())
        except JSONDecodeError:
            resp_debug = resp.text
        _log.debug(f"body:\n{resp_debug}")

        session_changed = False
        if (cntr := resp.headers.get("X-Apple-ID-Account-Country")) and cntr != self.session.account_country_code:
            self.session.account_country_code = cntr
            session_changed = True
        if (sess_tok := resp.headers.get("X-Apple-Session-Token")) and sess_tok != self.session.ds_web_auth_token:
            self.session.ds_web_auth_token = sess_tok
            session_changed = True
        if (trust_tok := resp.headers.get("X-Apple-TwoSV-Trust-Token")) and trust_tok != self.session.trust_token:
            self.session.trust_token = trust_tok
            session_changed = True
        if (sess_id := resp.headers.get("X-Apple-ID-Session-Id")) and sess_id != self.session.session_id:
            self.session.session_id = sess_id
            session_changed = True
        if (scnt := resp.headers.get("scnt")) and scnt != self.session.scnt:
            self.session.scnt = scnt
            session_changed = True

        if resp.headers.get("Set-Cookie"):
            session_changed = True
            self.session.cookies = []
            for cookie in self.cli.cookies.jar:
                if cookie.value:
                    self.session.cookies.append((cookie.name, cookie.value, cookie.path, cookie.domain))

        if session_changed:
            self.session.save()
        return resp

    async def stream_file(self, url: str, headers: dict[str, str] | None = None) -> AsyncIterator[bytes]:
        req_headers = self._headers()
        if headers:
            req_headers |= headers

        _log.debug(f"> (file) GET {url}")
        _log.debug(f"headers:\n{self._dict_to_str(req_headers)}")
        async with self.cli.stream("GET", url, headers=req_headers) as resp:
            async for data in resp.aiter_bytes():
                yield data

    async def get(self, url: str) -> dict[str, Any] | None:
        resp = await self._request_with_retry("GET", url)
        if resp.status_code == 204:
            return None
        return resp.json()

    async def post(
        self,
        url: str,
        headers: dict[str, str] | None = None,
        params: dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
        check_ok: bool = True,
    ) -> dict[str, Any] | None:
        resp = await self._request_with_retry("POST", url, headers=headers, params=params, json=json, check_ok=check_ok)
        if resp.status_code == 204:
            return None
        return resp.json()

    async def put(self, url: str, headers: dict[str, str] | None = None) -> dict[str, Any]:
        resp = await self._request_with_retry("PUT", url, headers=headers)
        return resp.json()

    async def login_full(self) -> None:
        class SRPPass:
            def __init__(self, password: str) -> None:
                self.password = password

            def set_params(self, protocol: str, salt: bytes, iterations: int) -> None:
                self.protocol = protocol
                self.salt = salt
                self.iterations = iterations

            def encode(self) -> bytes:
                pwd_hash = sha256(self.password.encode())
                if self.protocol == "s2k_fo":
                    pwd_digest = pwd_hash.hexdigest().encode()
                else:
                    pwd_digest = pwd_hash.digest()
                return pbkdf2_hmac("sha256", pwd_digest, self.salt, self.iterations, 32)

        srp.rfc5054_enable()
        srp.no_username_in_x()
        password = SRPPass(self.password)
        user = srp.User(self.login, password, hash_alg=srp.SHA256, ng_type=srp.NG_2048)
        username, a = user.start_authentication()
        data = {
            "a": b64encode(a).decode(),
            "accountName": username,
            "protocols": ["s2k", "s2k_fo"],
        }
        idmsa_headers = {"Origin": "https://idmsa.apple.com", "Referer": "https://idmsa.apple.com/"}
        resp = await self.post("https://idmsa.apple.com/appleauth/auth/signin/init", json=data, headers=idmsa_headers)
        assert resp
        salt = b64decode(resp["salt"])
        b = b64decode(resp["b"])
        c = resp["c"]
        iterations = resp["iteration"]
        protocol = resp["protocol"]
        password.set_params(protocol, salt, iterations)
        m1 = user.process_challenge(salt, b)
        m2 = user.H_AMK
        assert m1 and m2
        data = {
            "accountName": username,
            "c": c,
            "m1": b64encode(m1).decode(),
            "m2": b64encode(m2).decode(),
            "rememberMe": True,
            "trustTokens": [tkn] if (tkn := self.session.trust_token) else [],
        }
        # 409 with empty body
        resp = await self._request_with_retry(
            "POST",
            "https://idmsa.apple.com/appleauth/auth/signin/complete?isRememberMeEnabled=true",
            json=data,
            headers=idmsa_headers,
            check_ok=False,
        )
        js = resp.json()
        if resp.status_code == 409 and js.get("authType", "") == "hsa2":
            # 2FA required, login/pass valid
            self.state = "needs_token_login"
            await self.login_token()
        else:
            # Hardware 2FA / no 2FA / etc. - no way to test
            raise RuntimeError(f"Apple full auth failed. Auth response: {resp}")

    async def login_token(self) -> None:
        data = {
            "accountCountryCode": self.session.account_country_code,
            "dsWebAuthToken": self.session.ds_web_auth_token,
            "extended_login": True,
            "trustToken": self.session.trust_token,
        }
        resp = await self.post("https://setup.icloud.com/setup/ws/1/accountLogin", json=data)
        assert resp
        if (
            resp.get("dsInfo", {}).get("hsaVersion") == 2
            and resp.get("dsInfo", {}).get("hasICloudQualifyingDevice")
            and resp.get("hsaChallengeRequired")
            and not resp.get("hsaTrustedBrowser")
        ):
            self.state = "needs_2fa"
            if sys.stdin.isatty():
                # Running interactively: prompt for code and save it
                await self.send_2fa_push()
                code = input("Enter Apple 2FA code (no dashes, no spaces): ")
                await self.enter_2fa_code(code)
                await self.trust_session()
                # 2FA done, reauth to get new headers
                await self.login_token()
            else:
                # No interactive input: fail with error
                raise RuntimeError("Apple auth requires 2FA: run the script in interactive mode to enter it")
        elif (
            resp.get("dsInfo", {}).get("hsaVersion") == 2
            and resp.get("dsInfo", {}).get("hasICloudQualifyingDevice")
            and not resp.get("hsaChallengeRequired")
            and resp.get("hsaTrustedBrowser")
        ):
            # Valid session after 2FA
            self.state = "active"
            db_url = resp.get("webservices", {}).get("ckdatabasews", {}).get("url", "")
            if db_url:
                self.db_url = f"{db_url}/database/1/com.apple.photos.cloud/production/private"
            else:
                raise RuntimeError("Can't get iCloud DB URL after successful auth")
            return
        else:
            raise RuntimeError("Apple auth requires 2FA, but the method to do that is unknown")

    async def validate_session(self) -> bool:
        try:
            resp = await self.post("https://setup.icloud.com/setup/ws/1/validate")
            assert resp
            db_url = resp.get("webservices", {}).get("ckdatabasews", {}).get("url", "")
            if db_url:
                self.db_url = f"{db_url}/database/1/com.apple.photos.cloud/production/private"
            else:
                raise RuntimeError("Can't get iCloud DB URL after successful validation")
        except httpx.HTTPStatusError:
            self.state = "needs_token_login"
            return False
        self.state = "active"
        return True

    async def send_2fa_push(self) -> None:
        await self.put(
            "https://idmsa.apple.com/appleauth/auth/verify/trusteddevice/securitycode",
            headers={"Accept": "application/json"},
        )

    async def enter_2fa_code(self, code: str) -> None:
        data = {
            "securityCode": {"code": code},
        }
        await self.post("https://idmsa.apple.com/appleauth/auth/verify/trusteddevice/securitycode", json=data)

    async def trust_session(self) -> None:
        await self.get("https://idmsa.apple.com/appleauth/auth/2sv/trust")

    async def authenticate(self) -> None:
        session_valid = await self.validate_session()
        if session_valid:
            _log.info("Session valid, no reauth needed")
            return
        _log.info("Session invalidated, reauth using token")
        await self.login_token()
        if self.state == "active":
            _log.info("Reauth with token success")
            return
        _log.info("Token reauth failed, using full password reauth")
        await self.login_full()
        if self.state == "active":
            _log.info("Reauth with pass success")
            return
        _log.info("Reauth failed, 2FA may be required")

    async def albums(self) -> dict[str, dict[str, str]]:
        resp = await self.post(
            f"{self.db_url}/records/query",
            params=self.db_get_params,
            json={
                "query": {
                    "recordType": "CPLAlbumByPositionLive",
                },
                "zoneID": {"zoneName": "PrimarySync"},
            },
        )
        assert resp
        albums = {}
        for alb_data in resp["records"]:
            name = alb_data["fields"].get("albumNameEnc", "")
            if not name:
                continue
            albums[b64decode(name["value"]).decode()] = {
                "id": alb_data["recordName"],
                "change_tag": alb_data["recordChangeTag"],
            }
        return albums

    async def album_asset_ids(self, album_id: str) -> list[str]:
        assets = []
        offset = 0
        while True:
            resp = await self.post(
                f"{self.db_url}/records/query",
                params=self.db_get_params,
                json={
                    "query": {
                        "filterBy": [
                            {
                                "fieldName": "startRank",
                                "fieldValue": {"type": "INT64", "value": offset},
                                "comparator": "EQUALS",
                            },
                            {
                                "fieldName": "direction",
                                "fieldValue": {"type": "STRING", "value": "ASCENDING"},
                                "comparator": "EQUALS",
                            },
                            {
                                "fieldName": "parentId",
                                "fieldValue": {"type": "STRING", "value": album_id},
                                "comparator": "EQUALS",
                            },
                        ],
                        "recordType": "CPLContainerRelationLiveByAssetDate",
                    },
                    "resultsLimit": self.page_size * 3,
                    "zoneID": {"zoneName": "PrimarySync"},
                },
            )
            assert resp
            if not resp["records"]:
                break
            for record in resp["records"]:
                if record["recordType"] == "CPLContainerRelation":
                    assets.append(record["fields"]["itemId"]["value"])
                    offset += 1
        return assets

    async def add_to_album(self, album_id: str, asset_id: str) -> None:
        albums = await self.albums()
        target_album = next(a for a in albums.values() if a["id"] == album_id)
        change_tag = target_album["change_tag"]
        await self.post(
            f"{self.db_url}/records/modify",
            params=self.db_get_params,
            json={
                "atomic": True,
                "operations": [
                    {
                        "operationType": "create",
                        "record": {
                            "fields": {
                                "containerId": {"value": album_id},
                                "itemId": {"value": asset_id},
                            },
                            "recordName": f"{asset_id}-IN-{album_id}",
                            "recordType": "CPLContainerRelation",
                        },
                    },
                    {
                        "operationType": "update",
                        "record": {
                            "fields": {
                                "userModificationDate": {"value": int(datetime.now().timestamp() * 1000)},
                            },
                            "recordChangeTag": change_tag,
                            "recordName": album_id,
                            "recordType": "CPLAlbum",
                        },
                    },
                ],
                "zoneID": {"zoneName": "PrimarySync"},
            },
        )

    async def photos(self) -> AsyncIterator[MediaItem]:
        library_offset = 0
        while True:
            resp = await self.post(
                f"{self.db_url}/records/query",
                params=self.db_get_params,
                json={
                    "query": {
                        "filterBy": [
                            {
                                "fieldName": "startRank",
                                "fieldValue": {"type": "INT64", "value": library_offset},
                                "comparator": "EQUALS",
                            },
                            {
                                "fieldName": "direction",
                                "fieldValue": {"type": "STRING", "value": "ASCENDING"},
                                "comparator": "EQUALS",
                            },
                        ],
                        "recordType": "CPLAssetAndMasterByAssetDateWithoutHiddenOrDeleted",
                    },
                    "resultsLimit": self.page_size * 2,
                    "desiredKeys": [
                        "masterRef",
                        "resOriginalRes",
                        "resOriginalFileType",
                        "resOriginalVidComplRes",
                        "resOriginalVidComplFileType",
                        "importedByBundleIdentifierEnc",
                        "filenameEnc",
                        "assetDate",
                        "timeZoneOffset",
                    ],
                    "zoneID": {"zoneName": "PrimarySync"},
                },
            )
            assert resp
            asset_records = {}
            master_records = []
            for record in resp["records"]:
                if record["recordType"] == "CPLAsset":
                    master_id = record["fields"]["masterRef"]["value"]["recordName"]
                    asset_records[master_id] = record
                elif record["recordType"] == "CPLMaster":
                    master_records.append(record)
            if not master_records:
                break
            for record in master_records:
                key = record["recordName"]
                yield MediaItem.from_icloud(record, asset_records[key])
                # yield {"master": record, "asset": asset_records[key]}
                library_offset += 1
                asset_records.pop(key)
            if asset_records:
                raise RuntimeError("Stray asset records left")

    async def download(self, item: MediaItem) -> None:
        if not os.path.isdir(ASSETS_DIR):
            os.makedirs(ASSETS_DIR)
        if not item.original.validate():
            _log.info(f"Downloading {item.original.filename}")
            with open(item.original.full_path, "wb") as outf:
                async for block in self.stream_file(item.original.url):
                    outf.write(block)
            if not item.original.validate():
                raise RuntimeError("Failed to download media!")
        if item.live and not item.live.validate():
            _log.info(f"Downloading {item.live.filename}")
            with open(item.live.full_path, "wb") as outf:
                async for block in self.stream_file(item.live.url):
                    outf.write(block)
            if not item.live.validate():
                raise RuntimeError("Failed to download live media!")


async def main():
    unknown_apps = set()
    processed_pics = 0
    immich = Immich(IMMICH_URL, IMMICH_KEY)
    immich_albums = await immich.album_ids()
    icloud = iCloudPhotos(ICLOUD_LOGIN, ICLOUD_PASS)
    try:
        await icloud.authenticate()
    except RuntimeError:
        await immich.notify("error", "Sync failed: check auth")
        return
    _log.info("iCloud auth success")
    albums = await icloud.albums()
    icloud_target_album_id = albums[PROCESSED_ALBUM_NAME]["id"]
    _log.info(f"Found processed album {PROCESSED_ALBUM_NAME} with ID {icloud_target_album_id}")
    already_processed = await icloud.album_asset_ids(icloud_target_album_id)
    async for pic in icloud.photos():
        if pic.asset_id in already_processed:
            continue
        _log.info(f"Asset {pic.original.filename} at {pic.created_date} from {pic.imported_by}: processing")
        if pic.imported_by not in APP_TO_ALBUM_MAPPINGS:
            _log.warning(f"Unknown source {pic.imported_by}, ignoring")
            unknown_apps.add(pic.imported_by)
            continue
        target_immich_album = APP_TO_ALBUM_MAPPINGS[pic.imported_by]
        if target_immich_album == -1:
            _log.info("Target album is -1: skipping")
            continue
        elif target_immich_album is None:
            _log.info("Target album is None: uploading only")
            target_immich_album_id = None
        else:
            target_immich_album_id = immich_albums[target_immich_album]
        await icloud.download(pic)
        await immich.upload(pic, target_immich_album_id)
        await icloud.add_to_album(icloud_target_album_id, pic.asset_id)
        pic.remove()
        already_processed.append(pic.asset_id)
        _log.info("Uploaded successfully")
        processed_pics += 1
    if processed_pics > 0:
        await immich.notify("success", f"Sync finished; {processed_pics} media added")
        if unknown_apps:
            await immich.notify("warning", f"Sync found unknown apps: {unknown_apps}")


def sync() -> None:
    asyncio.run(main())


if __name__ == "__main__":
    sync()
