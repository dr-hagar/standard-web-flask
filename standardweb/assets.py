import logging
from flask.ext.assets import Bundle, Environment
from webassets.script import CommandLineEnvironment

from standardweb import app


assets_env = Environment(app)

js = Bundle(
    Bundle(
        'js/thirdparty/jquery-1.8.3.min.js',
        'js/thirdparty/jquery.flot.min.js',
        'js/thirdparty/jquery.placeholder.min.js',
        'js/thirdparty/jquery.tipsy.min.js',
        'js/thirdparty/jquery.sceditor.min.js',
        'js/thirdparty/jquery.sceditor.bbcode.min.js',
        'js/thirdparty/moment.min.js',
        'js/thirdparty/socket.io.min.js',
        'js/thirdparty/soundmanager2.min.js',
        'js/thirdparty/ZeroClipboard.min.js'
    ),
    Bundle(
        'js/local/base.js',
        'js/local/chat.js',
        'js/local/console.js',
        'js/local/graph.js',
        'js/local/messages.js',
        'js/local/realtime.js',
        'js/local/site.js'
    ),
    filters='uglifyjs',
    output='js/all.min.js'
)

css = Bundle(
    'css/font-awesome.min.css',
    'css/style.css',
    'css/tipsy.css',
    'css/bbcode.css',
    filters='cssmin',
    output='css/all.min.css'
)

assets_env.register('js_all', js)
assets_env.register('css_all', css)

if not app.config['DEBUG']:
    log = logging.getLogger('webassets')
    log.addHandler(logging.StreamHandler())
    log.setLevel(logging.DEBUG)
    cmdenv = CommandLineEnvironment(assets_env, log)
    cmdenv.build()
