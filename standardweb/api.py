from flask import abort
from flask import g
from flask import jsonify
from flask import request

from functools import wraps

from standardweb.models import *
from standardweb.lib.csrf import exempt_funcs
from standardweb.lib import helpers as h

from sqlalchemy import or_
from sqlalchemy.sql.expression import func
from sqlalchemy.orm import joinedload

import rollbar


_server_cache = {}

# Base API function decorator that builds a list of view functions for use in urls.py. 
def api_func(function):

    @wraps(function)
    def decorator(*args, **kwargs):
        version = int(kwargs['version'])
        if version < 1 or version > 1:
            abort(404)
        
        del kwargs['version']
        return function(*args, **kwargs)

    exempt_funcs.add(decorator)

    name = function.__name__
    app.add_url_rule('/api/v<int:version>/%s' % name, name, decorator, methods=['GET', 'POST'])
    
    return decorator


# API function decorator for any api operation exposed to a Minecraft server.
# This access must be authorized by server-id/secret-key pair combination.
def server_api(function):

    @wraps(function)
    def decorator(*args, **kwargs):
        server = None

        if request.headers.get('Authorization'):
            auth = request.headers['Authorization'].split(' ')[1]
            server_id, secret_key = auth.strip().decode('base64').split(':')

            server = Server.query.filter_by(id=server_id, secret_key=secret_key).first()

        if not server:
            abort(403)

        setattr(g, 'server', server)
        
        return function(*args, **kwargs)

    api_func(decorator)
    
    return decorator


@server_api
def log_death():
    type = request.form.get('type')
    victim_uuid = request.form.get('victim_uuid')
    killer_uuid = request.form.get('killer_uuid')

    victim = Player.query.filter_by(uuid=victim_uuid).first()

    if victim_uuid == killer_uuid:
        type = 'suicide'

    death_type = DeathType.factory(type=type)

    if type == 'player':
        killer = Player.query.filter_by(uuid=killer_uuid).first()

        death_event = DeathEvent(server=g.server, death_type=death_type, victim=victim, killer=killer)
        DeathCount.increment(g.server, death_type, victim, killer, commit=False)
    else:
        death_event = DeathEvent(server=g.server, death_type=death_type, victim=victim)
        DeathCount.increment(g.server, death_type, victim, None, commit=False)

    death_event.save(commit=True)

    return jsonify({
        'err': 0
    })


@server_api
def log_kill():
    type = request.form.get('type')
    killer_uuid = request.form.get('killer_uuid')

    killer = Player.query.filter_by(uuid=killer_uuid).first()
    if not killer:
        return jsonify({
            'err': 1
        })

    kill_type = KillType.factory(type=type)

    kill_event = KillEvent(server=g.server, kill_type=kill_type, killer=killer)
    KillCount.increment(g.server, kill_type, killer, commit=False)

    kill_event.save(commit=True)

    return jsonify({
        'err': 0
    })


@server_api
def log_ore_discovery():
    uuid = request.form.get('uuid')
    type = request.form.get('type')
    x = int(request.form.get('x'))
    y = int(request.form.get('y'))
    z = int(request.form.get('z'))

    player = Player.query.filter_by(uuid=uuid).first()

    if not player:
        return jsonify({
            'err': 1
        })

    material_type = MaterialType.factory(type=type)

    ore_event = OreDiscoveryEvent(server=g.server, material_type=material_type,
                                  player=player, x=x, y=y, z=z)
    OreDiscoveryCount.increment(g.server, material_type, player, commit=False)

    ore_event.save(commit=True)

    return jsonify({
        'err': 0
    })


@server_api
def register():
    uuid = request.form.get('uuid')
    password = request.form.get('password')

    player = Player.query.filter_by(uuid=uuid).first()

    if not player:
        return jsonify({
            'err': 1,
            'message': 'Please try again later.'
        })

    user = User.query.filter_by(player=player).first()

    if user:
        user.set_password(password)
        user.save(commit=True)

        return jsonify({
            'err': 0,
            'message': 'Your website password has been changed!'
        })

    User.create(player, password, commit=True)

    rollbar.report_message('Website account created', level='info', request=request)

    return jsonify({
        'err': 0,
        'message': 'Your username has been linked to a website account!'
    })


@server_api
def rank_query():
    username = request.args.get('username')
    uuid = request.args.get('uuid')

    if uuid:
        player = Player.query.options(
            joinedload(Player.titles)
        ).filter_by(uuid=uuid).first()
    else:
        player = Player.query.options(
                joinedload(Player.titles)
            ).filter(or_(Player.username.ilike('%%%s%%' % username),
                                         Player.nickname.ilike('%%%s%%' % username)))\
            .order_by(func.ifnull(Player.nickname, Player.username))\
            .limit(1).first()

    if not player:
        return jsonify({
            'err': 1
        })

    stats = PlayerStats.query.filter_by(server=g.server, player=player).first()

    if not stats:
        return jsonify({
            'err': 1
        })

    time = h.elapsed_time_string(stats.time_spent)

    veteran_statuses = VeteranStatus.query.filter_by(player=player)
    for veteran_status in veteran_statuses:
        server_id = veteran_status.server_id
        rank = veteran_status.rank

        server_name = {
            1: 'SS I',
            2: 'SS II',
            4: 'SS III'
        }[server_id]

        if rank <= 10:
            veteran_group = 'Top 10 Veteran'
        elif rank <= 40:
            veteran_group = 'Top 40 Veteran'
        else:
            veteran_group = 'Veteran'

        title_name = '%s %s' % (server_name, veteran_group)
        title = Title.query.filter_by(name=title_name).first()

        if title and title not in player.titles:
            player.titles.append(title)
            player.save(commit=True)

    titles = [{'name': x.name, 'broadcast': x.broadcast} for x in player.titles]

    retval = {
        'err': 0,
        'rank': stats.rank,
        'time': time,
        'minutes': stats.time_spent,
        'username': player.username,
        'titles': titles
    }

    return jsonify(retval)


@server_api
def join_server():
    # do nothing for now
    return jsonify({
        'err': 0
    })


@server_api
def leave_server():
    username = request.form.get('username')

    player = Player.query.filter_by(username=username).first()

    if player:
        player_stats = PlayerStats.query.filter_by(server=g.server, player=player).first()

        if player_stats:
            player_stats.last_seen = datetime.utcnow()
            player_stats.save(commit=True)

    return jsonify({
        'err': 0
    })



@api_func
def servers():
    servers = [{
        'id': server.id,
        'address': server.address,
        'online': server.online
    } for server in Server.query.all()]

    return jsonify({
        'err': 0,
        'servers': servers
    })
