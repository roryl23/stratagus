//       _________ __                 __
//      /   _____//  |_____________ _/  |______     ____  __ __  ______
//      \_____  \\   __\_  __ \__  \\   __\__  \   / ___\|  |  \/  ___/
//      /        \|  |  |  | \// __ \|  |  / __ \_/ /_/  >  |  /\___ |
//     /_______  /|__|  |__|  (____  /__| (____  /\___  /|____//____  >
//             \/                  \/          \//_____/            \/
//  ______________________                           ______________________
//                        T H E   W A R   B E G I N S
//         Stratagus - A free fantasy real time strategy game engine
//
/**@name test_net_lowlevel.cpp - The test file for net_lowlevel.cpp. */
//
//      (c) Copyright 2013 by Joris Dauphin
//
//      This program is free software; you can redistribute it and/or modify
//      it under the terms of the GNU General Public License as published by
//      the Free Software Foundation; only version 2 of the License.
//
//      This program is distributed in the hope that it will be useful,
//      but WITHOUT ANY WARRANTY; without even the implied warranty of
//      MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
//      GNU General Public License for more details.
//
//      You should have received a copy of the GNU General Public License
//      along with this program; if not, write to the Free Software
//      Foundation, Inc., 59 Temple Place - Suite 330, Boston, MA
//      02111-1307, USA.
//

#include "commands.h"
#include "net_message.h"
#include "network.h"
#include "stratagus.h"

#undef L
#include <doctest.h>

void FillCustomValue(CNetworkCommand *obj)
{
	obj->Dest = 0x1234;
	obj->Unit = 0x5678;
	obj->X = 0x9ABC;
	obj->Y = 0xDEF0;
}

void FillCustomValue(CNetworkExtendedCommand *obj)
{
	obj->ExtendedType = 11;
	obj->Arg1 = 22;
	obj->Arg2 = 0x1234;
	obj->Arg3 = 0x5678;
	obj->Arg4 = 0x9ABC;
}

void FillCustomValue(CNetworkChat *obj)
{
	obj->Text = "abcdefghijklmnopqrstuvwxyz";
}
void FillCustomValue(CNetworkCommandSync *obj)
{
	obj->syncSeed = 0x01234567;
	obj->syncHash = 0x89ABCDEF;
}
void FillCustomValue(CNetworkCommandQuit *obj)
{
	obj->player = 0x0123;
}
void FillCustomValue(CNetworkSelection *obj)
{
	for (int i = 0; i != 10; ++i) {
		obj->Units.push_back(0x0123 * i);
	}
}

void FillCustomValue(CNetworkPacketHeader *obj)
{
	obj->Cycle = 42;
	for (int i = 0; i != MaxNetworkCommands; ++i) {
		obj->Type[i] = 0x05 + i * 0x12;
	}
}

template <typename T>
bool Comp(const T &lhs, const T &rhs)
{
	return memcmp(&lhs, &rhs, sizeof(T)) == 0;
}

bool Comp(const CNetworkChat &lhs, const CNetworkChat &rhs)
{
	return lhs.Text == rhs.Text;
}

bool Comp(const CNetworkSelection &lhs, const CNetworkSelection &rhs)
{
	return lhs.Units == rhs.Units;
}

template <typename T>
bool CheckSerialization()
{
	T obj1;

	FillCustomValue(&obj1);
	std::vector<unsigned char> buffer(obj1.Size());
	obj1.Serialize(buffer.data());

	T obj2;
	obj2.Deserialize(buffer.data());
	bool res = Comp(obj1, obj2);
	return res;
}

TEST_CASE("CNetworkCommand")
{
	CHECK(CheckSerialization<CNetworkCommand>());
}
TEST_CASE("CNetworkExtendedCommand")
{
	CHECK(CheckSerialization<CNetworkExtendedCommand>());
}
TEST_CASE("CNetworkChat")
{
	CHECK(CheckSerialization<CNetworkChat>());
}
TEST_CASE("CNetworkCommandSync")
{
	CHECK(CheckSerialization<CNetworkCommandSync>());
}
TEST_CASE("CNetworkCommandQuit")
{
	CHECK(CheckSerialization<CNetworkCommandQuit>());
}
TEST_CASE("CNetworkSelection")
{
	CHECK(CheckSerialization<CNetworkSelection>());
}
TEST_CASE("CNetworkPacketHeader")
{
	CHECK(CheckSerialization<CNetworkPacketHeader>());
}
//TEST_CASE("CNetworkPacket")

TEST_CASE("Mixed AI batch and human command survive a packet round trip")
{
	CNetworkPacket packet;
	packet.Header.Cycle = 42;
	packet.Header.OrigPlayer = 3;
	packet.Header.Type[0] = MessageAiCommandBatch;
	packet.Header.Type[1] = MessageCommandMove;
	// Player, sequence, count, then verb/actor/x/y/target for each primitive.
	packet.Command[0] = {
		2, 0, 0, 0, 7, 2, static_cast<unsigned char>(AiCommandVerb::Move),     0, 4,
		0, 8, 0, 9, 0, 0, static_cast<unsigned char>(AiCommandVerb::CastAuto), 0, 5,
		0, 0, 0, 0, 0, 3};
	CNetworkCommand move;
	move.Unit = 12;
	move.X = 14;
	move.Y = 16;
	packet.Command[1].resize(move.Size());
	move.Serialize(packet.Command[1].data());
	std::vector<unsigned char> wire(packet.Size(2));
	REQUIRE(packet.Serialize(wire.data(), 2) == wire.size());

	CNetworkPacket decoded;
	int commandCount = -1;
	decoded.Deserialize(wire.data(), wire.size(), &commandCount);
	REQUIRE(commandCount == 2);
	CHECK(decoded.Header.Type[0] == MessageAiCommandBatch);
	CHECK(decoded.Header.Type[1] == MessageCommandMove);
	CHECK(decoded.Header.Cycle == 42);
	CHECK(decoded.Header.OrigPlayer == 3);
	CHECK(decoded.Command[0] == packet.Command[0]);
	CHECK(decoded.Command[1] == packet.Command[1]);
}

TEST_CASE("Malformed and oversized network packet command lists are rejected")
{
	CNetworkPacket packet;
	for (int i = 0; i != MaxNetworkCommands; ++i) {
		packet.Header.Type[i] = MessageCommandMove;
		packet.Command[i] = std::vector<unsigned char>(CNetworkCommand::Size(), i);
	}
	std::vector<unsigned char> wire(packet.Size(MaxNetworkCommands));
	packet.Serialize(wire.data(), MaxNetworkCommands);
	CNetworkPacket decoded;
	int commandCount = 0;

	decoded.Deserialize(wire.data(), CNetworkPacketHeader::Size() - 1, &commandCount);
	CHECK(commandCount == -1);
	decoded.Deserialize(wire.data(), wire.size() - 1, &commandCount);
	CHECK(commandCount == -1);
	decoded.Deserialize(wire.data(), wire.size(), &commandCount);
	CHECK(commandCount == MaxNetworkCommands);
	// A tenth length-prefixed item exceeds the fixed packet command count.
	wire.insert(wire.end(), {0, 0, 0, 0, 0});
	decoded.Deserialize(wire.data(), wire.size(), &commandCount);
	CHECK(commandCount == -1);
}
