![](static/booklab.png)


A derived and lightened version of [BinderHub](https://github.com/jupyterhub/binderhub) for private repositories hosted on gitlab without kubernetes.


What is Booklab
---------------

**Booklab** allow you to BUILD and RUN a docker image using GitLab repository, then CONNECT with JupyterHub and that
allows users to interact with the code and environment within a live JupyterHub instance.


Why Booklab ?
-------------

Binder is a wonderful tool but limited to public GitHub repository and associated with kubernetes in fact mainly google cloud.
So an entry ticket to expensive for a small but smart jupyter on demand service.

How it works ?
--------------

 Simple is beautiful

A **web app** supported by flask and the oauth plugin, authenticate and configure jupyterhub setup via redis
A **runner**, checkout source code, build the docker image (repo2docker) and run jupyter. The philosophy is 1 runner > 1 notebook.
To scale booklab, increase number of runners.

Booklab used [Redis](https://redis.io) and [Træfik](https://traefik.io/) to setup the service.

Booklab workflow for one click deployement :

1. The home page display the list of your project on gitlab configured server (via an oauth request)
2. You click on the repository to use (path + branch) and go to the deploy page
3. BL create a pair of ssh key, push the public version on gitlab for the repository selected and the private key in redis.
4. BL set the status of path:branch with the flag 'todo'
5. A runner select atomically a path:branch to deploy. Retrieve the private ssh key, clone the repo, build the docker image and run it.
6. Traefik detect a new label instanciate by docker container and configure the route http://pathbranch.local
7. The deploy page has displayed the logs in real time during step 3 to 6 and now redirect to the new notebook




Redis structure :

| TYPE  | key                   | description                                                       |
|-------|-----------------------|-------------------------------------------------------------------|
| SET   | runners               | set of runner (token)  declared                                   |
| STR   | heartbeat:<token>     | expire each 30 seconds, and set by runner in waiting mode         |
| HASH  | status                | key <path>:<branch> value : current deployment status             |
| LIST  | log:<path>:<branch>   |  deployment logs                                                  |
| STR   | token:<path>:<branch> | jupyter token authentication                                      |
| HASH  | conf                  | key:value shared from the webapp to the runner

Configuration is done with config.yml

License:
--------
GNU AFFERO GENERAL PUBLIC LICENSE

https://www.gnu.org/licenses/agpl-3.0.txt


Install:
--------

install redis and setup up bind 0.0.0.0 and protection mode no

    sudo ./traefik --api --docker --docker.watch
    docker build -t booklab .
    docker run --label traefik.frontend.rule=Host:whoami.docker.localhost -e BOOKLAB_REDIS_URI=redis://cajon.irisa.fr:6379/0 -p 5000:5000  booklab

NB: A docker compose is in progress.



Credits :
----------
- Author: Sebastien Campion sebastien.campion@inria.fr
- Font : Bangers

