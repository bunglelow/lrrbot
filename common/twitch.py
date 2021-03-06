import json
import random
import asyncio
import socket

import common.http
from common import utils
from common.config import config

GAME_CHECK_INTERVAL = 5*60

def get_info_uncached(username=None, use_fallback=True):
	"""
	Get the Twitch info for a particular user or channel.

	Defaults to the stream channel if not otherwise specified.

	For response object structure, see:
	https://github.com/justintv/Twitch-API/blob/master/v3_resources/channels.md#example-response

	May throw exceptions on network/Twitch error.
	"""
	if username is None:
		username = config['channel']

	# Attempt to get the channel data from /streams/channelname
	# If this succeeds, it means the channel is currently live
	res = common.http.request("https://api.twitch.tv/kraken/streams/%s" % username)
	data = json.loads(res)
	channel_data = data.get('stream') and data['stream'].get('channel')
	if channel_data:
		channel_data['live'] = True
		channel_data['viewers'] = data['stream'].get('viewers')
		channel_data['stream_created_at'] = data['stream'].get('created_at')
		return channel_data

	if not use_fallback:
		return None

	# If that failed, it means the channel is offline
	# Ge the channel data from here instead
	res = common.http.request("https://api.twitch.tv/kraken/channels/%s" % username)
	channel_data = json.loads(res)
	channel_data['live'] = False
	return channel_data

@utils.cache(GAME_CHECK_INTERVAL, params=[0, 1])
def get_info(username=None, use_fallback=True):
	return get_info_uncached(username, use_fallback=use_fallback)

@utils.cache(GAME_CHECK_INTERVAL, params=[0, 1])
def get_game(name, all=False):
	"""
	Get the game information for a particular game.

	For response object structure, see:
	https://github.com/justintv/Twitch-API/blob/master/v3_resources/search.md#example-response-1

	May throw exceptions on network/Twitch error.
	"""
	search_opts = {
		'query': name,
		'type': 'suggest',
		'live': 'false',
	}
	res = common.http.request("https://api.twitch.tv/kraken/search/games", search_opts)
	res = json.loads(res)
	if all:
		return res['games']
	else:
		for game in res['games']:
			if game['name'] == name:
				return game
		return None

def get_game_playing(username=None):
	"""
	Get the game information for the game the stream is currently playing
	"""
	channel_data = get_info(username, use_fallback=False)
	if not channel_data or not channel_data['live']:
		return None
	if channel_data.get('game') is not None:
		return get_game(name=channel_data['game'])
	return None

def is_stream_live(username=None):
	"""
	Get whether the stream is currently live
	"""
	channel_data = get_info(username, use_fallback=False)
	return channel_data and channel_data['live']

@asyncio.coroutine
def get_subscribers(channel, token, count=5, offset=None, latest=True):
	headers = {
		"Authorization": "OAuth %s" % token,
	}
	data = {
		"limit": count,
		"direction": "desc" if latest else "asc",
	}
	if offset is not None:
		data['offset'] = offset
	res = yield from common.http.request_coro("https://api.twitch.tv/kraken/channels/%s/subscriptions" % channel, headers=headers, data=data)
	subscriber_data = json.loads(res)
	return [
		(sub['user']['display_name'], sub['user'].get('logo'), sub['created_at'])
		for sub in subscriber_data['subscriptions']
	]

@asyncio.coroutine
def get_group_servers(token, loop):
	"""
	Get the secondary Twitch chat servers
	"""
	res = yield from common.http.request_coro("https://chatdepot.twitch.tv/room_memberships", {'oauth_token': token}, maxtries=1)
	res = json.loads(res)
	def parse_server(s):
		if ':' in s:
			bits = s.split(':')
			return bits[0], int(bits[1])
		else:
			return s, 6667
	servers = set(parse_server(s) for m in res['memberships'] for s in m['room']['servers'])
	# each server appears in this multiple times with different ports... pick one port we prefer for each server
	server_dict = {}
	for host, port in servers:
		server_dict.setdefault(host, set()).add(port)
	def preferred_port(ports):
		if 6667 in ports:
			return 6667
		elif ports - {80, 443}:
			return random.choice(list(ports - {80, 443}))
		else:
			return random.choice(list(ports))
	servers = [(host, preferred_port(ports)) for host,ports in server_dict.items()]

	# Try to connect to all servers. Out of 10 servers 4 are actually up.
	connections = []
	for addr in servers:
		s = socket.socket()
		s.setblocking(False)
		connections.append((s, asyncio.async(asyncio.wait_for(loop.sock_connect(s, addr), 1, loop=loop), loop=loop)))
	done, pending = yield from asyncio.wait([future for s, future in connections], loop=loop)
	assert len(pending) == 0

	working_servers = []
	for address, (s, future) in zip(servers, connections):
		s.close()
		try:
			future.result()
			working_servers.append(address)
		except (IOError, asyncio.TimeoutError):
			# Connecting failed
			continue

	random.shuffle(working_servers)
	return working_servers

@asyncio.coroutine
def get_follows_channels(username=None):
	if username is None:
		username = config["username"]
	url = "https://api.twitch.tv/kraken/users/%s/follows/channels" % username
	follows = []
	total = 1
	while len(follows) < total:
		data = yield from common.http.request_coro(url)
		data = json.loads(data)
		total = data["_total"]
		follows += data["follows"]
		url = data["_links"]["next"]
	return follows

@asyncio.coroutine
def get_streams_followed(token):
	url = "https://api.twitch.tv/kraken/streams/followed"
	headers = {
		"Authorization": "OAuth %s" % token,
	}
	streams = []
	total = 1
	while len(streams) < total:
		data = yield from common.http.request_coro(url, headers=headers)
		data = json.loads(data)
		total = data["_total"]
		streams += data["streams"]
		url = data["_links"]["next"]
	return streams

@asyncio.coroutine
def follow_channel(target, token):
	headers = {
		"Authorization": "OAuth %s" % token,
	}
	yield from common.http.request_coro("https://api.twitch.tv/kraken/users/%s/follows/channels/%s" % (config["username"], target),
										data={"notifications": "false"}, method="PUT", headers=headers)

@asyncio.coroutine
def unfollow_channel(target, token):
	headers = {
		"Authorization": "OAuth %s" % token,
	}
	yield from common.http.request_coro("https://api.twitch.tv/kraken/users/%s/follows/channels/%s" % (config["username"], target),
										method="DELETE", headers=headers)

@asyncio.coroutine
def get_videos(channel=None, offset=0, limit=10, broadcasts=False, hls=False):
	channel = channel or config["channel"]
	data = yield from common.http.request_coro("https://api.twitch.tv/kraken/channels/%s/videos" % channel, data={
		"offset": offset,
		"limit": limit,
		"broadcasts": "true" if broadcasts else "false",
		"hls": hls,
	})
	return json.loads(data)["videos"]

def get_user(user):
	return json.loads(common.http.request("https://api.twitch.tv/kraken/users/%s" % user))
