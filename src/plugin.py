import asyncio
import sys
import time
from functools import partial
import webbrowser
import logging
import platform
import subprocess

from galaxy.api.plugin import Plugin, create_and_run_plugin
from galaxy.api.types import Achievement, Authentication, FriendInfo, Game, GameTime, LicenseInfo, NextStep
from galaxy.api.errors import (
    AuthenticationRequired, ApplicationError, BackendError, BackendNotAvailable, BackendTimeout,
    InvalidCredentials, UnknownBackendResponse, UnknownError
)
from galaxy.api.consts import Platform, LicenseType

from backend import (
    AuthenticatedHttpClient, OriginBackendClient,
    MasterTitleId, OfferId, Timestamp
)
from local_games import LocalGames, get_local_content_path
from typing import Dict, List, NewType, Optional
from uri_scheme_handler import is_uri_handler_installed
from version import __version__


def is_windows():
    return platform.system().lower() == "windows"


LOCAL_GAMES_CACHE_VALID_PERIOD = 5
AUTH_PARAMS = {
    "window_title": "Login to Origin",
    "window_width": 495 if is_windows() else 480,
    "window_height": 746 if is_windows() else 708,
    "start_uri": "https://accounts.ea.com/connect/auth"
                 "?response_type=code&client_id=ORIGIN_SPA_ID&display=originXWeb/login"
                 "&locale=en_US&release_type=prod"
                 "&redirect_uri=https://www.origin.com/views/login.html",
    "end_uri_regex": r"^https://www\.origin\.com/views/login\.html.*"
}

MultiplayerId = NewType("MultiplayerId", str)


class OriginPlugin(Plugin):
    # pylint: disable=abstract-method
    def __init__(self, reader, writer, token):
        super().__init__(Platform.Origin, __version__, reader, writer, token)
        self._pid = None
        self._persona_id = None
        self._offer_id_cache = {}
        self._game_time_cache: Dict[OfferId, GameTime] = {}
        self._last_played_games: Dict[MasterTitleId, Timestamp] = {}

        self._local_games = LocalGames(get_local_content_path())
        self._local_games_last_update = 0
        self._local_games_update_in_progress = False

        def auth_lost():
            self.lost_authentication()

        self._http_client = AuthenticatedHttpClient()
        self._http_client.set_auth_lost_callback(auth_lost)
        self._http_client.set_cookies_updated_callback(self._update_stored_cookies)
        self._backend_client = OriginBackendClient(self._http_client)

    def shutdown(self):
        asyncio.create_task(self._http_client.close())

    def tick(self):
        self.handle_local_game_update_notifications()

    async def _do_authenticate(self, cookies):
        try:
            await self._http_client.authenticate(cookies)

            self._pid, self._persona_id, user_name = await self._backend_client.get_identity()
            return Authentication(self._pid, user_name)

        except (BackendNotAvailable, BackendTimeout, BackendError):
            raise
        except Exception:
            # TODO: more precise error reason
            logging.exception("Authentication failed")
            raise InvalidCredentials()

    async def authenticate(self, stored_credentials=None):
        stored_cookies = stored_credentials.get("cookies") if stored_credentials else None

        if not stored_cookies:
            return NextStep("web_session", AUTH_PARAMS)

        return await self._do_authenticate(stored_cookies)

    async def pass_login_credentials(self, step, credentials, cookies):
        new_cookies = {cookie["name"]: cookie["value"] for cookie in cookies}
        auth_info = await self._do_authenticate(new_cookies)
        self._store_cookies(new_cookies)
        return auth_info

    async def get_owned_games(self):
        if not self._http_client.is_authenticated():
            raise AuthenticationRequired()

        owned_offers = await self._get_owned_offers()

        games = []
        for offer in owned_offers:
            game = Game(
                offer["offerId"],
                offer["i18n"]["displayName"],
                None,
                LicenseInfo(LicenseType.SinglePurchase, None)
            )
            games.append(game)

        return games

    # Left for backward compatibility, until feature detection uses transactional methods
    async def get_unlocked_achievements(self, game_id):
        if not self._http_client.is_authenticated():
            raise AuthenticationRequired()

        try:
            achievement_set = (await self._get_offers([game_id]))[0]["platforms"][0]["achievementSetOverride"]
            if achievement_set is None:
                return []

            achievements = await self._backend_client.get_achievements(self._persona_id, {game_id: achievement_set})

            return [
                Achievement(achievement_id=key, achievement_name=value["name"], unlock_time=value["u"])
                for key, value in achievements[game_id].items() if value["complete"]
            ]
        except (KeyError, IndexError):
            raise UnknownBackendResponse()

    async def start_achievements_import(self, game_ids):
        if not self._http_client.is_authenticated():
            raise AuthenticationRequired

        await super().start_achievements_import(game_ids)

    async def import_games_achievements(self, _game_ids):
        game_ids = set(_game_ids)
        error = UnknownError()
        try:
            achievement_sets = {}  # 'offerId' to 'achievementSet' names mapping
            for offer_id, achievement_set in (await self._backend_client.get_achievements_sets(self._persona_id)).items():
                if not achievement_set:
                    self.game_achievements_import_success(offer_id, [])
                    game_ids.remove(offer_id)
                else:
                    achievement_sets[offer_id] = achievement_set

            if not achievement_sets:
                return

            for offer_id, achievements in (await self._backend_client.get_achievements(
                self._persona_id, achievement_sets
            )).items():
                try:
                    self.game_achievements_import_success(offer_id, [
                        Achievement(achievement_id=key, achievement_name=value["name"], unlock_time=value["u"])
                        for key, value in achievements.items() if value["complete"]
                    ])
                except KeyError:
                    self.game_achievements_import_failure(offer_id, UnknownBackendResponse())
                except ApplicationError as error:
                    self.game_achievements_import_failure(offer_id, error)
                finally:
                    game_ids.remove(offer_id)
        except KeyError:
            error = UnknownBackendResponse()
        except ApplicationError as _error:
            error = _error
        except Exception:
            pass  # handled below
        finally:
            # any other exceptions or not answered game_ids are responded with an error
            [self.game_achievements_import_failure(game_id, error) for game_id in game_ids]

    async def _get_offers(self, offer_ids):
        """
            Get offers from cache if exists.
            Fetch from backend if not and update cache.
        """
        offers = []
        missing_offers = []
        for offer_id in offer_ids:
            offer = self._offer_id_cache.get(offer_id, None)
            if offer is not None:
                offers.append(offer)
            else:
                missing_offers.append(offer_id)

        # request for missing offers
        if missing_offers:
            requests = [self._backend_client.get_offer(offer_id) for offer_id in missing_offers]
            new_offers = await asyncio.gather(*requests)

            # update
            for offer in new_offers:
                offer_id = offer["offerId"]
                offers.append(offer)
                self._offer_id_cache[offer_id] = offer

        return offers

    async def _get_owned_offers(self):
        entitlements = await self._backend_client.get_entitlements(self._pid)

        # filter
        entitlements = [x for x in entitlements if x["offerType"] == "basegame"]

        # check if we have offers in cache
        offer_ids = [entitlement["offerId"] for entitlement in entitlements]
        return await self._get_offers(offer_ids)

    async def get_local_games(self):
        if self._local_games_update_in_progress:
            logging.debug("LocalGames.update in progress, returning cached values")
            return self._local_games.local_games

        loop = asyncio.get_running_loop()
        try:
            self._local_games_update_in_progress = True
            local_games, _ = await loop.run_in_executor(None, partial(LocalGames.update, self._local_games))
            self._local_games_last_update = time.time()
        finally:
            self._local_games_update_in_progress = False
        return local_games

    def handle_local_game_update_notifications(self):
        async def notify_local_games_changed():
            notify_list = []
            try:
                self._local_games_update_in_progress = True
                _, notify_list = await loop.run_in_executor(None, partial(LocalGames.update, self._local_games))
                self._local_games_last_update = time.time()
            finally:
                self._local_games_update_in_progress = False

            for local_game_notify in notify_list:
                self.update_local_game_status(local_game_notify)

        # don't overlap update operations
        if self._local_games_update_in_progress:
            logging.debug("LocalGames.update in progress, skipping cache update")
            return

        if time.time() - self._local_games_last_update < LOCAL_GAMES_CACHE_VALID_PERIOD:
            logging.debug("Local games cache is fresh enough")
            return

        loop = asyncio.get_running_loop()
        asyncio.create_task(notify_local_games_changed())

    @staticmethod
    def _get_multiplayer_id(offer) -> Optional[MultiplayerId]:
        for game_platform in offer["platforms"]:
            multiplayer_id = game_platform["multiPlayerId"]
            if multiplayer_id is not None:
                return multiplayer_id
        return None

    async def _get_game_times_for_offer(
        self,
        offer_id: OfferId,
        master_title_id: MasterTitleId,
        multiplayer_id: Optional[MultiplayerId],
        lastplayed_time: Optional[Timestamp]
    ) -> GameTime:
        # returns None if a new entry should be retrieved
        def get_cached_game_times(_offer_id: OfferId, _lastplayed_time: Optional[Timestamp]) -> Optional[GameTime]:
            if _lastplayed_time is None:
                # double-check if 'lastplayed_time' is unknown (maybe it was just to long ago)
                return None

            _cached_game_time: GameTime = self._game_time_cache.get(offer_id)
            if _cached_game_time is None or _cached_game_time.last_played_time is None:
                # played time unknown yet
                return None
            if _lastplayed_time > _cached_game_time.last_played_time:
                # newer played time available
                return None
            return _cached_game_time

        cached_game_time: Optional[GameTime] = get_cached_game_times(offer_id, lastplayed_time)
        if cached_game_time is not None:
            return cached_game_time

        response = await self._backend_client.get_game_time(self._pid, master_title_id, multiplayer_id)
        game_time: GameTime = GameTime(offer_id, response[0], response[1])
        self._game_time_cache[offer_id] = game_time
        return game_time

    async def get_game_times(self):
        if not self._http_client.is_authenticated():
            raise AuthenticationRequired()

        owned_offers, last_played_games = await asyncio.gather(
            self._get_owned_offers(),
            self._backend_client.get_lastplayed_games(self._pid)
        )

        requests = []
        try:
            for offer in owned_offers:
                master_title_id = offer["masterTitleId"]

                requests.append(self._get_game_times_for_offer(
                    offer_id=offer["offerId"],
                    master_title_id=master_title_id,
                    multiplayer_id=self._get_multiplayer_id(offer),
                    lastplayed_time=last_played_games.get(master_title_id)
                ))
        except KeyError:
            raise UnknownBackendResponse()

        return await asyncio.gather(*requests)

    async def start_game_times_import(self, game_ids):
        if not self._http_client.is_authenticated():
            raise AuthenticationRequired()

        _, self._last_played_games = await asyncio.gather(
            self._get_offers(game_ids),  # update local cache ignoring return value
            self._backend_client.get_lastplayed_games(self._pid)
        )

        await super().start_game_times_import(game_ids)

    async def import_game_times(self, game_ids: List[OfferId]):
        async def import_game_time(offer_id: OfferId):
            try:
                offer = self._offer_id_cache.get(offer_id)
                if offer is None:
                    raise Exception("Internal cache out of sync")
                master_title_id: MasterTitleId = offer["masterTitleId"]
                multiplayer_id: Optional[MultiplayerId] = self._get_multiplayer_id(offer)

                self.game_time_import_success(
                    await self._get_game_times_for_offer(
                        offer_id,
                        master_title_id,
                        multiplayer_id,
                        self._last_played_games.get(master_title_id)
                    )
                )
            except KeyError:
                self.game_time_import_failure(offer_id, UnknownBackendResponse())
            except ApplicationError as error:
                self.game_time_import_failure(offer_id, error)
            except Exception:
                logging.exception("Unhandled exception. Please report it to the plugin developers")
                self.game_time_import_failure(offer_id, UnknownError())

        await asyncio.gather(*[import_game_time(offer_id) for offer_id in game_ids])
        self._last_played_games = None

    async def get_friends(self):
        if not self._http_client.is_authenticated():
            raise AuthenticationRequired()

        return [
            FriendInfo(user_id=str(user_id), user_name=str(user_name))
            for user_id, user_name in (await self._backend_client.get_friends(self._pid)).items()
        ]

    @staticmethod
    async def _open_uri(uri):
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, partial(webbrowser.open, uri))

    async def launch_game(self, game_id):
        if is_uri_handler_installed("origin2"):
            await OriginPlugin._open_uri("origin2://game/launch?offerIds={}&autoDownload=true".format(game_id))
        else:
            await OriginPlugin._open_uri("https://www.origin.com/download")

    async def install_game(self, game_id):
        if is_uri_handler_installed("origin2"):
            await OriginPlugin._open_uri("origin2://game/download?offerId={}".format(game_id))
        else:
            await OriginPlugin._open_uri("https://www.origin.com/download")

    if is_windows():
        async def uninstall_game(self, game_id):
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(None, partial(subprocess.run, ["control", "appwiz.cpl"]))

    def _store_cookies(self, cookies):
        self.store_credentials({"cookies": cookies})

    def _update_stored_cookies(self, morsels):
        cookies = {}
        for morsel in morsels:
            cookies[morsel.key] = morsel.value
        self._store_cookies(cookies)


def main():
    create_and_run_plugin(OriginPlugin, sys.argv)


if __name__ == "__main__":
    main()
