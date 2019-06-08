import os
import glob
import platform
import logging
import urllib.parse
from enum import Enum, auto
from galaxy.api.types import LocalGame, LocalGameState
from galaxy.api.errors import FailedParsingManifest

class _State(Enum):
    kInvalid = auto()
    kError = auto()
    kPaused = auto()
    kPausing = auto()
    kCanceling = auto()
    kReadyToStart = auto()
    kInitializing = auto()
    kResuming = auto()
    kPreTransfer = auto()
    kPendingInstallInfo = auto()
    kPendingEulaLangSelection = auto()
    kPendingEula = auto()
    kEnqueued = auto()
    kTransferring = auto()
    kPendingDiscChange = auto()
    kPostTransfer = auto()
    kMounting = auto()
    kUnmounting = auto()
    kUnpacking = auto()
    kDecrypting = auto()
    kReadyToInstall = auto()
    kPreInstall = auto()
    kInstalling = auto()
    kPostInstall = auto()
    kFetchLicense = auto()
    kCompleted = auto()

def _parse_msft_file(filepath):
    with open(filepath, encoding="utf-8") as file:
        data = file.read()
    parsed_url = urllib.parse.urlparse(data)
    parsed_data = dict(urllib.parse.parse_qsl(parsed_url.query))
    state = _State[parsed_data.get("currentstate", _State.kInvalid.name)]
    prev_state = _State[parsed_data.get("previousstate", _State.kInvalid.name)]
    ddinstallalreadycompleted = parsed_data.get("ddinstallalreadycompleted", "0")
    installed = \
        (state == _State.kReadyToStart and prev_state == _State.kCompleted) \
        or ddinstallalreadycompleted == "1"
    return LocalGame(
        parsed_data["id"],
        LocalGameState.Installed if installed else LocalGameState.None_
    )

def get_local_games_from_stats(stats):
    local_games = []
    for filename in stats.keys():
        try:
            local_games.append(_parse_msft_file(filename))
        except Exception as e:
            logging.exception("Failed to parse file {}".format(filename))
            raise FailedParsingManifest({"file": filename, "exception": e})

    return local_games

def get_local_games_manifest_stat(path):
    path = os.path.abspath(path)
    return {
        filename:os.stat(filename)
        for filename in glob.glob(
            os.path.join(path, "**", "*.mfst"),
            recursive=True
        )
    }

def get_state_changes(old_list, new_list):
    old_dict = {x.game_id: x.local_game_state for x in old_list}
    new_dict = {x.game_id: x.local_game_state for x in new_list}
    result = []
    # removed games
    result.extend(LocalGame(id, LocalGameState.None_) for id in old_dict.keys() - new_dict.keys())
    # added games
    result.extend(local_game for local_game in new_list if local_game.game_id in new_dict.keys() - old_dict.keys())
    # state changed
    result.extend(LocalGame(id, new_dict[id]) for id in new_dict.keys() & old_dict.keys() if new_dict[id] != old_dict[id])
    return result

def get_local_content_path():
    platform_id = platform.system()

    if platform_id == "Windows":
        local_content_path = os.path.join(os.environ["ProgramData"], "Origin", "LocalContent")
    elif platform_id == "Darwin":
        local_content_path = os.path.join(os.sep, "Library", "Application Support", "Origin", "LocalContent")
    else:
        local_content_path = "." # fallback for testing on another platform
        # raise NotImplementedError("Not implemented on {}".format(platform_id))

    return local_content_path

class LocalGames:

    def __init__(self, path):
        self._path = path
        self._manifests_stats = get_local_games_manifest_stat(self._path)
        try:
            self._local_games = get_local_games_from_stats(self._manifests_stats)
        except FailedParsingManifest as e:
            self._local_games = []
            logging.warning("Failed to parse local games on start: {}, {}".format(e.message, e.data))

    @property
    def local_games(self):
        return self._local_games

    def update(self):
        '''
        returns list of changed games (added, removed, or changed)
        updated local_games property
        '''
        new_stats = get_local_games_manifest_stat(self._path)
        if new_stats == self._manifests_stats:
            return self._local_games, []
        self._manifests_stats = new_stats
        new_local_games = get_local_games_from_stats(new_stats)
        notify_list = get_state_changes(self._local_games, new_local_games)
        self._local_games = new_local_games
        return self._local_games, notify_list
