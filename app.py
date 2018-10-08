import hashlib
import json
import logging
import os
import random
import string
from functools import wraps
from secrets import token_hex

import redis
import yaml
from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from flask import Flask, redirect, url_for, session, request, jsonify, Response, render_template
from authlib.flask.client import OAuth
from authlib.specs.rfc6749.wrappers import OAuth2Token
with open("config.yml", 'r') as stream:
    conf = yaml.load(stream)

# logging.basicConfig(filename=conf['logfile'], filemode='w', level=logging.DEBUG)
logger = logging.getLogger(__name__)
REDIS_URI = os.getenv("BOOKLAB_REDIS_URI", "redis://localhost:6379/0")
print(REDIS_URI)
rc = redis.from_url(REDIS_URI)
rc.hset("conf", 'gitlab_host', conf['gitlab']['host'])
SSH_EXP_TIME = conf.get('ssh_expiration_time', 60 * 60 * 24)

app = Flask(__name__)
app.debug = True
app.secret_key = 'development'


def fetch_gitlab_token():
    if not 'current_user' in session or not rc.exists("oauth:%s" % session['current_user']):
        return
    d = json.loads(rc.get("oauth:%s" % session['current_user']).decode('utf8'))
    return OAuth2Token.from_dict(d)


oauth = OAuth(app, cache=rc)
oauth.register('gitlab',
               api_base_url='https://%s/api/v4/' % conf['gitlab']['host'],
               request_token_url=None,
               access_token_url='https://%s/oauth/token' % conf['gitlab']['host'],
               authorize_url='https://%s/oauth/authorize' % conf['gitlab']['host'],
               access_token_method='POST',
               client_id=conf['gitlab']['consumer_key'],
               client_secret=conf['gitlab']['consumer_secret'],
               fetch_token=fetch_gitlab_token
               )


@app.route('/login')
def login():
    redirect_uri = url_for('authorize', _external=True)
    return oauth.gitlab.authorize_redirect(redirect_uri)


@app.route('/authorize')
def authorize():
    token = oauth.gitlab.authorize_access_token()
    print(type(token), token)
    print(json.dumps(token))
    user = ''.join(random.choices(string.ascii_lowercase, k=32))
    session['current_user'] = user
    rc.setex("oauth:%s" % user, json.dumps(token), 60 * 60 * 24 * 7)
    return redirect(url_for('index'))


@app.route('/logout')
def logout():
    del session['current_user']
    return redirect(url_for('.index'))


def login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not 'current_user' in session:  # or not rc.exists("oauth:%s" % session['current_user']):
            return redirect(url_for('login', next=request.url))
        return f(*args, **kwargs)

    return decorated_function


@app.route('/')
@login_required
def index():
    rc.hset("conf", 'host', request.host)
    nbofrunners = len([r for r in rc.smembers("runners") if rc.exists("heartbeat:" + r.decode('utf8'))])
    username = oauth.gitlab.get('user').json()['username']
    return render_template("index.html", username=username, nbofrunners=nbofrunners)


@app.route('/projects')
@login_required
def projects():
    return jsonify([{'id': p['id'], 'name': p['path_with_namespace']} for p in oauth.gitlab.get('projects').json()])


@app.route('/branches/<int:id>')
@login_required
def branches(id):
    url = 'projects/%s/repository/branches' % id
    branches = [branch['name'] for branch in oauth.gitlab.get(url).json() if type(branch) == dict]
    return jsonify(branches)


@app.route('/build')
@login_required
def build():
    username = oauth.gitlab.get('user').json()['username']
    branch = request.args.get('branch')
    id = request.args.get('id')
    path = oauth.gitlab.get('projects/%s' % id).json()['path_with_namespace']

    rc.hset("status", "%s:%s:%s" % (path, branch, username), "todo")
    token = token_hex(16)
    rc.setex("token:%s:%s:%s" % (path, branch, username), token, 60 * 60 * 24)
    setup_ssh(id, path, branch, username)

    nburl = "http://%s" % hashlib.sha1((path + branch + username).encode('utf8')).hexdigest()[0:8]
    nburl += "." + request.host
    nburl += "/tree/?token=%s" % token
    return render_template("deploy.html", path=path, branch=branch, nburl=nburl)


@app.route('/deploy')
@login_required
def deploy():
    username = oauth.gitlab.get('user').json()['username']
    id = request.args.get('id')
    path = request.args.get('path')
    branch = request.args.get('branch')

    rc.hset("status", "%s:%s:%s" % (path, branch, username), "todo")
    token = token_hex(16)
    rc.setex("token:%s:%s:%s" % (path, branch, username), token, 60 * 60 * 24)
    setup_ssh(id, path, branch, username)

    nburl = "http://%s" % hashlib.sha1((path + branch + username).encode('utf8')).hexdigest()[0:8]
    nburl += "." + request.host
    nburl += "/tree/?token=%s" % token
    return render_template("deploy.html", path=path, branch=branch, nburl=nburl)


@app.route('/status')
@login_required
def status():
    username = oauth.gitlab.get('user').json()['username']
    path = request.args.get('path')
    branch = request.args.get('branch')
    status = rc.hget("status", "%s:%s:%s" % (path, branch, username))
    if status:
        return status.decode('utf8')
    else:
        return "undefined"


@app.route('/log')
@login_required
def log():
    username = oauth.gitlab.get('user').json()['username']
    path = request.args.get('path')
    branch = request.args.get('branch')

    def event_stream(path, branch):
        pubsub = rc.pubsub()
        channel = "%s:%s_%s" % (path, branch, username)
        pubsub.subscribe(channel)
        for message in pubsub.listen():
            if message['type'] == 'message':
                if message['data'] == b'EOF':
                    return
                else:
                    yield "%s\n" % message['data'].decode('utf8')

    return Response(event_stream(path, branch), mimetype="text/event-stream")


def setup_ssh(id, path, branch, username):
    key = rsa.generate_private_key(backend=default_backend(), public_exponent=65537, key_size=1024)
    public_key = key.public_key().public_bytes(serialization.Encoding.OpenSSH, serialization.PublicFormat.OpenSSH)
    pem = key.private_bytes(encoding=serialization.Encoding.PEM, format=serialization.PrivateFormat.TraditionalOpenSSL,
                            encryption_algorithm=serialization.NoEncryption())

    rc.setex("ssh:%s:%s:%s" % (path, branch, username), pem.decode('utf-8'), SSH_EXP_TIME)
    key_id = 'booklab:%s:%s' % (branch, username)
    data = {'id': id, 'title': key_id, 'key': public_key.decode('utf-8'),
            'can_push': True}
    oauth.gitlab.delete('projects/%s/deploy_keys/%s' % (id, key_id))
    it = oauth.gitlab.post('projects/%s/deploy_keys' % id, data=data)
    assert it.status_code < 300, it.json()


if __name__ == "__main__":
    app.run()
