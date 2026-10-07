from __future__ import annotations

import json
import re
import socket
import subprocess
import time
from pathlib import Path

import pytest

from helpers import terminate_process, write_war1gus_preferences


def _read(path: Path) -> str:
    return path.read_text(errors="replace") if path.exists() else ""


def _free_udp_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _launch(cmd: list[str], *, cwd: Path, env: dict[str, str], stdout: Path, stderr: Path) -> subprocess.Popen:
    with stdout.open("wb") as out, stderr.open("wb") as err:
        try:
            return subprocess.Popen(cmd, cwd=cwd, env=env, stdout=out, stderr=err)
        finally:
            if pos := env.get("SDL_VIDEO_WINDOW_POS"):
                env["SDL_VIDEO_WINDOW_POS"] = ",".join(str(int(c) + 640) for c in pos.split(","))


def _wait_for_log(path: Path, needle: str, proc: subprocess.Popen, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if needle in _read(path):
            return True
        if proc.poll() is not None:
            return False
        time.sleep(0.5)
    return False


def _combined_logs(paths: tuple[Path, ...]) -> str:
    return "\n".join(f"--- {path.name} ---\n{_read(path)}" for path in paths)


def _participant_cmd(participant: dict, args: list[str]) -> list[str]:
    return [*participant["argv"], *args]


def write_war1gus_campaign_start(startup, race, i, extra=""):
    startup.write_text(f"""
    Load("scripts/stratagus.lua")
    SetTitleScreens({{}})
    local function log(message)
      if not (os and os.getenv and os.getenv("STRATAGUS_UNBUFFERED_STDIO")) then
        return
      end
      print(message)
      if io and io.stdout then
        io.stdout:flush()
      end
    end
    CustomStartup = function()
      Load("scripts/campaigns.lua")
      race = "{race}"
      campaign = CreateCampaign(race)
      position = {i}
      currentCampaign = campaign
      currentRace = race
      currentState = {i}
      RunResultsMenu = function()
        if GameResult == GameVictory then
          log("PYTEST_WAR1_WON")
        end
        return
      end
      for i={i + 1},14,1 do
        campaign.steps[i] = function() end
      end
      Briefing = function(title, objs, bgImg, mapbg, mapVideo, text, voices) end
      RunCampaignSubmenu = function(race) end
      AddTrigger(
        function() return GameCycle > 25 end,
        function()
          log("PYTEST_WAR1_LOADED")
          for x=3,63,4 do
            for y=3,63,4 do
              CreateUnit("unit-knight", 0, {{x, y}})
            end
          end
        end)
      {extra}
      RunCampaign(campaign)
    end
    """)


@pytest.mark.gui
@pytest.mark.cross
@pytest.mark.slow
@pytest.mark.parametrize("sets", (("orc", 2), ("orc", 3), ("human", 5), ("human", 6), ("orc", 5), ("orc", 6)), ids=["orc2", "orc3", "elwynn", "northshire-abbey", "redridge-mountains", "sunnyglade"])
def test_war1gus_campaign_maps(
    stratagus_player: dict,
    extracted_war1gus_data: Path,
    sets,
    gui_env,
    tmp_path: Path,
):
    write_war1gus_preferences(tmp_path)
    startup = tmp_path / "start.lua"

    extra = ""
    if sets == ("orc", 6):
        # the human tower must survive in this mission
        extra = """
        AddTrigger(
          function() return GameCycle >= 1 end,
          function()
            for i,unit in ipairs(GetUnits(1)) do
              local ident = GetUnitVariable(unit, "Ident")
              if ident == "unit-human-tower" then
                local tower = unit
                AddTrigger(function()
                    SetUnitVariable(tower, "HitPoints", 3000)
                  end,
                  function() return true end)
              end
            end
          end)
        """

    write_war1gus_campaign_start(startup, sets[0], sets[1], extra)
    test_env = dict(gui_env)
    test_env["STRATAGUS_UNBUFFERED_STDIO"] = "1"

    host_out = tmp_path / ".stdout"
    host_err = tmp_path / ".stderr"
    common = [
        "-d",
        str(extracted_war1gus_data),
        "-W",
        "640x480",
        "-v",
        "640x480",
        "-g",
    ]
    host_cmd = _participant_cmd(
        stratagus_player,
        [
            *common,
            "-b",
            "-u",
            str(tmp_path),
            "-c",
            str(startup),
        ],
    )

    # War1gus still has legacy map-loading paths during network start that may
    # retry the raw relative map name. Run from the composed data tree so those
    # fallbacks resolve to the same files as the -d path.
    host = _launch(host_cmd, cwd=extracted_war1gus_data, env=test_env, stdout=host_out, stderr=host_err)
    logs = (host_out, host_err)
    try:
        host.wait(120)
    finally:
        terminate_process(host)

    combined = _combined_logs(logs)
    assert "PYTEST_WAR1_LOADED" in _read(host_out), _combined_logs((host_out, host_err))
    assert "PYTEST_WAR1_WON" in _read(host_out), _combined_logs((host_out, host_err))
    for marker in (
        "Network out of sync",
        "sent bad command",
        "Unknown unitType",
        "PYTEST_WAR1_JOINING_MAP_SETTINGS_ERROR",
        "Segmentation fault",
        "Aborted",
    ):
        assert marker not in combined


@pytest.mark.gui
@pytest.mark.cross
@pytest.mark.slow
def test_war1gus_idle_footman_autoattacks_without_policy_orders(
    stratagus_player: dict,
    extracted_war1gus_data: Path,
    gui_env,
    tmp_path: Path,
):
    """The war1gus AI bypass must not suppress ordinary unit target acquisition."""
    write_war1gus_preferences(tmp_path)
    map_path = tmp_path / "idle.smp"
    map_path.write_text(
        'DefinePlayerTypes("computer", "person", "computer", "person", "person")\n'
        'PresentMap("Idle auto-attack isolation", 5, 32, 32, 1)\n'
    )
    (tmp_path / "idle.sms").write_text("""
local function log(message)
  print("AUTOATTACK_TEST " .. message)
  if io and io.stdout then io.stdout:flush() end
end
for i = 0, 4 do
  SetStartView(i, 15, 15)
  SetPlayerData(i, "Resources", "gold", 1000)
  SetPlayerData(i, "Resources", "wood", 1000)
  SetPlayerData(i, "Resources", "lumber", 0)
  SetPlayerData(i, "RaceName", (i == 1 or i == 3) and "orc" or "human")
end
SetAiType(0, "war1gus-ai")
SetAiType(2, "idle-control-ai")
LoadTileModels("scripts/tilesets/forest.lua")
for y = 0, 31 do
  for x = 0, 31 do SetTile(80, x, y, 0) end
end
if MapUnitsInit ~= nil then MapUnitsInit() end
local policySoldier = CreateUnit("unit-footman", 0, {6, 6})
local policyTarget = CreateUnit("unit-orc-farm", 1, {9, 6})
local controlSoldier = CreateUnit("unit-footman", 2, {6, 16})
local controlTarget = CreateUnit("unit-orc-farm", 3, {9, 16})
local attackMoveSoldier = CreateUnit("unit-footman", 0, {4, 24})
local attackMoveTarget = CreateUnit("unit-orc-farm", 1, {13, 27})
CreateUnit("unit-human-town-hall", 4, {23, 23})
AddTrigger(function() return GameCycle >= 1 end, function()
  SetDiplomacy(0, "enemy", 1)
  SetDiplomacy(1, "enemy", 0)
  SetDiplomacy(2, "enemy", 3)
  SetDiplomacy(3, "enemy", 2)
  SetFogOfWar(false)
  RevealMap("explored")
  return false
end)
AddTrigger(function() return GameCycle >= 3 end, function()
  OrderUnit(0, "unit-footman", {4, 24}, {27, 24}, "attack")
  log("ATTACK_MOVE_ORDERED")
  return false
end)
local policyInitial = GetUnitVariable(policyTarget, "HitPoints")
local controlInitial = GetUnitVariable(controlTarget, "HitPoints")
local attackMoveInitial = GetUnitVariable(attackMoveTarget, "HitPoints")
log("READY policy_target_hp=" .. policyInitial .. " control_target_hp=" .. controlInitial
  .. " attackmove_target_hp=" .. attackMoveInitial)
AddTrigger(function() return GameCycle >= 450 end, function()
  log("RESULT cycle=" .. GameCycle
    .. " policy_target_hp=" .. GetUnitVariable(policyTarget, "HitPoints")
    .. " control_target_hp=" .. GetUnitVariable(controlTarget, "HitPoints")
    .. " attackmove_target_hp=" .. GetUnitVariable(attackMoveTarget, "HitPoints")
    .. " attackmove_x=" .. GetUnitVariable(attackMoveSoldier, "PosX")
    .. " attackmove_y=" .. GetUnitVariable(attackMoveSoldier, "PosY")
    .. " policy_diplomacy=" .. GetDiplomacy(0, 1)
    .. " control_diplomacy=" .. GetDiplomacy(2, 3))
  Exit(0)
  return false
end)
""")
    startup = tmp_path / "start.lua"
    startup.write_text(f"""
Load("scripts/stratagus.lua")
SetTitleScreens({{}})
-- Both AI callbacks deliberately issue no commands. Only the AI type name
-- differs, so damage must come from the engine's unit-level auto-attack.
local policyCalls = 0
local controlCalls = 0
DefineAi("war1gus-ai", "*", "war1gus-ai", function()
  policyCalls = policyCalls + 1
  if policyCalls == 1 then print("AUTOATTACK_TEST POLICY_CALLBACK_NO_ORDERS") io.stdout:flush() end
end, 5)
DefineAi("idle-control-ai", "*", "idle-control-ai", function()
  controlCalls = controlCalls + 1
  if controlCalls == 1 then print("AUTOATTACK_TEST CONTROL_CALLBACK_NO_ORDERS") io.stdout:flush() end
end, 5)
CustomStartup = function()
  InitGameSettings()
  GameSettings.GameType = -1
  RunMap({json.dumps(str(map_path))}, false)
  Exit(0)
end
""")
    test_env = dict(gui_env)
    test_env["STRATAGUS_UNBUFFERED_STDIO"] = "1"
    stdout = tmp_path / "autoattack.stdout"
    stderr = tmp_path / "autoattack.stderr"
    cmd = _participant_cmd(
        stratagus_player,
        ["-b", "-r", "-d", str(extracted_war1gus_data), "-u", str(tmp_path), "-c", str(startup)],
    )
    process = _launch(cmd, cwd=extracted_war1gus_data, env=test_env, stdout=stdout, stderr=stderr)
    try:
        process.wait(timeout=60)
    finally:
        terminate_process(process)

    logs = _combined_logs((stdout, stderr))
    assert process.returncode == 0, logs
    assert "AUTOATTACK_TEST POLICY_CALLBACK_NO_ORDERS" in logs, logs
    assert "AUTOATTACK_TEST ATTACK_MOVE_ORDERED" in logs, logs
    assert "AUTOATTACK_TEST CONTROL_CALLBACK_NO_ORDERS" in logs, logs
    ready = re.search(
        r"AUTOATTACK_TEST READY policy_target_hp=(\d+) control_target_hp=(\d+) "
        r"attackmove_target_hp=(\d+)",
        logs,
    )
    result = re.search(
        r"AUTOATTACK_TEST RESULT cycle=(\d+) policy_target_hp=(\d+) "
        r"control_target_hp=(\d+) attackmove_target_hp=(\d+) attackmove_x=(\d+) "
        r"attackmove_y=(\d+) policy_diplomacy=(\w+) control_diplomacy=(\w+)",
        logs,
    )
    assert ready is not None and result is not None, logs
    policy_initial, control_initial, attackmove_initial = map(int, ready.groups())
    cycle, policy_hp, control_hp, attackmove_hp, attackmove_x, attackmove_y = map(
        int, result.groups()[:6]
    )
    assert cycle >= 450, logs
    assert result.groups()[6:] == ("enemy", "enemy"), logs
    assert policy_initial > 0 and control_initial > 0, logs
    assert control_hp < control_initial, logs
    assert policy_hp < policy_initial, logs
    assert attackmove_initial > 0 and attackmove_hp < attackmove_initial, logs
    assert attackmove_x < 27, logs
