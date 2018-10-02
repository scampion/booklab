FROM alpine
RUN apk add --no-cache build-base python3-dev libffi-dev openssl-dev openssh linux-headers
RUN apk add --no-cache bash git nginx uwsgi uwsgi-python3 
RUN pip3 install --upgrade pip 


RUN mkdir /app 
COPY config.yml /app/config.yml
COPY app.py /app/app.py
COPY runner.py /app/runner.py
COPY requirements.txt /app/requirements.txt
RUN pip3 install -r /app/requirements.txt

RUN adduser --disabled-password flask
RUN chown -R flask /app
USER flask

WORKDIR /app

# expose web server port
# only http, for ssl use reverse proxy
EXPOSE 5000

CMD uwsgi --processes 5 --plugins-dir /usr/lib/uwsgi/ --need-plugin python3 --plugins-list --http-socket 0.0.0.0:5000 --static-map /static=/app/static --wsgi-file app.py --callable app



