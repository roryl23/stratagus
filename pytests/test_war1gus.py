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
@pytest.mark.slow
@pytest.mark.parametrize("wall_only", (True, False), ids=("wall-only", "non-wall-survives"))
def test_war1gus_rollout_eliminates_wall_only_player(
    stratagus_player: dict,
    extracted_war1gus_data: Path,
    gui_env,
    tmp_path: Path,
    wall_only: bool,
):
    """The real engine, rollout, and AI reward agree on wall-only elimination."""
    write_war1gus_preferences(tmp_path)
    map_path = tmp_path / "wall-survival.smp"
    map_path.write_text(
        'DefinePlayerTypes("person", "computer")\n'
        'PresentMap("Wall-only survival", 2, 32, 32, 1)\n'
    )
    (tmp_path / "wall-survival.sms").write_text(f"""
for i = 0, 1 do
  SetStartView(i, 15, 15)
  SetPlayerData(i, "Resources", "gold", 1000)
  SetPlayerData(i, "Resources", "wood", 1000)
  SetPlayerData(i, "RaceName", i == 0 and "human" or "orc")
end
SetAiType(1, "ai-passive")
LoadTileModels("scripts/tilesets/forest.lua")
for y = 0, 31 do
  for x = 0, 31 do SetTile(80, x, y, 0) end
end
if MapUnitsInit ~= nil then MapUnitsInit() end
CreateUnit("unit-footman", 0, {{25, 25}})
CreateUnit("unit-wall", 1, {{5, 5}})
if not {"true" if wall_only else "false"} then
  CreateUnit("unit-orc-farm", 1, {{8, 8}})
end
SetDiplomacy(0, "enemy", 1)
SetDiplomacy(1, "enemy", 0)
local function hasNonWallUnits(player)
  for _, unit in ipairs(GetUnits(player)) do
    if not GetUnitBoolFlag(unit, "Wall") then return true end
  end
  return false
end
local state, _, reward = War1gusAiFinalState(1)
print("WALL_SURVIVAL train_units=" .. GetPlayerData(1, "TotalNumUnits")
  .. " train_alive=" .. tostring(hasNonWallUnits(1))
  .. " opponent_alive=" .. tostring(hasNonWallUnits(0))
  .. " opponent_count=" .. GetNumOpponents(0)
  .. " train_opponent_count=" .. GetNumOpponents(1)
  .. " terminal_reward=" .. reward.terminal
  .. " state_terminal_word=" .. state[22])
io.stdout:flush()
""")
    repo_root = Path(__file__).resolve().parents[2]
    env = dict(gui_env)
    env.update({
        "STRATAGUS_UNBUFFERED_STDIO": "1",
        "WAR1GUS_ROLLOUT_MAP": str(map_path),
        "WAR1GUS_ROLLOUT_TRAIN_PLAYER": "1",
        "WAR1GUS_ROLLOUT_TIMEOUT_CYCLES": "30",
        "WAR1GUS_ROLLOUT_MATCH_ID": "pytest-wall-survival",
        "WAR1GUS_ROLLOUT_MODE": "evaluate",
    })
    stdout, stderr = tmp_path / "rollout.stdout", tmp_path / "rollout.stderr"
    cmd = _participant_cmd(
        stratagus_player,
        ["-b", "-r", "-d", str(extracted_war1gus_data), "-u", str(tmp_path),
         "-c", str(repo_root / "scripts/ai/war1gus/rollout.lua")],
    )
    process = _launch(cmd, cwd=repo_root, env=env, stdout=stdout, stderr=stderr)
    try:
        process.wait(timeout=60)
    finally:
        terminate_process(process)

    logs = _combined_logs((stdout, stderr))
    assert process.returncode == 0, logs
    observation = re.search(
        r"WALL_SURVIVAL train_units=(\d+) train_alive=(true|false) "
        r"opponent_alive=(true|false) opponent_count=(\d+) "
        r"train_opponent_count=(\d+) terminal_reward=(-?\d+) "
        r"state_terminal_word=(\d+)",
        logs,
    )
    assert observation is not None, logs
    units, train_alive, opponent_alive, opponents, train_opponents, reward, word = observation.groups()
    assert int(units) >= 1, logs
    assert opponent_alive == "true" and int(train_opponents) == 1, logs
    assert train_alive == str(not wall_only).lower(), logs
    assert int(opponents) == (0 if wall_only else 1), logs
    assert int(reward) == (-1000 if wall_only else 0), logs
    assert int(word) == (4294966296 if wall_only else 0), logs
    terminals = [
        json.loads(line) for line in _read(stdout).splitlines()
        if line.startswith('{"type":"rollout_terminal"')
    ]
    assert len(terminals) == 1, logs
    assert terminals[0]["trainable_player"] == 1, logs
    assert terminals[0]["outcome"] == ("loss" if wall_only else "timeout"), logs
    assert all(terminals[0][key] == 0 for key in (
        "produced_units", "completed_buildings", "lost_units", "lost_buildings"
    )), logs
    if wall_only:
        assert terminals[0]["cycles"] < 30, logs
    else:
        assert terminals[0]["cycles"] >= 30, logs


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


@pytest.mark.gui
@pytest.mark.slow
def test_war1gus_enemy_damage_reward_requires_player_attribution(
    stratagus_player: dict,
    extracted_war1gus_data: Path,
    gui_env,
    tmp_path: Path,
):
    """Another player's damage is not ours; our lethal hit credits remaining HP."""
    write_war1gus_preferences(tmp_path)
    map_path = tmp_path / "attributed.smp"
    map_path.write_text(
        'DefinePlayerTypes("person", "computer", "computer")\n'
        'PresentMap("Damage reward attribution", 3, 32, 32, 1)\n'
    )
    (tmp_path / "attributed.sms").write_text("""
local function log(stage, state, target)
  print("DAMAGE_REWARD " .. stage
    .. " progress=" .. state.enemyProgress
    .. " hp=" .. GetUnitVariable(target, "HitPoints")
    .. " kills=" .. GetPlayerData(0, "TotalKills")
    .. " razings=" .. GetPlayerData(0, "TotalRazings")
    .. " damage=" .. GetPlayerData(0, "TotalEnemyAssetDamage")
    .. " cycle=" .. GameCycle)
  io.stdout:flush()
end
for i = 0, 2 do
  SetStartView(i, 15, 15)
  SetPlayerData(i, "Resources", "gold", 1000)
  SetPlayerData(i, "Resources", "wood", 1000)
  SetPlayerData(i, "Resources", "lumber", 0)
  SetPlayerData(i, "RaceName", i == 1 and "orc" or "human")
end
LoadTileModels("scripts/tilesets/forest.lua")
for y = 0, 31 do
  for x = 0, 31 do SetTile(80, x, y, 0) end
end
if MapUnitsInit ~= nil then MapUnitsInit() end
local attackerA = CreateUnit("unit-footman", 0, {4, 4})
local targetB = CreateUnit("unit-orc-farm", 1, {14, 14})
local attackerC = CreateUnit("unit-footman", 2, {26, 26})
local ownCycle
AddTrigger(function() return GameCycle >= 1 end, function()
  SetDiplomacy(0, "enemy", 1)
  SetDiplomacy(1, "enemy", 0)
  SetDiplomacy(2, "enemy", 1)
  SetDiplomacy(1, "enemy", 2)
  SetDiplomacy(0, "neutral", 2)
  SetDiplomacy(2, "neutral", 0)
  return false
end)
AddTrigger(function() return GameCycle >= 2 end, function()
  assert(GetDiplomacy(0, 1) == "enemy")
  local _, _, baseline = War1gusAiFinalState(0, "draw")
  log("baseline", baseline, targetB)
  DamageUnit(attackerC, targetB, 20)
  local _, _, third = War1gusAiFinalState(0, "draw")
  log("third", third, targetB)
  SetDiplomacy(1, "neutral", 0)
  return false
end)
AddTrigger(function() return GameCycle >= 3 and GetDiplomacy(1, 0) == "neutral" end, function()
  -- A considers B an enemy even though B does not consider A an enemy.
  assert(GetDiplomacy(0, 1) == "enemy")
  assert(GetDiplomacy(1, 0) == "neutral")
  DamageUnit(attackerA, targetB, 20)
  local _, _, own = War1gusAiFinalState(0, "draw")
  log("own", own, targetB)
  ownCycle = GameCycle
  SetDiplomacy(1, "enemy", 0)
  return false
end)
AddTrigger(function() return ownCycle ~= nil and GameCycle > ownCycle and GetDiplomacy(1, 0) == "enemy" end, function()
  assert(GetDiplomacy(1, 0) == "enemy")
  DamageUnit(attackerA, targetB, 1000)
  local _, _, lethal = War1gusAiFinalState(0, "draw")
  log("lethal", lethal, targetB)
  Exit(0)
  return false
end)
""")
    startup = tmp_path / "start.lua"
    startup.write_text(f"""
Load("scripts/stratagus.lua")
SetTitleScreens({{}})
CustomStartup = function()
  InitGameSettings()
  GameSettings.GameType = -1
  RunMap({json.dumps(str(map_path))}, false)
  Exit(0)
end
""")
    test_env = dict(gui_env)
    test_env["STRATAGUS_UNBUFFERED_STDIO"] = "1"
    stdout = tmp_path / "damage.stdout"
    stderr = tmp_path / "damage.stderr"
    cmd = _participant_cmd(
        stratagus_player,
        ["-b", "-r", "-d", str(extracted_war1gus_data), "-u", str(tmp_path), "-c", str(startup)],
    )
    # Load the checked-out AI reward script; -d supplies proprietary assets only.
    process = _launch(
        cmd, cwd=Path(__file__).resolve().parents[2], env=test_env, stdout=stdout, stderr=stderr
    )
    try:
        process.wait(timeout=60)
    finally:
        terminate_process(process)

    logs = _combined_logs((stdout, stderr))
    assert process.returncode == 0, logs
    observations = re.findall(
        r"DAMAGE_REWARD (baseline|third|own|lethal) progress=(-?[\d.]+) "
        r"hp=(\d+) kills=(\d+) razings=(\d+) damage=([\d.]+) cycle=(\d+)",
        logs,
    )
    assert [row[0] for row in observations] == ["baseline", "third", "own", "lethal"], logs
    baseline, third, own, lethal = observations
    assert int(baseline[6]) == int(third[6]) < int(own[6]) < int(lethal[6]), logs
    assert [int(row[2]) for row in observations] == [400, 380, 360, 0], logs
    assert float(baseline[1]) == float(third[1]) == 0, logs
    assert float(own[1]) > 0, logs
    assert float(lethal[1]) > float(own[1]) + 50, logs
    assert [int(row[3]) for row in observations[:3]] == [0, 0, 0], logs
    assert [int(row[4]) for row in observations[:3]] == [0, 0, 0], logs
    assert int(lethal[4]) == 1, logs
    assert float(baseline[5]) == float(third[5]) == 0, logs
    assert float(own[5]) > 0 and float(lethal[5]) > float(own[5]), logs


@pytest.mark.gui
@pytest.mark.slow
def test_war1gus_production_and_casualties_count_actual_events(
    stratagus_player: dict,
    extracted_war1gus_data: Path,
    gui_env,
    tmp_path: Path,
):
    """Starting assets and captures are not production or combat casualties."""
    write_war1gus_preferences(tmp_path)
    map_path = tmp_path / "production-events.smp"
    map_path.write_text(
        'DefinePlayerTypes("computer", "person")\n'
        'PresentMap("Production and loss events", 2, 32, 32, 1)\n'
    )
    (tmp_path / "production-events.sms").write_text("""
local captive
local function log(stage)
  print("PRODUCTION_EVENTS " .. stage
    .. " trained=" .. GetPlayerData(0, "TrainedUnits")
    .. " completed=" .. GetPlayerData(0, "CompletedBuildings")
    .. " lost_units=" .. GetPlayerData(0, "LostUnits")
    .. " lost_buildings=" .. GetPlayerData(0, "LostBuildings")
    .. " total_units=" .. GetPlayerData(0, "TotalUnits")
    .. " total_buildings=" .. GetPlayerData(0, "TotalBuildings")
    .. " captured_owner=" .. GetUnitVariable(captive, "Player"))
  io.stdout:flush()
end
for i = 0, 1 do
  SetStartView(i, 15, 15)
  SetPlayerData(i, "Resources", "gold", 10000)
  SetPlayerData(i, "Resources", "wood", 10000)
  SetPlayerData(i, "RaceName", "human")
end
SetPlayerData(0, "SpeedTrain", 10000)
SetPlayerData(0, "SpeedBuild", 10000)
SetAiType(0, "production-metrics-ai")
LoadTileModels("scripts/tilesets/forest.lua")
for y = 0, 31 do
  for x = 0, 31 do SetTile(80, x, y, 0) end
end
if MapUnitsInit ~= nil then MapUnitsInit() end
local hall = CreateUnit("unit-human-town-hall", 0, {3, 3})
local farm = CreateUnit("unit-human-farm", 0, {11, 5})
-- Finished farms require adjacency to a neutral road during actual play.
CreateUnit("unit-road", 15, {14, 8})
local worker = CreateUnit("unit-peasant", 0, {17, 5})
local footman = CreateUnit("unit-footman", 0, {18, 17})
captive = CreateUnit("unit-peasant", 1, {26, 17})
CreateUnit("unit-human-farm", 1, {24, 24})
SetDiplomacy(0, "neutral", 1)
SetDiplomacy(1, "neutral", 0)
log("initial")
local ordered = false
AddTrigger(function() return GameCycle >= 2 end, function()
  if ordered then return false end
  ordered = true
  -- Select a valid site under the engine's building and AI placement rules.
  local site
  for y = 2, 27 do
    for x = 2, 27 do
      if AiCanBuildAt(0, worker, "unit-human-farm", {x, y}) then
        site = {x = x, y = y}
        break
      end
    end
    if site then break end
  end
  assert(site, "no valid farm site on the test map")
  assert(AiPublishCommandBatch(0, 1, {
    {actor = hall, verb = "train", argument = "unit-peasant"}
  }))
  assert(AiPublishCommandBatch(0, 2, {
    {actor = worker, verb = "build-at",
      argument = {type = "unit-human-farm", x = site.x, y = site.y}}
  }))
  log("ordered")
  return false
end)
local started = false
AddTrigger(function()
  return ordered and not started
    and GetPlayerData(0, "TotalBuildings") >= 3
    and GetPlayerData(0, "CompletedBuildings") == 0
end, function()
  started = true
  log("started")
  return false
end)
local finished = false
AddTrigger(function()
  return ordered and not finished
    and GetPlayerData(0, "TrainedUnits") >= 1
    and GetPlayerData(0, "CompletedBuildings") >= 1
end, function()
  finished = true
  log("finished")
  ChangeUnitsOwner({26, 17}, {26, 17}, 1, 0, "unit-peasant")
  log("captured")
  ChangeUnitsOwner({26, 17}, {26, 17}, 0, 1, "unit-peasant")
  log("returned")
  DamageUnit(captive, footman, 10000)
  DamageUnit(captive, farm, 10000)
  log("killed")
  Exit(0)
  return false
end)
AddTrigger(function() return GameCycle >= 1200 end, function()
  log("timed_out")
  Exit(0)
  return false
end)
""")
    startup = tmp_path / "start.lua"
    startup.write_text(f"""
Load("scripts/stratagus.lua")
SetTitleScreens({{}})
DefineAi("production-metrics-ai", "*", "production-metrics-ai", function() end, 5)
CustomStartup = function()
  InitGameSettings()
  GameSettings.GameType = -1
  RunMap({json.dumps(str(map_path))}, false)
  Exit(0)
end
""")
    env = dict(gui_env)
    env["STRATAGUS_UNBUFFERED_STDIO"] = "1"
    stdout, stderr = tmp_path / "production.stdout", tmp_path / "production.stderr"
    repo_root = Path(__file__).resolve().parents[2]
    cmd = _participant_cmd(
        stratagus_player,
        ["-b", "-r", "-d", str(extracted_war1gus_data), "-u", str(tmp_path),
         "-c", str(startup)],
    )
    process = _launch(cmd, cwd=repo_root, env=env, stdout=stdout, stderr=stderr)
    try:
        process.wait(timeout=60)
    finally:
        terminate_process(process)

    logs = _combined_logs((stdout, stderr))
    assert process.returncode == 0, logs
    matches = re.findall(
        r"PRODUCTION_EVENTS (initial|ordered|started|finished|captured|returned|killed|timed_out)"
        r" trained=(\d+) completed=(\d+) lost_units=(\d+) lost_buildings=(\d+)"
        r" total_units=(\d+) total_buildings=(\d+) captured_owner=(\d+)",
        logs,
    )
    assert [row[0] for row in matches] == [
        "initial", "ordered", "started", "finished", "captured", "returned", "killed"
    ], logs
    initial, ordered, started, finished, captured, returned, killed = (
        tuple(map(int, row[1:])) for row in matches
    )
    assert initial[:4] == ordered[:4] == (0, 0, 0, 0), logs
    assert initial[4] >= 2 and initial[5] >= 2, logs
    assert started[1:4] == (0, 0, 0) and started[5] == initial[5] + 1, logs
    assert finished[:4] == (1, 1, 0, 0), logs
    assert captured[:4] == returned[:4] == finished[:4], logs
    assert (finished[6], captured[6], returned[6]) == (1, 0, 1), logs
    assert killed[:4] == (1, 1, 1, 1), logs
