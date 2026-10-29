#!/usr/bin/env python3
"""Serveur en ligne de Solascii.

- Comptes sans mot de passe : chaque joueur reçoit un identifiant (« Pseudo#1234 ») et un jeton
  secret gardé sur son appareil.
- Amis : demandes par identifiant, acceptation, présence (en ligne, dans un salon).
- Salons : publics, entre amis ou privés (rejoints par code), invitations, 4 joueurs maximum.
- Relais : tout le trafic du jeu passe par ce serveur (WebSocket), donc aucun port à ouvrir
  chez les joueurs, même sur téléphone.

Lancer : python serveur.py   (variables : PORT, DATA_DIR, DATABASE_URL)

Stockage des comptes : si DATABASE_URL est défini (base Postgres, ex. Neon gratuit), les comptes
et listes d'amis y sont gardés pour toujours ; sinon, dans le fichier DATA_DIR/comptes.json.
"""
import asyncio
import hashlib
import json
import os
import random
import re
import secrets
import string
import struct

import websockets

PORT = int(os.environ.get("PORT", "8770"))
DATA_DIR = os.environ.get("DATA_DIR", os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.path.join(DATA_DIR, "comptes.json")
DATABASE_URL = os.environ.get("DATABASE_URL", "")
SAVE_EVERY = 3.0          # secondes entre deux sauvegardes (seulement si quelque chose a changé)
MAX_PLAYERS = 4
NAME_RE = re.compile(r"^[\w\- ]{2,16}$")

accounts = {}   # identifiant -> {"name", "token", "friends": [ids], "requests": [ids]}
clients = {}    # identifiant -> Client (joueurs connectés)
lobbies = {}    # code -> Lobby


class Client:
	def __init__(self, ws):
		self.ws = ws
		self.id = ""
		self.lobby = None   # code du salon
		self.peer = 0       # identifiant réseau dans le salon (1 = hôte)


class Lobby:
	def __init__(self, code, name, host, visibility):
		self.code = code
		self.name = name
		self.host = host
		self.visibility = visibility   # public | friends | private
		self.members = {}              # identifiant -> pair réseau


# ---------------------------------------------------------------- stockage

_dirty = False

SQL_CREATE = "CREATE TABLE IF NOT EXISTS solascii (cle TEXT PRIMARY KEY, valeur TEXT NOT NULL)"
SQL_READ = "SELECT valeur FROM solascii WHERE cle = %s"
SQL_WRITE = ("INSERT INTO solascii (cle, valeur) VALUES (%s, %s) "
	"ON CONFLICT (cle) DO UPDATE SET valeur = EXCLUDED.valeur")


def _db():
	import psycopg
	return psycopg.connect(DATABASE_URL, autocommit=True)


def load():
	global accounts
	if DATABASE_URL:
		with _db() as conn:
			conn.execute(SQL_CREATE)
			row = conn.execute(SQL_READ, ("comptes",)).fetchone()
			accounts = json.loads(row[0]) if row else {}
		print("Comptes chargés depuis la base de données : %d" % len(accounts), flush=True)
	elif os.path.exists(DB_PATH):
		with open(DB_PATH, encoding="utf-8") as f:
			accounts = json.load(f)


def save():
	"""Marque les comptes à sauvegarder ; l'écriture réelle se fait en arrière-plan."""
	global _dirty
	_dirty = True


def _write(text):
	if DATABASE_URL:
		with _db() as conn:
			conn.execute(SQL_WRITE, ("comptes", text))
	else:
		tmp = DB_PATH + ".tmp"
		with open(tmp, "w", encoding="utf-8") as f:
			f.write(text)
		os.replace(tmp, DB_PATH)


async def saver():
	global _dirty
	while True:
		await asyncio.sleep(SAVE_EVERY)
		if _dirty:
			_dirty = False
			try:
				await asyncio.to_thread(_write, json.dumps(accounts, ensure_ascii=False))
			except Exception as e:  # base injoignable : on réessaiera
				_dirty = True
				print("Sauvegarde impossible :", e, flush=True)


def hash_token(t):
	return hashlib.sha256(t.encode()).hexdigest()


# ---------------------------------------------------------------- envois

async def send(c, obj):
	try:
		await c.ws.send(json.dumps(obj, ensure_ascii=False))
	except websockets.ConnectionClosed:
		pass


def name_of(uid):
	return accounts.get(uid, {}).get("name", uid)


def visible_lobby_for(viewer, lobby):
	if lobby.visibility == "public":
		return True
	return lobby.visibility == "friends" and lobby.host in accounts.get(viewer, {}).get("friends", [])


def lobby_view(lobby):
	return {"code": lobby.code, "name": lobby.name, "host": lobby.host, "host_name": name_of(lobby.host),
		"visibility": lobby.visibility, "count": len(lobby.members), "max": MAX_PLAYERS}


async def push_friends(uid):
	c = clients.get(uid)
	if c is None:
		return
	acc = accounts[uid]
	friends = []
	for fid in acc["friends"]:
		fc = clients.get(fid)
		lob = None
		if fc is not None and fc.lobby in lobbies and lobbies[fc.lobby].visibility != "private":
			lob = lobbies[fc.lobby].code
		friends.append({"id": fid, "name": name_of(fid), "online": fc is not None, "lobby": lob})
	reqs = [{"id": r, "name": name_of(r)} for r in acc["requests"]]
	await send(c, {"t": "friends", "friends": friends, "requests": reqs})


async def push_friends_of(uid):
	for fid in accounts.get(uid, {}).get("friends", []):
		await push_friends(fid)


async def push_lobby_state(lobby):
	members = [{"id": m, "name": name_of(m), "peer": p} for m, p in lobby.members.items()]
	for m, p in lobby.members.items():
		c = clients.get(m)
		if c is not None:
			view = lobby_view(lobby)
			view.update({"t": "lobby", "members": members, "you_peer": p})
			await send(c, view)


# ---------------------------------------------------------------- salons

async def leave_lobby(c, reason="Le salon a été fermé par l'hôte."):
	lobby = lobbies.get(c.lobby)
	c.lobby = None
	c.peer = 0
	if lobby is None:
		return
	if lobby.host == c.id:
		del lobbies[lobby.code]
		for m in list(lobby.members):
			mc = clients.get(m)
			if mc is not None and mc is not c:
				mc.lobby = None
				mc.peer = 0
				await send(mc, {"t": "lobby_closed", "reason": reason})
			await push_friends_of(m)
	else:
		peer = lobby.members.pop(c.id, 0)
		host = clients.get(lobby.host)
		if host is not None and peer:
			await send(host, {"t": "peer_leave", "peer": peer})
		await push_lobby_state(lobby)
		await push_friends_of(c.id)


async def handle(c, msg):
	op = msg.get("op")
	acc = accounts.get(c.id)
	if op == "hello":
		uid, token = str(msg.get("id", "")), str(msg.get("token", ""))
		name = str(msg.get("name", "Errant")).strip()[:16] or "Errant"
		if not NAME_RE.match(name):
			name = "Errant"
		if uid in accounts and accounts[uid]["token"] == hash_token(token):
			if uid in clients:
				await send(clients[uid], {"t": "error", "msg": "Connecté depuis un autre appareil."})
				await clients[uid].ws.close()
			accounts[uid]["name"] = name
		else:
			while True:
				uid = "%s#%04d" % (name.replace(" ", ""), random.randint(0, 9999))
				if uid not in accounts:
					break
			token = secrets.token_hex(24)
			accounts[uid] = {"name": name, "token": hash_token(token), "friends": [], "requests": []}
		save()
		c.id = uid
		clients[uid] = c
		await send(c, {"t": "welcome", "id": uid, "token": token, "name": name})
		await push_friends(uid)
		await push_friends_of(uid)
		return
	if acc is None:
		await send(c, {"t": "error", "msg": "Non identifié."})
		return
	if op == "friend_add":
		target = str(msg.get("id", "")).strip()
		if target not in accounts or target == c.id:
			await send(c, {"t": "error", "msg": "Identifiant introuvable : %s" % target})
		elif target in acc["friends"]:
			await send(c, {"t": "info", "msg": "%s est déjà ton ami." % name_of(target)})
		elif target in acc["requests"]:
			await handle(c, {"op": "friend_accept", "id": target})
		elif c.id not in accounts[target]["requests"]:
			accounts[target]["requests"].append(c.id)
			save()
			await send(c, {"t": "info", "msg": "Demande d'ami envoyée à %s." % name_of(target)})
			if target in clients:
				await send(clients[target], {"t": "friend_request", "id": c.id, "name": acc["name"]})
				await push_friends(target)
	elif op == "friend_accept":
		other = str(msg.get("id", ""))
		if other in acc["requests"] and other in accounts:
			acc["requests"].remove(other)
			if other not in acc["friends"]:
				acc["friends"].append(other)
			if c.id not in accounts[other]["friends"]:
				accounts[other]["friends"].append(c.id)
			if c.id in accounts[other]["requests"]:
				accounts[other]["requests"].remove(c.id)
			save()
			await push_friends(c.id)
			await push_friends(other)
			if other in clients:
				await send(clients[other], {"t": "info", "msg": "%s a accepté ta demande d'ami." % acc["name"]})
	elif op == "friend_decline":
		other = str(msg.get("id", ""))
		if other in acc["requests"]:
			acc["requests"].remove(other)
			save()
			await push_friends(c.id)
	elif op == "friend_remove":
		other = str(msg.get("id", ""))
		if other in acc["friends"]:
			acc["friends"].remove(other)
			if other in accounts and c.id in accounts[other]["friends"]:
				accounts[other]["friends"].remove(c.id)
			save()
			await push_friends(c.id)
			await push_friends(other)
	elif op == "lobby_list":
		out = [lobby_view(l) for l in lobbies.values() if visible_lobby_for(c.id, l) and len(l.members) < MAX_PLAYERS]
		await send(c, {"t": "lobbies", "list": out})
	elif op == "lobby_create":
		await leave_lobby(c)
		vis = msg.get("visibility", "public")
		if vis not in ("public", "friends", "private"):
			vis = "public"
		while True:
			code = "".join(random.choice(string.ascii_uppercase + string.digits) for _ in range(6))
			if code not in lobbies:
				break
		name = str(msg.get("name", "")).strip()[:24] or "Salon de %s" % acc["name"]
		lobby = Lobby(code, name, c.id, vis)
		lobby.members[c.id] = 1
		lobbies[code] = lobby
		c.lobby, c.peer = code, 1
		await push_lobby_state(lobby)
		await push_friends_of(c.id)
	elif op == "lobby_join":
		code = str(msg.get("code", "")).strip().upper()
		lobby = lobbies.get(code)
		if lobby is None:
			await send(c, {"t": "error", "msg": "Salon introuvable : %s" % code})
		elif c.id in lobby.members:
			await push_lobby_state(lobby)
		elif len(lobby.members) >= MAX_PLAYERS:
			await send(c, {"t": "error", "msg": "Ce salon est complet (%d joueurs)." % MAX_PLAYERS})
		else:
			await leave_lobby(c)
			used = set(lobby.members.values())
			peer = 1
			while peer in used or peer <= 1:
				peer = random.randint(2, 2_000_000_000)
			lobby.members[c.id] = peer
			c.lobby, c.peer = code, peer
			host = clients.get(lobby.host)
			if host is not None:
				await send(host, {"t": "peer_join", "peer": peer, "name": acc["name"]})
			await push_lobby_state(lobby)
			await push_friends_of(c.id)
	elif op == "lobby_leave":
		await leave_lobby(c)
	elif op == "lobby_invite":
		target = str(msg.get("id", ""))
		lobby = lobbies.get(c.lobby)
		if lobby is not None and target in acc["friends"] and target in clients:
			await send(clients[target], {"t": "invite", "from": c.id, "from_name": acc["name"], "code": lobby.code, "lobby": lobby.name})
			await send(c, {"t": "info", "msg": "Invitation envoyée à %s." % name_of(target)})
	elif op == "lobby_kick":
		lobby = lobbies.get(c.lobby)
		peer = int(msg.get("peer", 0))
		if lobby is not None and lobby.host == c.id:
			for m, p in list(lobby.members.items()):
				if p == peer and m != c.id:
					mc = clients.get(m)
					if mc is not None:
						await leave_lobby(mc)
						await send(mc, {"t": "lobby_closed", "reason": "Tu as été retiré du salon."})


async def relay(c, data):
	"""Paquet de jeu : [cible int32][mode u8][canal u8][données] -> [expéditeur][mode][canal][données]."""
	lobby = lobbies.get(c.lobby)
	if lobby is None or len(data) < 6:
		return
	target, mode, chan = struct.unpack_from("<iBB", data)
	out = struct.pack("<iBB", c.peer, mode, chan) + data[6:]
	dests = []
	if c.peer != 1:
		dests = [m for m, p in lobby.members.items() if p == 1]        # un client ne parle qu'à l'hôte
	elif target == 0:
		dests = [m for m, p in lobby.members.items() if p != 1]
	elif target > 0:
		dests = [m for m, p in lobby.members.items() if p == target]
	else:
		dests = [m for m, p in lobby.members.items() if p != 1 and p != -target]
	for m in dests:
		mc = clients.get(m)
		if mc is not None:
			try:
				await mc.ws.send(out)
			except websockets.ConnectionClosed:
				pass


async def session(ws):
	c = Client(ws)
	try:
		async for msg in ws:
			if isinstance(msg, bytes):
				await relay(c, msg)
			else:
				try:
					data = json.loads(msg)
				except ValueError:
					continue
				if isinstance(data, dict):
					await handle(c, data)
	except websockets.ConnectionClosed:
		pass
	finally:
		if c.id and clients.get(c.id) is c:
			await leave_lobby(c)
			del clients[c.id]
			await push_friends_of(c.id)


def health(connection, request):
	"""Répond aux simples visites web (vérifications de l'hébergeur) ; laisse passer les WebSocket."""
	if request.headers.get("Upgrade", "").lower() != "websocket":
		return connection.respond(200, "Solascii OK\n")
	return None


async def main():
	load()
	async with websockets.serve(session, "0.0.0.0", PORT, max_size=2 ** 22, ping_interval=20, process_request=health):
		print("Serveur Solascii en ligne sur le port %d" % PORT, flush=True)
		await saver()


if __name__ == "__main__":
	asyncio.run(main())
