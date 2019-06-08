import aiohttp
import logging
import random
import xml.etree.ElementTree as ET

from datetime import datetime
from yarl import URL
from typing import Dict, NewType
from galaxy.api.errors import (
    BackendNotAvailable, BackendTimeout, BackendError, UnknownBackendResponse,
    AccessDenied, AuthenticationRequired, NetworkError
)
from galaxy.http import HttpClient

MasterTitleId = NewType("MasterTitleId", str)
OfferId = NewType("OfferId", str)
Timestamp = NewType("Timestamp", int)


class CookieJar(aiohttp.CookieJar):
    def __init__(self):
        super().__init__()
        self._cookies_updated_callback = None

    def set_cookies_updated_callback(self, callback):
        self._cookies_updated_callback = callback

    def update_cookies(self, cookies, url=URL()):
        super().update_cookies(cookies, url)
        if cookies and self._cookies_updated_callback:
            self._cookies_updated_callback(list(self))


class AuthenticatedHttpClient(HttpClient):
    def __init__(self):
        self._auth_lost_callback = None
        self._cookie_jar = CookieJar()
        self._access_token = None
        super().__init__(cookie_jar=self._cookie_jar)

    def set_auth_lost_callback(self, callback):
        self._auth_lost_callback = callback

    def set_cookies_updated_callback(self, callback):
        self._cookie_jar.set_cookies_updated_callback(callback)

    async def authenticate(self, cookies):
        self._cookie_jar.update_cookies(cookies)
        await self._get_access_token()

    def is_authenticated(self):
        return self._access_token is not None

    async def get(self, *args, **kwargs):
        if not self._access_token:
            raise AccessDenied()

        try:
            return await self._authorized_get(*args, **kwargs)
        except (AuthenticationRequired, AccessDenied):
            # Origin backend returns 403 when the auth token expires
            await self._refresh_token()
            return await self._authorized_get(*args, **kwargs)

    async def _authorized_get(self, *args, **kwargs):
        headers = kwargs.setdefault("headers", {})
        headers["Authorization"] = "Bearer {}".format(self._access_token)
        headers["AuthToken"] = self._access_token
        headers["X-AuthToken"] = self._access_token

        return await super().request("GET", *args, **kwargs)

    async def _refresh_token(self):
        try:
            await self._get_access_token()
        except (BackendNotAvailable, BackendTimeout, BackendError, NetworkError):
            logging.warning("Failed to refresh token for independent reasons")
            raise
        except Exception:
            logging.exception("Failed to refresh token")
            self._access_token = None
            if self._auth_lost_callback:
                self._auth_lost_callback()
            raise AccessDenied()

    async def _get_access_token(self):
        url = "https://accounts.ea.com/connect/auth"
        params = {
            "client_id": "ORIGIN_JS_SDK",
            "response_type": "token",
            "redirect_uri": "nucleus:rest",
            "prompt": "none"
        }
        response = await super().request("GET", url, params=params)

        try:
            data = await response.json(content_type=None)
            self._access_token = data["access_token"]
        except (ValueError, KeyError):
            logging.exception("Can not parse backend response")
            raise UnknownBackendResponse()


class OriginBackendClient:
    def __init__(self, http_client):
        self._http_client = http_client

    @staticmethod
    def _get_api_host():
        return "https://api{}.origin.com".format(random.randint(1, 4))

    async def get_identity(self):
        pid_response = await self._http_client.get(
            "https://gateway.ea.com/proxy/identity/pids/me"
        )
        data = await pid_response.json()
        pid = data["pid"]["pidId"]

        persona_id_response = await self._http_client.get(
            "{}/atom/users?userIds={}".format(self._get_api_host(), pid)
        )
        content = await persona_id_response.text()

        try:
            origin_account_info = ET.fromstring(content)
            persona_id = origin_account_info.find("user").find("personaId").text
            user_name = origin_account_info.find("user").find("EAID").text

            return str(pid), str(persona_id), str(user_name)
        except (ET.ParseError, AttributeError):
            logging.exception("Can not parse backend response")
            raise UnknownBackendResponse()

    async def get_entitlements(self, pid):
        url = "{}/ecommerce2/consolidatedentitlements/{}?machine_hash=1".format(
            self._get_api_host(),
            pid
        )
        headers = {
            "Accept": "application/vnd.origin.v3+json; x-cache/force-write"
        }
        response = await self._http_client.get(url, headers=headers)
        try:
            data = await response.json()
            return data["entitlements"]
        except (ValueError, KeyError):
            logging.exception("Can not parse backend response")
            raise UnknownBackendResponse()

    async def get_offer(self, offer_id):
        url = "{}/ecommerce2/public/supercat/{}/{}".format(
            self._get_api_host(),
            offer_id,
            "en_US"
        )
        response = await self._http_client.get(url)
        try:
            return await response.json()
        except ValueError:
            logging.exception("Can not parse backend response")
            raise UnknownBackendResponse()

    async def get_achievements(self, persona_id: str, achievement_sets: Dict[str, str]):
        response = await self._http_client.get(
            "https://achievements.gameservices.ea.com/achievements/personas/{persona_id}/all".format(
                persona_id=persona_id
            ),
            params={
                "lang": "en_US",
                "metadata": "true"
            }
        )
        try:
            data = await response.json()
            return {
                offer_id: data[achievement_set]["achievements"]
                for offer_id, achievement_set in achievement_sets.items()
            }
        except (KeyError, ValueError):
            logging.exception("Can not parse backend response")
            raise UnknownBackendResponse()

    async def get_game_time(self, pid, master_title_id, multiplayer_id):
        url = "{}/atom/users/{}/games/{}/usage".format(
            self._get_api_host(),
            pid,
            master_title_id
        )

        # 'multiPlayerId' must be used if exists, otherwise '**/lastplayed' backend returns zero
        headers = {}
        if multiplayer_id:
            headers["Multiplayerid"] = multiplayer_id

        response = await self._http_client.get(url, headers=headers)

        """
        response looks like following:
        <?xml version="1.0" encoding="UTF-8" standalone="yes"?>
        <usage>
            <gameId>192140</gameId>
            <total>30292</total>
            <MultiplayerId>1024390</MultiplayerId>
            <lastSession>9</lastSession>
            <lastSessionEndTimeStamp>1497190184759</lastSessionEndTimeStamp>
        </usage>
        """
        try:
            content = await response.text()
            xml_response = ET.fromstring(content)
            total_play_time = round(int(xml_response.find("total").text)/60)  # response is in seconds
            last_session_end_time = round(int(xml_response.find("lastSessionEndTimeStamp").text)/1000)  # response is in miliseconds
            return total_play_time, last_session_end_time
        except (ET.ParseError, AttributeError, ValueError):
            logging.exception("Can not parse backend response")
            raise UnknownBackendResponse()

    async def get_friends(self, pid):
        response = await self._http_client.get(
            "{base_api}/atom/users/{user_id}/other/{other_user_id}/friends?page={page}".format(
                base_api=self._get_api_host(),
                user_id=pid,
                other_user_id=pid,
                page=0
            )
        )

        """
        <?xml version="1.0" encoding="UTF-8" standalone="yes"?>
        <users>
            <user>
                <userId>1003118773678</userId>
                <personaId>1781965055</personaId>
                <EAID>martinaurtica</EAID>
            </user>
            <user>
                <userId>1008880909879</userId>
                <personaId>1004303509879</personaId>
                <EAID>testerg976</EAID>
            </user>
        </users>
        """
        try:
            content = await response.text()
            return {
                user_xml.find("userId").text: user_xml.find("EAID").text
                for user_xml in ET.ElementTree(ET.fromstring(content)).iter("user")
            }
        except (ET.ParseError, AttributeError, ValueError):
            logging.exception("Can not parse backend response")
            raise UnknownBackendResponse()

    async def get_achievements_sets(self, user_id) -> Dict[str, str]:
        response = await self._http_client.get("{base_api}/atom/users/{user_id}/other/{other_user_id}/games".format(
            base_api=self._get_api_host(),
            user_id=user_id,
            other_user_id=user_id
        ))

        '''
        <?xml version="1.0" encoding="UTF-8" standalone="yes"?>
        <productInfoList>
            <productInfo>
                <productId>OFB-EAST:109552153</productId>
                <displayProductName>Battlefield 4™ (Trial)</displayProductName>
                <cdnAssetRoot>http://static.cdn.ea.com/ebisu/u/f/products/1015365</cdnAssetRoot>
                <imageServer>https://Eaassets-a.akamaihd.net/origin-com-store-final-assets-prod</imageServer>
                <packArtSmall>/76889/63.0x89.0/1007968_SB_63x89_en_US_^_2013-11-13-18-04-11_e8670.jpg</packArtSmall>
                <packArtMedium>/76889/142.0x200.0/1007968_MB_142x200_en_US_^_2013-11-13-18-04-08_2ff.jpg</packArtMedium>
                <packArtLarge>/76889/231.0x326.0/1007968_LB_231x326_en_US_^_2013-11-13-18-04-04_18173.jpg</packArtLarge>
                <softwareList>
                    <software softwarePlatform="PCWIN">
                        <achievementSetOverride>51302_76889_50844</achievementSetOverride>
                    </software>
                </softwareList>
                <masterTitleId>76889</masterTitleId>
                <gameDistributionSubType>Limited Trial</gameDistributionSubType>
            </productInfo>
        </productInfoList>
        '''
        try:
            def parse_product_id(product_info_xml):
                return product_info_xml.find("productId").text

            def parse_achievement_set(product_info_xml):
                set_xml = product_info_xml.find(".//softwareList/*/achievementSetOverride")
                if set_xml is None:
                    return None
                return set_xml.text

            content = await response.text()
            return {
                parse_product_id(product_info_xml): parse_achievement_set(product_info_xml)
                for product_info_xml in ET.ElementTree(ET.fromstring(content)).iter("productInfo")
            }
        except (ET.ParseError, AttributeError, ValueError):
            logging.exception("Can not parse backend response")
            raise UnknownBackendResponse()

    async def get_lastplayed_games(self, user_id) -> Dict[MasterTitleId, Timestamp]:
        response = await self._http_client.get("{base_api}/atom/users/{user_id}/games/lastplayed".format(
            base_api=self._get_api_host(),
            user_id=user_id
        ))

        '''
        <?xml version="1.0" encoding="UTF-8" standalone="yes"?>
        <lastPlayedGames>
            <userId>1008620950926</userId>
            <lastPlayed>
                <masterTitleId>180975</masterTitleId>
                <timestamp>2019-05-17T14:45:48.001Z</timestamp>
            </lastPlayed>
        </lastPlayedGames>
        '''
        try:
            def parse_title_id(product_info_xml) -> MasterTitleId:
                return product_info_xml.find("masterTitleId").text

            def parse_timestamp(product_info_xml) -> Timestamp:
                return Timestamp(int(
                    (
                        datetime.strptime(product_info_xml.find("timestamp").text, "%Y-%m-%dT%H:%M:%S.%fZ")
                        - datetime(1970, 1, 1)
                    ).total_seconds()
                ))

            content = await response.text()
            return {
                parse_title_id(product_info_xml): parse_timestamp(product_info_xml)
                for product_info_xml in ET.ElementTree(ET.fromstring(content)).iter("lastPlayed")
            }
        except (ET.ParseError, AttributeError, ValueError):
            logging.exception("Can not parse backend response")
            raise UnknownBackendResponse()
