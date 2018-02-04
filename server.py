import sys
import random
import json
import collections
from twisted.web.static import File
from twisted.python import log
from twisted.web.server import Site
from twisted.internet import reactor
import codecs
import time
import os
from typing import Dict, List, Iterable, Any, Union, Optional, Tuple

from autobahn.twisted.websocket import WebSocketServerFactory, \
    WebSocketServerProtocol

from autobahn.twisted.resource import WebSocketResource

# clients need to send:
# - on join, a user name and a room name
# - "start round"
# - "next round"
# servers need to send:
# - updated lists of users in the room
# - a round number, a wordlist, and a word

wordlists: Dict[str, List[str]] = {}

version = "v0.2"

wordlist_directory = 'wordlists'

for filename in os.listdir(wordlist_directory):
    with open(os.path.join(wordlist_directory, filename)) as infile:
        key = filename
        if key.endswith('.txt'): key = key[:-4]
        wordlists[key] = [line.strip() for line in infile]

class CastlefallProtocol(WebSocketServerProtocol):
    def onOpen(self) -> None: pass

    def connectionLost(self, reason) -> None:
        self.factory.unregister(self)

    def onMessage(self, payload: Union[str, bytes], isBinary: bool) -> None:
        assert isinstance(self.factory, CastlefallFactory)
        if isinstance(payload, bytes):
            payload = codecs.decode(payload, 'utf-8')
        data = json.loads(payload)
        if 'name' in data:
            room_name = data['room']
            name = data['name']
            print('{}: registering as {}'.format(self.peer, name))
            self.factory.register(room_name, name, self)
        if 'start' in data:
            start_val = data['start']
            print('{}: start {}'.format(self.peer, start_val))
            self.factory.start_round(self, start_val)
        if 'kick' in data:
            kick_target = data['kick']
            print('{}: kicking {}'.format(self.peer, kick_target))
            self.factory.kick(self, kick_target)

def json_to_bytes(obj: dict) -> bytes:
    return codecs.encode(json.dumps(obj), 'utf-8')

class ClientStatus:
    def __init__(self, room: str, name: str) -> None:
        self.room = room
        self.name = name

class Room:
    def __init__(self) -> None:
        self.d: Dict[str, CastlefallProtocol] = {}
        self.round = 0
        self.last_start = time.time()
        self.players_in_round: List[str] = []
        self.assigned_words: Dict[str, str] = {}
        self.words_left: Dict[str, List[str]] = collections.defaultdict(list)

    def has_player(self, name: str) -> bool:
        return name in self.d

    def get_player_names(self) -> List[str]:
        return list(sorted(self.d.keys()))

    def get_player_client(self, name: str) -> CastlefallProtocol:
        return self.d[name]

    def set_player_client(self, name: str, p: CastlefallProtocol) -> None:
        self.d[name] = p

    def get_clients(self) -> Iterable[CastlefallProtocol]:
        return self.d.values()

    def get_named_clients(self) -> Iterable[Tuple[str, CastlefallProtocol]]:
        return self.d.items()

    def delete_player_client(self, name: str) -> None:
        del self.d[name]

    def clear_assigned_words(self):
        self.assigned_words = {}

    def get_assigned_word(self, name: str) -> Optional[str]:
        return self.assigned_words.get(name)

    def set_assigned_word(self, name: str, word: str) -> None:
        self.assigned_words[name] = word

    def select_words(self, key: str, num: int) -> List[str]:
        left = self.words_left[key]
        if len(left) < num:
            print('(Re)shuffling words for {} {}'.format(key, num))
            left = list(wordlists[key])
            random.shuffle(left)
        self.words_left[key] = left[num:]
        return left[:num]

    def start_round(self, val: dict) -> bool:
        if self.round != val.get('round'):
            print('Start fail: round out of sync')
            return False
        if time.time() < self.last_start + 2:
            print('Start fail: too soon')
            return False
        self.round += 1
        self.last_start = time.time()
        try:
            wordcount = int(val.get('wordcount', 18))
        except ValueError as e:
            wordcount = 18

        words = self.select_words(val['wordlist'], wordcount)
        named_clients = list(self.get_named_clients())
        random.shuffle(named_clients)
        half = len(named_clients) // 2
        word1, word2 = random.sample(words, 2)
        self.players_in_round = list(self.get_player_names())
        self.clear_assigned_words()
        self.words = words
        print(', '.join(words))
        for i, (name, client) in enumerate(named_clients):
            word = word2 if i >= half else word1
            self.set_assigned_word(name, word)
        return True

    def get_words_shuffled(self) -> List[str]:
        copy = list(self.words)
        random.shuffle(copy)
        return copy

class CastlefallFactory(WebSocketServerFactory):
    def __init__(self, *args, **kwargs):
        super(CastlefallFactory, self).__init__(*args, **kwargs)
        # room -> (name -> client)
        self.rooms: Dict[str, Room] = collections.defaultdict(Room)
        self.status_for_peer: Dict[str, ClientStatus] = {} # peer -> status
        self.words = []

    def players_in_room(self, room: str) -> List[str]:
        return list(sorted(self.rooms[room].get_player_names()))

    def register(self, room_name: str, name: str, client: CastlefallProtocol) -> None:
        room = self.rooms[room_name]
        if room.has_player(name):
            old_client = room.get_player_client(name)
            self.send(old_client, {
                'msg': 'Disconnected: your name was taken.',
            })
            del self.status_for_peer[old_client.peer]
            # del room_dict[name] # will get overwritten

        room.set_player_client(name, client)
        self.status_for_peer[client.peer] = ClientStatus(room_name, name)
        self.broadcast(room, {'players': room.get_player_names()})
        self.send(client, {
            'room': room_name,
            'round': room.round,
            'playersinround': room.players_in_round,
            'words': self.words,
            'word': room.get_assigned_word(name),
            'wordlists': [[k, len(v)] for k, v in sorted(wordlists.items())],
            'version': version,
        })

    def unregister(self, client: CastlefallProtocol) -> None:
        if client.peer in self.status_for_peer:
            status = self.status_for_peer[client.peer]
            del self.status_for_peer[client.peer]
            room = self.rooms[status.room]
            if status.name:
                if room.has_player(status.name):
                    room.delete_player_client(status.name)
                else:
                    print("client's peer had name, but its name wasn't there :(")
            self.broadcast(room, {'players': room.get_player_names()})

    def kick(self, client: CastlefallProtocol, name: str):
        if client.peer not in self.status_for_peer:
            return
        room_name = self.status_for_peer[client.peer].room
        room = self.rooms[room_name]
        if room.has_player(name):
            client = room.get_player_client(name)
            self.send(client, {
                'msg': 'Disconnected: you were kicked.',
            })
            room.delete_player_client(name)
            if client.peer in self.status_for_peer:
                del self.status_for_peer[client.peer]
            else:
                print("name had client, but the peer wasn't there :(")
        self.broadcast(room, {'players': room.get_player_names()})

    def broadcast(self, room: Room, obj: dict) -> None:
        payload = json_to_bytes(obj)
        for client in room.get_clients():
            client.sendMessage(payload)

    def send(self, client: CastlefallProtocol, obj: dict) -> None:
        client.sendMessage(json_to_bytes(obj))

    def room_of(self, client: CastlefallProtocol) -> Optional[Room]:
        if client.peer in self.status_for_peer:
            return self.rooms[self.status_for_peer[client.peer].room]
        else:
            return None

    def start_round(self, client: CastlefallProtocol, val: dict) -> None:
        room = self.room_of(client)
        if room.start_round(val):
            for name, client in room.get_named_clients():
                client.sendMessage(json_to_bytes({
                    'round': room.round,
                    'playersinround': room.players_in_round,
                    'words': room.get_words_shuffled(),
                    'word': room.get_assigned_word(name),
                }))

if __name__ == "__main__":
    log.startLogging(sys.stdout)

    if sys.argv and sys.argv[0] == "prod":
        print("Prod server")
        factory = CastlefallFactory("ws://127.0.0.1:8372")
    else:
        print("Dev server")
        factory = CastlefallFactory("ws://localhost:8372")

    factory.protocol = CastlefallProtocol
    resource = WebSocketResource(factory)

    reactor.listenTCP(8372, factory)
    reactor.run()