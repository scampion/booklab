FROM alpine
MAINTAINER sebastien.campion@inria.fr

RUN apk add --no-cache build-base python3-dev libffi-dev openssl-dev openssh linux-headers
RUN apk add --no-cache wget bash git nginx uwsgi uwsgi-python3
RUN pip3 install --upgrade pip 
RUN wget -O /usr/local/bin/wait-for-it https://raw.githubusercontent.com/vishnubob/wait-for-it/master/wait-for-it.sh
RUN chmod a+x /usr/local/bin/wait-for-it
RUN mkdir /app
COPY requirements.txt /app/requirements.txt
RUN pip3 install -r /app/requirements.txt
COPY config.yml /app/config.yml
COPY app.py /app/app.py
COPY runner.py /app/runner.py



WORKDIR /app
EXPOSE 5000

#uwsgi --processes 5 --plugins-dir /usr/lib/uwsgi/ --need-plugin python3 --plugins-list --http-socket 0.0.0.0:5000 --static-map /static=/app/static --wsgi-file app.py --callable app
RUN printf "uwsgi --processes 5 " > /usr/local/bin/booklab
RUN printf "--plugins-dir /usr/lib/uwsgi/ --need-plugin python3 --plugins-list " >> /usr/local/bin/booklab
RUN printf "--http-socket 0.0.0.0:5000 --static-map /static=/app/static " >> /usr/local/bin/booklab
RUN printf "--wsgi-file app.py --callable app " >> /usr/local/bin/booklab
RUN chmod a+x /usr/local/bin/booklab

RUN adduser --disabled-password booklab
RUN chown -R booklab /app
USER booklab

CMD /usr/local/bin/booklab


