import hashlib
import logging
import os
import subprocess
import stat
import tempfile
import time
from secrets import token_hex

import datetime
import docker
import git
import redis
from docker import APIClient

logging.getLogger("urllib3").setLevel(logging.WARNING)
logging.basicConfig(level=logging.DEBUG)
log = logging.getLogger(__name__)

REDIS_URI = os.getenv("BOOKLAB_REDIS_URI", "redis://localhost:6379/0")
rc = redis.from_url(REDIS_URI)

cli = APIClient()
log_expiration_time = 60 * 60 * 24 * 7  # 7 days
client = docker.from_env()


def get_port():
    def exposed_ports():
        for c in client.containers.list():
            for v in c.attrs['NetworkSettings']['Ports'].values():
                if v:
                    for p in v:
                        yield int(p['HostPort'])

    port = 8888
    while port in set(exposed_ports()):
        port += 1
    return port


class Runner:
    """
    Runne class steps:
    1- git clone repo with branch in tmpd dir
    2- run docker build and store log lines in redis list key log:<path>:<branch> (expiration 7days)

    Between each step, status is updated in redis hash status with key <path>:<branch>
    """

    def __init__(self, path, branch, username, tmpdir):
        self.path = path
        self.branch = branch
        self.username = username
        self.tmpdir = tmpdir
        self.tag = "%s:%s_%s" % (path, branch, username)
        self.lk = "log:%s:%s:%s" % (path, branch, username)  # redis log key
        rc.delete(self.lk)
        rc.rpush(self.lk, "init")
        rc.expire(self.lk, log_expiration_time)

        gitlab_host = rc.hget("conf", 'gitlab_host').decode('utf8')
        self.git_url = "git@%s:%s" % (gitlab_host, self.path)
        private_key_path = self.checkout_private_ssh_key(tmpdir)
        self.git_ssh_cmd = 'ssh -o StrictHostKeyChecking=no -i %s' % private_key_path

        log.info("build started for %s:%s", path, branch)
        try:
            self.clone_repo()
            self.build()

        except (docker.errors.APIError, git.exc.GitCommandError) as e:
            rc.rpush(self.lk, str(e))
            self.status("error")
            log.error(str(e))
            print(str(e))

    def clone_repo(self):
        """
        Checkout the repository in a local directory

        The private ssh key to clone the repo is in the redis key ssh:path:branch and deleted after
        Will create 2 folders in tmpdirname:
            .ssh : with the private ssh key inside
            repo : with the source code
        TODO: instead of clone, pull update but GitPython doesn't allow it easly
        :return:
        """
        self.status("clone")
        self.log("clone repo %s" % self.git_url)
        git.Repo.clone_from(self.git_url, branch=self.branch, to_path=os.path.join(self.tmpdir, 'repo'),
                            env=dict(GIT_SSH_COMMAND=self.git_ssh_cmd))

    def checkout_private_ssh_key(self, tmpdir):
        ssh_rk = "ssh:%s:%s:%s" % (self.path, self.branch, self.username)
        if not rc.exists(ssh_rk):
            raise git.exc.GitCommandError("No ssh private key available for checkout")
        sshdir = os.path.join(tmpdir, '.ssh')
        os.mkdir(sshdir)
        private_key_path = os.path.join(sshdir, 'sshkey')
        with open(private_key_path, 'w+') as f:
            f.writelines(rc.get(ssh_rk).decode('utf8'))
            rc.delete(ssh_rk)
        os.chmod(sshdir, stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR)
        os.chmod(private_key_path, stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR)
        return private_key_path

    def build(self):
        """
        Build the docker image using jupyter-repo2docker

        logs are publish on channel path:branch and copied in a list log:path:branch (expire after 24 hours)
        :return:
        """
        self.status("build")
        log.info("run repo2docker to produce image " + self.tag)
        cmd = "/usr/bin/repo2docker --no-run " \
              "--user-id %s --user-name %s " \
              "--image-name %s %s" % (os.environ.get('BOOKLAB_UID'), os.environ.get('BOOKLAB_USER'),
                                      self.tag, os.path.join(self.tmpdir, 'repo'))
        log.info("repo2docker cmd %s", cmd)
        process = subprocess.Popen(cmd.split(), stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        for line in iter(process.stdout.readline, b''):
            self.log(line.rstrip())
        self.status("image")

    def parse_logs(self, container):
        logs = set()
        while container.status in ["running", "created"]:
            for l in container.logs().decode('utf8').split('\n'):
                if l not in logs:
                    self.log(l)
                    logs.add(l)
            time.sleep(1)
            container.reload()

    def run(self):
        self.status("run")
        taghostname = hashlib.sha1((self.path + self.branch + self.username).encode('utf8')).hexdigest()[0:8]
        traefik_labels = {"traefik.frontend.rule": "Host:%s.%s" % (taghostname, rc.hget("conf", "host").decode('utf8'))}
        nbtoken = rc.get("token:%s:%s:%s" % (self.path, self.branch, self.username))
        ep = "jupyter notebook --ip=0.0.0.0 --allow-root --no-browser --NotebookApp.token='%s'" % nbtoken.decode('utf8')
        repodir = os.path.join(os.environ.get('BOOKLAB_TMP'), self.tmpdir.split(os.path.sep)[-1], "repo")
        vol = {repodir: {'bind': '/home/%s' % os.environ.get('BOOKLAB_USER'), 'mode': 'rw'}}
        self.log(ep)
        self.log(vol)
        client.containers.run(self.tag, ep, ports={"8888/tcp": get_port()}, detach=False, stderr=True, volumes=vol,
                              user=os.environ.get('BOOKLAB_USER'), labels=traefik_labels, network="traefik")
        self.push_backup()

    def push_backup(self):
        repodir = os.path.join(self.tmpdir, 'repo')
        repo = git.Repo(repodir)
        branch = "booklab %s %s" % (self.username, datetime.datetime.now())
        branch = branch.replace(' ', '_').replace('.', '_').replace(':', '_')
        repo.git.checkout('HEAD', b=branch)
        repo.index.add([f for f in os.listdir(repodir) if not f.startswith('.')])
        repo.index.commit("backup")
        repo.git.push('origin', branch, env=dict(GIT_SSH_COMMAND=self.git_ssh_cmd))

    def status(self, s):
        rc.hset("status", "%s:%s:%s" % (self.path, self.branch, self.username), s)
        self.log("status  %s:%s:%s %s" % (self.path, self.branch, self.username, s))

    def log(self, l):
        rc.publish(self.tag, l)
        rc.rpush(self.lk, l)
        log.info(l)
        print("log", l)


if __name__ == '__main__':
    id = token_hex(8)
    print(id)
    while True:
        rc.sadd("runners", id)
        rc.setex("heartbeat:" + id, True, 10)
        for pb, status in rc.hgetall("status").items():
            if status == b"todo" and not rc.sismember("to_build", pb):  # feature add if path/branch in app2build
                rc.sadd("to_build", pb)  # to_build set ensures build atomicity for several builder
                pb = rc.spop("to_build")
                if pb:
                    path, branch, username = pb.decode('utf8').split(":")
                    with tempfile.TemporaryDirectory(prefix=id) as tmpdir:
                        print(tmpdir)
                        r = Runner(path, branch, username, tmpdir)
                        r.run()
        time.sleep(2)
        print(".", end='', flush=True)
