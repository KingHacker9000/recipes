FROM python:3.13-alpine3.23

#Install all dependencies.
RUN apk add --no-cache postgresql-libs postgresql-client gettext zlib libjpeg libwebp libxml2-dev libxslt-dev openldap git libgcc libstdc++ nginx tini envsubst nodejs npm ripgrep

# Dedicated unprivileged identity and private credential root for subscription AI runtimes.
# Deployments should mount a private persistent volume/bind at /var/lib/tandoor-ai;
# never place this directory under MEDIA_ROOT or STATIC_ROOT.
RUN addgroup -S aiagent && adduser -S -D -H -G aiagent -h /var/lib/tandoor-ai aiagent && \
    install -d -m 0700 -o aiagent -g aiagent /var/lib/tandoor-ai

# Claude's Python SDK is installed with the Python requirements. On Alpine we use
# the official native/musl Claude Code npm package explicitly as its CLI runtime.
RUN npm install -g @anthropic-ai/claude-code@2.1.238 && claude --version

ENV AI_RUNTIME_DATA_DIR=/var/lib/tandoor-ai \
    CLAUDE_CLI_PATH=/usr/local/bin/claude

#Print all logs without buffering it.
ENV PYTHONUNBUFFERED=1 \
    DOCKER=true

#This port will be used by gunicorn.
EXPOSE 80 8080

#Create app dir and install requirements.
RUN mkdir /opt/recipes
WORKDIR /opt/recipes

COPY requirements.txt ./

# remove Development dependencies from requirements.txt
RUN sed -i '/# Development/,$d' requirements.txt
RUN apk add --no-cache --virtual .build-deps gcc musl-dev postgresql-dev zlib-dev jpeg-dev libwebp-dev openssl-dev libffi-dev cargo openldap-dev python3-dev xmlsec-dev xmlsec build-base g++ curl rust && \
    python -m venv venv && \
    /opt/recipes/venv/bin/python -m pip install --upgrade pip && \
    venv/bin/pip debug -v && \
    venv/bin/pip install wheel==0.45.1 && \
    venv/bin/pip install setuptools_rust==1.10.2 && \
    venv/bin/pip install -r requirements.txt --no-cache-dir &&\
    apk --purge del .build-deps

#Copy project and execute it.
COPY . ./

RUN <<EOF
    # delete default nginx config and link it to tandoors config
    rm -rf /etc/nginx/http.d
    ln -s /opt/recipes/http.d /etc/nginx/http.d
    # allow all write/read but set sticky bit to restrict non-root to only create files (conf templating in boot.sh)
    chmod 1777 /opt/recipes/http.d/
    # create symlinks to access and error log to show them on stdout
    ln -sf /dev/stdout /var/log/nginx/access.log
    ln -sf /dev/stderr /var/log/nginx/error.log
EOF

# commented for now https://github.com/TandoorRecipes/recipes/issues/3478
#HEALTHCHECK --interval=30s \
#            --timeout=5s \
#            --start-period=10s \
#            --retries=3 \
#            CMD [ "/usr/bin/wget", "--no-verbose", "--tries=1", "--spider", "http://127.0.0.1:8080/openapi" ]

# collect information from git repositories
RUN /opt/recipes/venv/bin/python version.py
# delete git repositories to reduce image size
RUN find . -type d -name ".git" | xargs rm -rf

RUN chmod +x boot.sh
ENTRYPOINT ["/sbin/tini", "--", "/opt/recipes/boot.sh"]
