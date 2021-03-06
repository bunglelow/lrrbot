import asyncio
import random
import re
import math
import logging
import json
import traceback

import sqlalchemy

import common.utils
from common import utils
from common.config import config
from common import twitch
from lrrbot import googlecalendar, storage
import lrrbot.docstring

log = logging.getLogger('serverevents')

GLOBAL_FUNCTIONS = {}
def global_function(name=None):
	def wrapper(function):
		nonlocal name
		if name is None:
			name = function.__name__
		GLOBAL_FUNCTIONS[name] = function
		return function
	return wrapper

class Server:
	def __init__(self, lrrbot, loop):
		self.lrrbot = lrrbot
		self.loop = loop
		self.functions = dict(GLOBAL_FUNCTIONS)

	def add(self, name, function):
		self.functions[name] = function

	def remove(self, name):
		del self.functions[name]

	def function(self, name=None):
		def wrapper(function):
			nonlocal name
			if name is None:
				name = function.__name__
			self.add(name, function)
			return function
		return wrapper

	def __call__(self):
		return Protocol(self)

class Protocol(asyncio.Protocol):
	def __init__(self, server):
		self.server = server
		self.buffer = b""

	def connection_made(self, transport):
		self.transport = transport
		log.debug("Received event connection from server")

	def data_received(self, data):
		self.buffer += data
		if b"\n" in self.buffer:
			request = json.loads(self.buffer.decode())
			log.debug("Command from server (%s): %s(%r)", request['user'], request['command'], request['param'])
			try:
				response = self.server.functions[request['command']](self.server.lrrbot, request['user'], request['param'])
			except utils.PASSTHROUGH_EXCEPTIONS:
				raise
			except Exception:
				log.exception("Exception in on_server_event")
				response = {'success': False, 'result': ''.join(traceback.format_exc())}
			else:
				log.debug("Returning: %r", response)
				response = {'success': True, 'result': response}
			response = json.dumps(response).encode() + b"\n"
			self.transport.write(response)
			self.transport.close()

@global_function()
def current_game(lrrbot, user, data):
	game = lrrbot.get_current_game()
	if game:
		return game['id']
	else:
		return None

@global_function()
def current_game_name(lrrbot, user, data):
	game = lrrbot.get_current_game()
	if game:
		return game['name']
	else:
		return None

@global_function()
def get_data(lrrbot, user, data):
	if not isinstance(data['key'], (list, tuple)):
		data['key'] = [data['key']]
	node = storage.data
	for subkey in data['key']:
		node = node.get(subkey, {})
	return node

@global_function()
def set_data(lrrbot, user, data):
	if not isinstance(data['key'], (list, tuple)):
		data['key'] = [data['key']]
	log.info("Setting storage (%s) %s to %r" % (user, '.'.join(data['key']), data['value']))
	# if key is, eg, ["a", "b", "c"]
	# then we want to effectively do:
	# storage.data["a"]["b"]["c"] = value
	# But in case one of those intermediate dicts doesn't exist:
	# storage.data.setdefault("a", {}).setdefault("b", {})["c"] = value
	node = storage.data
	for subkey in data['key'][:-1]:
		node = node.setdefault(subkey, {})
	node[data['key'][-1]] = data['value']
	storage.save()

@global_function()
def get_commands(bot, user, data):
	ret = []
	for command in bot.commands.commands.values():
		doc = lrrbot.docstring.parse_docstring(command['func'].__doc__)
		for cmd in doc.walk():
			if cmd.get_content_maintype() == "multipart":
				continue
			if cmd.get_all("command") is None:
				continue
			ret += [{
				"aliases": cmd.get_all("command"),
				"mod-only": cmd.get("mod-only") == "true",
				"sub-only": cmd.get("sub-only") == "true",
				"public-only": cmd.get("public-only") == "true",
				"throttled": (int(cmd.get("throttle-count", 1)), int(cmd.get("throttled"))) if "throttled" in cmd else None,
				"literal-response": cmd.get("literal-response") == "true",
				"section": cmd.get("section"),
				"description": cmd.get_payload(),
			}]
	return ret

@global_function()
def get_header_info(lrrbot, user, data):
	game = lrrbot.get_current_game()
	live = twitch.is_stream_live()

	data = {
		"is_live": live,
		"channel": config['channel'],
	}

	if live and game is not None:
		data['current_game'] = {
			"name": game['name'],
			"display": game.get("display", game["name"]),
			"id": game["id"],
			"is_override": lrrbot.game_override is not None,
		}
		show = lrrbot.show_override or lrrbot.show
		data['current_show'] = {
			"id": show,
			"name": storage.data.get("shows", {}).get(show, {}).get("name", show),
		}
		stats = [{
			"count": v,
			"type": storage.data['stats'][k].get("singular" if v == 1 else "plural", k)
		} for (k, v) in game['stats'].items() if v]
		stats.sort(key=lambda i: (-i['count'], i['type']))
		data['current_game']['stats'] = stats
		if game.get("votes"):
			good = sum(game['votes'].values())
			total = len(game['votes'])
			data["current_game"]["rating"] = {
				"good": good,
				"total": total,
				"perc": 100.0 * good / total,
			}
		if user is not None:
			users = lrrbot.metadata.tables["users"]
			with lrrbot.engine.begin() as conn:
				name, = conn.execute(sqlalchemy.select([users.c.name]).where(users.c.id == user)).first()
			data["current_game"]["my_rating"] = game.get("votes", {}).get(name)
	elif not live:
		data['nextstream'] = googlecalendar.get_next_event_text(googlecalendar.CALENDAR_LRL)

	if 'advice' in storage.data['responses']:
		data['advice'] = random.choice(storage.data['responses']['advice']['response'])

	return data

@global_function()
def nextstream(lrrbot, user, data):
	return googlecalendar.get_next_event_text(googlecalendar.CALENDAR_LRL, verbose=False)

@global_function()
def set_show(bot, user, data):
	import lrrbot.commands
	lrrbot.commands.show.set_show(bot, data["show"])
	return {"status": "OK"}

@global_function()
def get_show(lrrbot, user, data):
	return lrrbot.show_override or lrrbot.show

@global_function()
def get_tweet(lrrbot, user, data):
	import lrrbot.commands
	mode = utils.weighted_choice([(0, 10), (1, 4), (2, 1)])
	if mode == 0: # get random !advice
		return random.choice(storage.data['responses']['advice']['response'])
	elif mode == 1: # get a random !quote
		quotes = lrrbot.metadata.tables["quotes"]
		with lrrbot.engine.begin() as conn:
			query = sqlalchemy.select([quotes.c.quote, quotes.c.attrib_name]).where(~quotes.c.deleted)
			row = common.utils.pick_random_elements(conn.execute(query), 1)[0]
		if row is None:
			return None

		quote, name = row

		quote_msg = "\"{quote}\"".format(quote=quote)
		if name:
			quote_msg += " —{name}".format(name=name)
		return quote_msg
	else: # get a random statistic
		show, game_id, stat = utils.weighted_choice(
			((show, game_id, stat), math.log(count))
			for show in storage.data['shows']
			for game_id in storage.data['shows'][show]['games']
			for stat in storage.data['stats']
			for count in [storage.data['shows'][show]['games'][game_id]['stats'].get(stat)]
			if count
		)
		game = storage.data['shows'][show]['games'][game_id]
		count = game['stats'][stat]
		display = storage.data['stats'][stat].get("singular", stat) if count == 1 else storage.data['stats'][stat].get("plural", stat + "s")
		return "%d %s for %s on %s" % (count, display, lrrbot.commands.game.game_name(game), lrrbot.commands.show.show_name(show))
