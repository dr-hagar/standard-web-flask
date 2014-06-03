"""
Script that should run every minute. Collects and stores stats from all servers to the db.
"""
from standardweb.models import *
from standardweb.lib import api
from standardweb.lib.constants import *

from sqlalchemy import not_
from sqlalchemy.orm import joinedload

from datetime import datetime, timedelta
import time

import requests

import rollbar


def _handle_groups(server, server_groups):
    group_uids = [x['uid'] for x in server_groups]
    groups = Group.query.filter(Group.server == server, Group.uid.in_(group_uids))
    group_map = {x.uid: x for x in groups}

    group_playerstat_ids = []

    removed_group_ids = []
    removed_groups = Group.query.filter(Group.server == server, not_(Group.uid.in_(group_uids)))
    for group in removed_groups:
        removed_group_ids.append(group.id)

    if removed_group_ids:
        removed_invites = GroupInvite.query.filter(GroupInvite.group_id.in_(removed_group_ids))

        for group_invite in removed_invites:
            db.session.delete(group_invite)

        db.session.flush()

        for group in removed_groups:
            db.session.delete(group)

    db.session.flush()

    for group_info in server_groups:
        uid = group_info['uid']
        name = group_info['name']
        established = group_info['established']
        land_count = group_info['land_count']
        land_limit = group_info['land_limit']
        lock_count = group_info['lock_count']
        members = group_info['members']
        leader = group_info['leader']
        moderators = group_info['moderators']
        invites = group_info['invites']

        established = datetime.utcfromtimestamp(established / 1000)
        moderators = set(moderators)
        invites = set(invites)

        group = group_map.get(uid)
        if not group:
            group = Group(uid=uid, server=server)

        group.name = name
        group.established = established
        group.land_count = land_count
        group.land_limit = land_limit
        group.member_count = len(members)
        group.lock_count = lock_count
        group.save(commit=False)

        if members:
            stats = [p for p in PlayerStats.query.options(
                joinedload(PlayerStats.player)
            ).join(Player).filter(PlayerStats.server == server, Player.username.in_(members))]

            for stat in stats:
                group_playerstat_ids.append(stat.id)

                if stat.player.username == leader:
                    stat.is_leader = True
                    stat.is_moderator = False
                elif stat.player.username in moderators:
                    stat.is_leader = False
                    stat.is_moderator = True
                else:
                    stat.is_leader = False
                    stat.is_moderator = False

                stat.group = group
                stat.save(commit=False)

            if group.id and invites:
                removed_invites = GroupInvite.query.filter(GroupInvite.group_id == group.id,
                                                           not_(GroupInvite.invite.in_(invites)))
                for group_invite in removed_invites:
                    db.session.delete(group_invite)

                existing_invites = GroupInvite.query.filter_by(group=group)
                existing_invites = set([x.invite for x in existing_invites])

                for invite in (invites - existing_invites):
                    group_invite = GroupInvite(group=group, invite=invite)
                    group_invite.save(commit=False)

    for groupless in PlayerStats.query.filter(PlayerStats.server == server,
                                              PlayerStats.group_id.isnot(None),
                                              not_(PlayerStats.id.in_(group_playerstat_ids))):
        groupless.group = None
        groupless.is_leader = False
        groupless.is_moderator = False
        groupless.save(commit=False)


def _query_server(server, mojang_status):
    server_status = api.get_server_status(server) or {}
    
    player_stats = []
    
    online_player_ids = []
    for player_info in server_status.get('players', []):
        username = player_info['username']
        uuid = player_info['uuid']

        player = Player.query.options(
            joinedload(Player.titles)
        ).filter_by(uuid=uuid).first()

        if player:
            if player.username != username:
                player.username = username
                player.save(commit=False)
        else:
            player = Player(username=username, uuid=uuid)
            player.save(commit=False)
        
        online_player_ids.append(player.id)

        last_activity = PlayerActivity.query.filter_by(server=server, player=player)\
            .order_by(PlayerActivity.timestamp.desc()).first()
        
        # if the last activity for this player is an 'exit' activity (or there isn't an activity),
        # create a new 'enter' activity since they just joined this minute
        if not last_activity or last_activity.activity_type == PLAYER_ACTIVITY_TYPES['exit']:
            enter = PlayerActivity(server=server, player=player,
                                   activity_type=PLAYER_ACTIVITY_TYPES['enter'])
            enter.save(commit=False)
        
        # respect nicknames from the main server
        if server.id == app.config['MAIN_SERVER_ID']:
            nickname_ansi = player_info.get('nickname_ansi')
            nickname = player_info.get('nickname')

            player.nickname_ansi = nickname_ansi
            player.nickname = nickname
            player.save(commit=False)
        
        ip = player_info.get('address')
        
        if ip:
            if not IPTracking.query.filter_by(ip=ip, player=player).first():
                existing_player_ip = IPTracking(ip=ip, player=player)
                existing_player_ip.save(commit=False)

        stats = PlayerStats.query.filter_by(server=server, player=player).first()
        if not stats:
            stats = PlayerStats(server=server, player=player)

        stats.last_seen = datetime.utcnow()
        stats.pvp_logs = player_info.get('pvp_logs')
        stats.time_spent = (stats.time_spent or 0) + 1
        stats.save(commit=False)

        titles = [{'name': x.name, 'broadcast': x.broadcast} for x in player.titles]

        player_stats.append({
            'username': player.username,
            'uuid': player.uuid,
            'minutes': stats.time_spent,
            'rank': stats.rank,
            'titles': titles
        })

    _handle_groups(server, server_status.get('groups', []))

    five_minutes_ago = datetime.utcnow() - timedelta(minutes=10)
    result = PlayerStats.query.filter(PlayerStats.server == server,
                                      PlayerStats.last_seen > five_minutes_ago)
    recent_player_ids = [x.player_id for x in result]
    
    # find all players that have recently left and insert an 'exit' activity for them
    # if their last activity was an 'enter'
    for player_id in set(recent_player_ids) - set(online_player_ids):
        latest_activity = PlayerActivity.query.filter_by(server=server, player_id=player_id)\
        .order_by(PlayerActivity.timestamp.desc()).first()
        
        if latest_activity and latest_activity.activity_type == PLAYER_ACTIVITY_TYPES['enter']:
            ex = PlayerActivity(server=server, player_id=player_id,
                                activity_type=PLAYER_ACTIVITY_TYPES['exit'])
            ex.save(commit=False)
    
    player_count = server_status.get('numplayers', 0) or 0
    cpu_load = server_status.get('load', 0) or 0
    tps = server_status.get('tps', 0) or 0
    
    status = ServerStatus(server=server, player_count=player_count, cpu_load=cpu_load, tps=tps)
    status.save(commit=True)

    api.send_stats(server, {
        'player_stats': player_stats,
        'session': mojang_status.session,
        'account': mojang_status.account,
        'auth': mojang_status.auth
    })


def _get_mojang_status():
    statuses = {}

    try:
        resp = requests.get('http://status.mojang.com/check')
        result = resp.json()

        for status in result:
            for k, v in status.items():
                statuses[k] = v == 'green'
    except:
        pass

    mojang_status = MojangStatus(website=statuses.get('minecraft.net', False),
                                 session=statuses.get('session.minecraft.net', False),
                                 account=statuses.get('account.mojang.com', False),
                                 auth=statuses.get('auth.mojang.com', False),
                                 skins=statuses.get('skins.minecraft.net', False))
    mojang_status.save(commit=True)

    return mojang_status


def main():
    mojang_status = _get_mojang_status()

    durations = []

    for server in Server.query.filter_by(online=True):
        start = int(round(time.time() * 1000))

        try:
            _query_server(server, mojang_status)
        except:
            db.session.rollback()
            rollbar.report_exc_info(extra_data={'server_id': server.id})
            raise
        else:
            db.session.commit()
            duration = int(round(time.time() * 1000)) - start
            durations.append((server.id, duration))
            print 'Done with server %d in %d milliseconds' % (server.id, duration)

    extra_data = {'server.%d.ms' % server_id: duration for server_id, duration in durations}
    extra_data['session'] = mojang_status.session
    rollbar.report_message('Server queries complete', 'debug',
                           extra_data=extra_data)


if __name__ == '__main__':
    main()
