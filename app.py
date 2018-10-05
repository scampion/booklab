import hashlib
import logging
import os
from secrets import token_hex

import redis
import yaml
from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from flask import Flask, redirect, url_for, session, request, jsonify, Response, render_template
from flask_oauthlib.client import OAuth

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

oauth = OAuth(app)
gitlab = oauth.remote_app('gitlab',
                          base_url=('https://%s/api/v4/' % conf['gitlab']['host']),
                          request_token_url=None,
                          access_token_url=('https://%s/oauth/token' % conf['gitlab']['host']),
                          authorize_url=('https://%s/oauth/authorize' % conf['gitlab']['host']),
                          access_token_method='POST',
                          consumer_key=conf['gitlab']['consumer_key'],
                          consumer_secret=conf['gitlab']['consumer_secret']
                          )


def request_wants_json():
    print(request.accept_mimetypes)
    best = request.accept_mimetypes.best_match(['application/json', 'text/html'])
    return best == 'application/json' and request.accept_mimetypes[best] > request.accept_mimetypes['text/html']


@app.route('/')
def index():
    rc.hset("conf", 'host', request.host)
    if 'gitlab_token' in session:
        return render_template("index.html")
    return redirect(url_for('.login'))


@app.route('/projects')
def projects():
    if 'gitlab_token' in session:
        pr = gitlab.get('projects')
        print(pr.data)
        return jsonify([{'id': p['id'], 'name': p['path_with_namespace']} for p in pr.data])
    else:
        return redirect(url_for('.login'))


@app.route('/branches/<int:id>')
def branches(id):
    if 'gitlab_token' in session:
        url = 'projects/%s/repository/branches' % id
        branches = [branch['name'] for branch in gitlab.get(url).data if type(branch) == dict]
        return jsonify(branches)
    else:
        return redirect(url_for('.login'))


@app.route('/build')
def build():
    if 'gitlab_token' in session:
        me = gitlab.get('user')
        username = me.data['username']
        branch = request.args.get('branch')
        id = request.args.get('id')
        path = gitlab.get('projects/%s' % id).data['path_with_namespace']

        rc.hset("status", "%s:%s:%s" % (path, branch, username), "todo")
        token = token_hex(16)
        rc.setex("token:%s:%s:%s" % (path, branch, username), token, 60 * 60 * 24)
        setup_ssh(id, path, branch, username)

        nburl = "http://%s" % hashlib.sha1((path + branch + username).encode('utf8')).hexdigest()[0:8]
        nburl += "." + request.host
        nburl += "/tree/?token=%s" % token
        return render_template("deploy.html", path=path, branch=branch, nburl=nburl)
    else:
        return redirect(url_for('.login'))


@app.route('/deploy')
def deploy():
    if 'gitlab_token' in session:
        me = gitlab.get('user')
        username = me.data['username']

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
    else:
        return redirect(url_for('.login'))


@app.route('/status')
def status():
    if 'gitlab_token' in session:
        me = gitlab.get('user')
        username = me.data['username']
        path = request.args.get('path')
        branch = request.args.get('branch')
        status = rc.hget("status", "%s:%s:%s" % (path, branch, username))
        if status:
            return status.decode('utf8')
        else:
            return "undefined"
    else:
        return redirect(url_for('.login'))


@app.route('/log')
def log():
    if 'gitlab_token' in session:
        me = gitlab.get('user')
        username = me.data['username']
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
    else:
        return redirect(url_for('.login'))


def setup_ssh(id, path, branch, username):
    key = rsa.generate_private_key(backend=default_backend(), public_exponent=65537, key_size=1024)
    public_key = key.public_key().public_bytes(serialization.Encoding.OpenSSH, serialization.PublicFormat.OpenSSH)
    pem = key.private_bytes(encoding=serialization.Encoding.PEM, format=serialization.PrivateFormat.TraditionalOpenSSL,
                            encryption_algorithm=serialization.NoEncryption())

    rc.setex("ssh:%s:%s:%s" % (path, branch, username), pem.decode('utf-8'), SSH_EXP_TIME)
    data = {'id': id, 'title': 'booklab:%s:%s' % (branch, username), 'key': public_key.decode('utf-8'),
            'can_push': True}
    it = gitlab.post('projects/%s/deploy_keys' % id, data=data)
    assert it.status < 300, it.data


@app.route('/login')
def login():
    cb = url_for('.authorized', _external=True, _scheme='https')
    if 'callback' in conf:
        if 'scheme' in conf['callback']:
            cb = url_for('.authorized', _external=True, _scheme=conf['callback']['scheme'])
        if 'url' in conf['callback']:
            cb = conf['callback']['url']
    logger.info("Callback Oauth, %s", cb)
    return gitlab.authorize(callback=cb)


@app.route('/logout')
def logout():
    del session['gitlab_token']
    return redirect(url_for('.index'))


@app.route('/login/authorized')
def authorized():
    resp = gitlab.authorized_response()
    if resp is None:
        return 'Access denied: reason=%s error=%s' % (
            request.args['error'],
            request.args['error_description']
        )
    session['gitlab_token'] = (resp['access_token'], '')
    return redirect(url_for('.index'))


@gitlab.tokengetter
def get_gitlab_oauth_token():
    return session.get('gitlab_token')


if __name__ == "__main__":
    app.run()
